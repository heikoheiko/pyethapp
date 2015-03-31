import time
from operator import attrgetter
from pyethereum import signals
from pyethereum.db import EphemDB
from pyethereum import utils
import rlp
from rlp.utils import decode_hex, encode_hex
from pyethereum import blocks
from pyethereum import processblock
from pyethereum import peermanager
from pyethereum.transactions import Transaction
from pyethereum.miner import Miner
from pyethereum.synchronizer import Synchronizer
from pyethereum.peer import MAX_GET_CHAIN_SEND_HASHES
from pyethereum.peer import MAX_GET_CHAIN_REQUEST_BLOCKS
from pyethereum.slogging import get_logger
from pyethereum.chain import Chain
from devp2p.service import WiredBaseService
import eth_protocol
log = get_logger('eth.chainservice')


rlp_hash_hex = lambda data: encode_hex(utils.sha3(rlp.encode(data)))

NUM_BLOCKS_PER_REQUEST = 256  # MAX_GET_CHAIN_REQUEST_BLOCKS


class ChainService(WiredBaseService):

    """
    Manages the chain and requests to it.
    """
    # required by BaseService
    name = 'chain'
    default_config = dict(chain=dict())

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
        self.db = app.services['db']
        super(ChainService, self).__init__()
        self.chain = Chain(self.db, new_head_cb=self._on_new_head)
        self.new_miner()
        self.synchronizer = Synchronizer(self)

    def _on_new_head(self, block):
        self.new_miner()  # reset mining
        # if we are not syncing, forward all blocks
        if not self.synchronizer.synchronization_tasks:
            log.debug("broadcasting new head", block=block)
            signals.broadcast_new_block.send(sender=None, block=block)

    def loop_body(self):
        ts = time.time()
        pct_cpu = self.config.getint('misc', 'mining')
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
                u = utils.sha3(rlp.encode(u))
                if u in self:
                    uncles.discard(self.chain.get(u))
            if blk.has_parent():
                blk = blk.get_parent()

        coinbase = decode_hex(self.config.get('wallet', 'coinbase'))
        miner = Miner(self.chain.head, uncles, coinbase)
        if self.miner:
            for tx in self.miner.get_transactions():
                miner.add_transaction(tx)
        self.miner = miner

    def mine(self):
        with self.lock:
            block = self.miner.mine()
            if block:
                # create new block
                if not self.chain.add_block(block):
                    log.debug("newly mined block is invalid!?", block_hash=block)
                    self.new_miner()

    def receive_chain(self, transient_blocks, peer=None):
        with self.lock:
            _db = EphemDB()
            # assuming to receive chain order w/ oldest block first
            transient_blocks.sort(key=attrgetter('number'))
            assert transient_blocks[0].number <= transient_blocks[-1].number

            # notify syncer
            self.synchronizer.received_blocks(peer, transient_blocks)

            for t_block in transient_blocks:  # oldest to newest
                log.debug('Checking PoW', block_hash=t_block.hash)
                if not t_block.header.check_pow(_db):
                    log.debug('Invalid PoW', block_hash=t_block.hash)
                    continue
                log.debug('Deserializing', block_hash=t_block.hash)
                try:
                    block = blocks.Block(t_block.header, t_block.transaction_list, t_block.uncles,
                                         db=self.chain.db)
                except processblock.InvalidTransaction as e:
                    # FIXME there might be another exception in
                    # blocks.deserializeChild when replaying transactions
                    # if this fails, we need to rewind state
                    log.debug('invalid transaction', block_hash=t_block, error=e)
                    # stop current syncing of this chain and skip the child blocks
                    self.synchronizer.stop_synchronization(peer)
                    return
                except blocks.UnknownParentException:
                    if t_block.prevhash == blocks.GENESIS_PREVHASH:
                        log.debug('Rec Incompatible Genesis', block_hash=t_block)
                        if peer:
                            peer.send_Disconnect(reason='Wrong genesis block')
                    else:  # should be a single newly mined block
                        assert t_block.prevhash not in self
                        assert t_block.prevhash != self.chain.genesis.hash
                        log.debug('unknown parent', block_hash=t_block,
                                  parent_hash=encode_hex(t_block.prevhash), remote_id=peer)
                        if len(transient_blocks) != 1:
                            # strange situation here.
                            # we receive more than 1 block, so it's not a single newly mined one
                            # sync/network/... failed to add the needed parent at some point
                            # well, this happens whenever we can't validate a block!
                            # we should disconnect!
                            log.warn(
                                'blocks received, but unknown parent.', num=len(transient_blocks))
                        if peer:
                            # request chain for newest known hash
                            self.synchronizer.synchronize_unknown_block(
                                peer, transient_blocks[-1].hash)
                    break
                if block.hash in self.chain:
                    log.debug('known', block_hash=block)
                else:
                    assert block.has_parent()
                    # assume single block is newly mined block
                    success = self.chain.add_block(block)
                    if success:
                        log.debug('added', block_hash=block)

    def add_transaction(self, transaction):
        _log = log.bind(tx_hash=transaction)
        _log.debug("add transaction")
        with self.lock:
            res = self.miner.add_transaction(transaction)
            if res:
                _log.debug("broadcasting valid")
                signals.send_local_transactions.send(
                    sender=None, transactions=[transaction])
            return res

    def get_transactions(self):
        log.debug("get_transactions called")
        return self.miner.get_transactions()

    # wire protocol receivers ###########

    def on_peer_handshake(self, proto):
        assert isinstance(proto, self.wire_protocol)
        proto.receive_status_callbacks.append(self.receive_status)
        proto.receive_transactions_callbacks.append(self.receive_transactions)
        proto.receive_getblockhashes_callbacks.append(self.receive_getblockhashes)
        proto.receive_blockhashes_callbacks.append(self.receive_blockhashes)
        proto.receive_getblocks_callbacks.append(self.receive_getblocks)
        proto.receive_blocks_callbacks.append(self.receive_blocks)
        proto.receive_newblock_callbacks.append(self.receive_newblock)

    def on_peer_disconnect(self, proto):
        assert isinstance(proto, self.wire_protocol)

    def handle_get_block_hashes(self, proto, block_hash, count, peer, **kwargs):
        _log = log.bind(block_hash=encode_hex(block_hash))
        _log.debug("handle_get_block_hashes", count=count)
        max_hashes = min(count, MAX_GET_CHAIN_SEND_HASHES)
        found = []
        if block_hash not in self.chain:
            log.debug("unknown block")
            peer.send_BlockHashes([])
        last = self.chain.get(block_hash)
        while len(found) < max_hashes:
            if last.has_parent():
                last = last.get_parent()
                found.append(last.hash)
            else:
                break
        _log.debug("sending: found block_hashes", count=len(found))
        peer.send_BlockHashes(found)

    def handle_get_blocks(self, proto, block_hashes, peer, **kwargs):
        log.debug("handle_get_blocks", count=len(block_hashes))
        found = []
        for bh in block_hashes[:MAX_GET_CHAIN_REQUEST_BLOCKS]:
            if bh in self.chain:
                found.append(self.chain.get(bh))
            else:
                log.debug("unknown block requested", block_hash=encode_hex(bh))
        log.debug("found", count=len(found))
        peer.send_Blocks(found)

    def peer_status_received(self, proto, genesis_hash, peer, **kwargs):
        log.debug("received status", remote_id=peer, genesis_hash=encode_hex(genesis_hash))
        # check genesis
        if genesis_hash != self.chain.genesis.hash:
            return peer.send_Disconnect(reason='wrong genesis block')

        # request chain
        self.synchronizer.synchronize_status(
            peer, peer.status_head_hash, peer.status_total_difficulty)
        # send transactions
        log.debug("sending transactions", remote_id=peer)
        transactions = self.get_transactions()
        transactions = [rlp.decode(x.serialize()) for x in transactions]
        peer.send_Transactions(transactions)

    def peer_handshake(self, proto, peer, **kwargs):
        # reply with status if not yet sent
        if peer.has_ethereum_capabilities() and not peer.status_sent:
            log.debug("handshake, sending status", remote_id=peer)
            peer.send_Status(self.chainhead.hash, self.chain.head.chain_difficulty(),
                             self.chain.genesis.hash)
        else:
            log.debug("handshake, but peer has no 'eth' capablities", remote_id=peer)

    def remote_transactions_received_handler(self, proto, transactions, peer, **kwargs):
        "receives rlp.decoded serialized"
        txl = [Transaction.deserialize(rlp.encode(tx)) for tx in transactions]
        log.debug('remote_transactions_received', count=len(txl), remote_id=peer)
        for tx in txl:
            peermanager.txfilter.add(tx, peer)  # FIXME
            self.add_transaction(tx)

    def local_transaction_received_handler(self, proto, transaction, **kwargs):
        "receives transaction object"
        log.debug('local_transaction_received', tx_hash=transaction)
        self.add_transaction(transaction)

    def new_block_received_handler(self, proto, block, peer, **kwargs):
        log.debug("recv new remote block", block_hash=block, remote_id=peer)
        self.receive_chain([block], peer)

    def remote_blocks_received_handler(self, proto, transient_blocks, peer, **kwargs):
        log.debug("recv remote blocks", count=len(transient_blocks), remote_id=peer,
                  highest_number=max(x.number for x in transient_blocks))
        if transient_blocks:
            self.receive_chain(transient_blocks, peer)

    def remote_block_hashes_received_handler(self, proto, block_hashes, peer, **kwargs):
        if block_hashes:
            log.debug("recv remote block_hashes", count=len(block_hashes), remote_id=peer,
                      first=encode_hex(block_hashes[0]), last=encode_hex(block_hashes[-1]))
        else:
            log.debug("recv 0 remore block hashes, signifying genesis block")
        self.synchronizer.received_block_hashes(peer, block_hashes)
