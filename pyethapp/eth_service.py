# https://github.com/ethereum/go-ethereum/wiki/Blockpool
import time
from operator import attrgetter
from pyethereum.db import EphemDB
from pyethereum.utils import privtoaddr, sha3
import rlp
from rlp.utils import decode_hex, encode_hex
from pyethereum import blocks
from pyethereum import processblock
from pyethereum.miner import Miner
from blockpool import Synchronizer
from pyethereum.slogging import get_logger
from pyethereum.chain import Chain
from devp2p.service import WiredService
import eth_protocol
log = get_logger('eth.chainservice')


rlp_hash_hex = lambda data: encode_hex(sha3(rlp.encode(data)))

NUM_BLOCKS_PER_REQUEST = 256  # MAX_GET_CHAIN_REQUEST_BLOCKS
MAX_GET_CHAIN_REQUEST_BLOCKS = 512
MAX_GET_CHAIN_SEND_HASHES = 2048


class ChainService(WiredService):

    """
    Manages the chain and requests to it.
    """
    # required by BaseService
    name = 'chain'
    default_config = dict(chain=dict(coinbase=privtoaddr(sha3('cow'))))

    # required by WiredService
    wire_protocol = eth_protocol.ETHProtocol  # create for each peer

    # initialized after configure:
    chain = None
    genesis = None
    miner = None
    synchronizer = None
    config = None

    def __init__(self, app):
        self.config = app.config
        self.db = app.services.db
        assert self.db is not None
        super(ChainService, self).__init__(app)
        log.info('initializing chain')
        self.chain = Chain(self.db, new_head_cb=self._on_new_head)
        self.new_miner()
        self.synchronizer = Synchronizer(self.chain)

    def _on_new_head(self, block):
        self.new_miner()  # reset mining
        # if we are not syncing, forward all blocks
        if not self.synchronizer.synchronization_tasks:
            log.debug("broadcasting new head", block=block)
            # signals.broadcast_new_block.send(sender=None, block=block)

    def loop_body(self):
        ts = time.time()
        pct_cpu = self.config['misc']['mining']
        if pct_cpu > 0:
            self.mine()
            delay = (time.time() - ts) * (100. / pct_cpu - 1)
            assert delay >= 0
            time.sleep(min(delay, 1.))
        else:
            time.sleep(.01)

    def new_miner(self):
        "new miner is initialized if HEAD is updated"
        if not self.config:
            return  # not configured yet

        # prepare uncles
        uncles = set(self.chain.get_uncles(self.chain.head))
        blk = self.chain.head
        for i in range(8):
            for u in blk.uncles:  # assuming uncle headers
                u = sha3(rlp.encode(u))
                if u in self:
                    uncles.discard(self.chain.get(u))
            if blk.has_parent():
                blk = blk.get_parent()

        coinbase = self.config['chain']['coinbase']
        uncles = list(uncles)  # FIXME VERY MUCH !!!
        miner = Miner(self.chain.head, uncles, coinbase)
        if self.miner:
            for tx in self.miner.get_transactions():
                miner.add_transaction(tx)
        self.miner = miner

    def mine(self):
        block = self.miner.mine()
        if block:
            # create new block
            if not self.chain.add_block(block):
                log.debug("newly mined block is invalid!?", block_hash=block)
                self.new_miner()

    def receive_chain(self, transient_blocks, proto=None):
        _db = EphemDB()
        # assuming to receive chain order w/ oldest block first
        transient_blocks.sort(key=lambda x: x.header.number)
        assert transient_blocks[0].header.number <= transient_blocks[-1].header.number

        # notify syncer
        self.synchronizer.received_blocks(proto, transient_blocks)

        for t_block in transient_blocks:  # oldest to newest
            log.debug('Checking PoW', block=t_block.hex_hash)
            if not t_block.header.check_pow(_db):
                log.debug('Invalid PoW', block=t_block.hex_hash)
                continue
            log.debug('Deserializing', block=t_block.hex_hash)
            if t_block.header.prevhash == self.chain.head.hash:
                log.debug('is child')
            if t_block.header.prevhash == self.chain.genesis.hash:
                log.debug('is child of genesis')
            try:
                # block = blocks.Block(t_block.header, t_block.transaction_list, t_block.uncles,
                #                      db=self.chain.db)
                block = t_block.to_block(db=self.chain.db)
            except processblock.InvalidTransaction as e:
                # FIXME there might be another exception in
                # blocks.deserializeChild when replaying transactions
                # if this fails, we need to rewind state
                log.debug('invalid transaction', block=t_block.hex_hash, error=e)
                # stop current syncing of this chain and skip the child blocks
                self.synchronizer.stop_synchronization(proto)
                return
            except blocks.UnknownParentException:
                log.debug('unknown parent', block=t_block)
                if t_block.header.prevhash == blocks.GENESIS_PREVHASH:
                    log.debug('Rec Incompatible Genesis', block=t_block.hex_hash)
                    if proto is not None:
                        proto.send_disconnect(reason='Wrong genesis block')
                    raise eth_protocol.ETHProtocolError('wrong genesis')
                else:  # should be a single newly mined block
                    assert t_block.header.prevhash not in self.chain
                    if t_block.header.prevhash == self.chain.genesis.hash:
                        print t_block.serialize().encode('hex')
                    assert t_block.header.prevhash != self.chain.genesis.hash
                    log.debug('unknown parent', block=t_block,
                              parent_hash=encode_hex(t_block.header.prevhash), remote_id=proto)
                    if len(transient_blocks) != 1:
                        # strange situation here.
                        # we receive more than 1 block, so it's not a single newly mined one
                        # sync/network/... failed to add the needed parent at some point
                        # well, this happens whenever we can't validate a block!
                        # we should disconnect!
                        log.warn(
                            'blocks received, but unknown parent.', num=len(transient_blocks))
                    if proto is not None:
                        # request chain for newest known hash
                        self.synchronizer.synchronize_unknown_block(
                            proto, transient_blocks[-1].header.hash)
                break
            if block.hash in self.chain:
                log.debug('known', block=block)
            else:
                assert block.has_parent()
                # assume single block is newly mined block
                success = self.chain.add_block(block)
                if success:
                    log.debug('added', block=block)

    def add_transaction(self, transaction):
        _log = log.bind(tx_hash=transaction)
        _log.debug("add transaction")
        res = self.miner.add_transaction(transaction)
        if res:
            _log.debug("broadcasting valid")
            #signals.send_local_transactions.send(sender=None, transactions=[transaction])
        return res

    def get_transactions(self):
        log.debug("get_transactions called")
        return self.miner.get_transactions()

    # wire protocol receivers ###########

    def on_wire_protocol_start(self, proto):
        log.debug('on_wire_protocol_start', proto=proto)
        assert isinstance(proto, self.wire_protocol)
        proto.receive_status_callbacks.append(self.on_receive_status)
        proto.receive_transactions_callbacks.append(self.on_receive_transactions)
        proto.send_GetBlockHashes = proto.send_getblockhashes  # FIXME, hack for pyethereum sync
        proto.send_GetBlocks = proto.send_getblocks  # FIXME, hack for pyethereum sync
        proto.receive_getblockhashes_callbacks.append(self.on_receive_getblockhashes)
        proto.receive_blockhashes_callbacks.append(self.on_receive_blockhashes)
        proto.receive_getblocks_callbacks.append(self.on_receive_getblocks)
        proto.receive_blocks_callbacks.append(self.on_receive_blocks)
        proto.receive_newblock_callbacks.append(self.on_receive_newblock)

        # send status
        head = self.chain.head
        proto.send_status(total_difficulty=head.chain_difficulty(), chain_head_hash=head.hash,
                          genesis_hash=self.chain.genesis.hash)

    def on_wire_protocol_stop(self, proto):
        assert isinstance(proto, self.wire_protocol)
        log.debug('on_wire_protocol_stop', proto=proto)

    def on_receive_status(self, proto, eth_version, network_id, total_difficulty, chain_head_hash,
                          genesis_hash):

        log.debug('status received', proto=proto, eth_version=eth_version)
        assert eth_version == proto.version
        assert network_id == proto.network_id

        # check genesis
        if genesis_hash != self.chain.genesis.hash:
            raise eth_protocol.ETHProtocolError('wrong genesis block')

        # request chain
        self.synchronizer.synchronize_status(proto, chain_head_hash, total_difficulty)

        # send transactions
        log.debug("sending transactions", remote_id=proto)
        transactions = self.get_transactions()
        proto.send_transactions(*transactions)

    # transactions

    def on_receive_transactions(self, proto, transactions):
        "receives rlp.decoded serialized"
        log.debug('remote_transactions_received', count=len(transactions), remote_id=proto)
        for tx in transactions:
            # fixme bloomfilter
            self.add_transaction(tx)

    # blockhashes ###########

    def on_receive_getblockhashes(self, proto, child_block_hash, count):
        log.debug("handle_get_block_hashes", count=count, block_hash=encode_hex(child_block_hash))
        max_hashes = min(count, MAX_GET_CHAIN_SEND_HASHES)
        found = []
        if child_block_hash not in self.chain:
            log.debug("unknown block")
            proto.send_blockhashes([])
        last = self.chain.get(child_block_hash)
        while len(found) < max_hashes:
            if last.has_parent():
                last = last.get_parent()
                found.append(last.hash)
            else:
                break
        log.debug("sending: found block_hashes", count=len(found))
        proto.send_blockhashes(*found)

    def on_receive_blockhashes(self, proto, block_hashes):
        if block_hashes:
            log.debug("on_receive_blockhashes", count=len(block_hashes), remote_id=proto,
                      first=encode_hex(block_hashes[0]), last=encode_hex(block_hashes[-1]))
        else:
            log.debug("recv 0 remote block hashes, signifying genesis block")
        self.synchronizer.received_block_hashes(proto, block_hashes)

    # blocks ################

    def on_receive_getblocks(self, proto, block_hashes):
        log.debug("on_receive_getblocks", count=len(block_hashes))
        found = []
        for bh in block_hashes[:MAX_GET_CHAIN_REQUEST_BLOCKS]:
            if bh in self.chain:
                found.append(self.chain.get(bh))
            else:
                log.debug("unknown block requested", block_hash=encode_hex(bh))
        log.debug("found", count=len(found))
        proto.send_blocks(*found)

    def on_receive_blocks(self, proto, transient_blocks):
        log.debug("recv remote blocks", count=len(transient_blocks), remote_id=proto,
                  highest_number=max(x.header.number for x in transient_blocks))
        if transient_blocks:
            self.receive_chain(transient_blocks, proto)

    def on_receive_newblock(self, proto, block, total_difficulty):
        log.debug("recv new remote block", block=block, remote_id=proto)
        self.receive_chain([block], proto)
