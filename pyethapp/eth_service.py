# https://github.com/ethereum/go-ethereum/wiki/Blockpool
import time
from ethereum.utils import privtoaddr, sha3
import rlp
from rlp.utils import encode_hex
from ethereum import processblock
from synchronizer import Synchronizer
from ethereum.slogging import get_logger
from ethereum.chain import Chain
from devp2p.service import WiredService
import eth_protocol
import gevent
from gevent.queue import Queue
log = get_logger('eth.chainservice')


# patch to get context switches between tx replay
processblock_apply_transaction = processblock.apply_transaction


def apply_transaction(block, tx):
    log.debug('apply_transaction ctx switch', at=time.time())
    gevent.sleep(0.001)
    return processblock_apply_transaction(block, tx)
processblock.apply_transaction = apply_transaction


rlp_hash_hex = lambda data: encode_hex(sha3(rlp.encode(data)))


class ChainService(WiredService):

    """
    Manages the chain and requests to it.
    """
    # required by BaseService
    name = 'chain'
    default_config = dict(eth=dict(privkey_hex=''))

    # required by WiredService
    wire_protocol = eth_protocol.ETHProtocol  # create for each peer

    # initialized after configure:
    chain = None
    genesis = None
    synchronizer = None
    config = None
    block_queue_size = 1024
    transaction_queue_size = 1024

    def __init__(self, app):
        self.config = app.config
        self.db = app.services.db
        assert self.db is not None
        super(ChainService, self).__init__(app)
        log.info('initializing chain')
        self.chain = Chain(self.db, new_head_cb=self._on_new_head)
        self.synchronizer = Synchronizer(self, force_sync=None)
        self.chain.coinbase = privtoaddr(self.config['eth']['privkey_hex'].decode('hex'))

        self.block_queue = Queue(maxsize=self.block_queue_size)
        self.transaction_queue = Queue(maxsize=self.transaction_queue_size)
        self.add_blocks_lock = False

    def _on_new_head(self, block):
        pass

    def add_block(self, t_block, proto):
        "adds a block to the block_queue and spawns _add_block if not running"
        self.block_queue.put((t_block, proto))  # blocks if full
        if not self.add_blocks_lock:
            self.add_blocks_lock = True
            gevent.spawn(self._add_blocks)

    def _add_blocks(self):
        log.warn('_add_blocks', qsize=self.block_queue.qsize())
        try:
            while not self.block_queue.empty():
                t_block, proto = self.block_queue.get()
                if t_block.header.hash in self.chain:
                    log.warn('known block', block=t_block)
                    continue
                if t_block.header.prevhash not in self.chain:
                    log.warn('missing parent', block=t_block)
                    continue
                if not t_block.header.check_pow(db='FIXME'):
                    log.warn('invalid pow', block=t_block)
                    # FIXME ban node
                    continue
                try:  # deserialize
                    st = time.time()
                    block = t_block.to_block(db=self.chain.db)
                    elapsed = time.time() - st
                    log.debug('deserialized', elapsed='%.2fs' % elapsed,
                              gas_used=block.gas_used, gpsec=int(block.gas_used / elapsed))
                except processblock.InvalidTransaction as e:
                    log.warn('invalid transaction', block=t_block, error=e)
                    # FIXME ban node
                    continue

                if self.chain.add_block(block):
                    log.debug('added', block=block)
                gevent.sleep(0.001)
        finally:
            self.add_blocks_lock = False

    def broadcast_newblock(self, block, chain_difficulty, origin=None):
        # eth_protocol needs to support
        log.warn('broadcasting newblock', origin=origin)
        bcast = self.app.services.peermanager.broadcast
        bcast(eth_protocol.ETHProtocol, 'newblock', args=(block, chain_difficulty),
              num_peers=None, exclude_protos=[origin])

    # wire protocol receivers ###########

    def on_wire_protocol_start(self, proto):
        log.debug('on_wire_protocol_start', proto=proto)
        assert isinstance(proto, self.wire_protocol)
        # register callbacks
        proto.receive_status_callbacks.append(self.on_receive_status)
        proto.receive_transactions_callbacks.append(self.on_receive_transactions)
        proto.receive_getblockhashes_callbacks.append(self.on_receive_getblockhashes)
        proto.receive_blockhashes_callbacks.append(self.on_receive_blockhashes)
        proto.receive_getblocks_callbacks.append(self.on_receive_getblocks)
        proto.receive_blocks_callbacks.append(self.on_receive_blocks)
        proto.receive_newblock_callbacks.append(self.on_receive_newblock)

        # send status
        head = self.chain.head
        proto.send_status(chain_difficulty=head.chain_difficulty(), chain_head_hash=head.hash,
                          genesis_hash=self.chain.genesis.hash)

    def on_wire_protocol_stop(self, proto):
        assert isinstance(proto, self.wire_protocol)
        log.debug('on_wire_protocol_stop', proto=proto)

    def on_receive_status(self, proto, eth_version, network_id, chain_difficulty, chain_head_hash,
                          genesis_hash):

        log.debug('status received', proto=proto, eth_version=eth_version)
        assert eth_version == proto.version, (eth_version, proto.version)
        if network_id != proto.network_id:
            log.warn("invalid network id", remote_id=proto, network_id=network_id)
            raise eth_protocol.ETHProtocolError('wrong network_id')

        # check genesis
        if genesis_hash != self.chain.genesis.hash:
            log.warn("invalid genesis hash", remote_id=proto, genesis=genesis_hash.encode('hex'))
            raise eth_protocol.ETHProtocolError('wrong genesis block')

        # request chain
        self.synchronizer.receive_status(proto, chain_head_hash, chain_difficulty)

        # send transactions
        transactions = self.chain.get_transactions()
        if transactions:
            log.debug("sending transactions", remote_id=proto)
            proto.send_transactions(*transactions)

    # transactions

    def on_receive_transactions(self, proto, transactions):
        "receives rlp.decoded serialized"
        log.debug('remote_transactions_received', count=len(transactions), remote_id=proto)
        log.debug('skipping, FIXME')
        return
        for tx in transactions:
            # fixme bloomfilter
            self.chain.add_transaction(tx)

    # blockhashes ###########

    def on_receive_getblockhashes(self, proto, child_block_hash, count):
        log.debug("handle_get_blockhashes", count=count, block_hash=encode_hex(child_block_hash))
        max_hashes = min(count, self.wire_protocol.max_getblockhashes_count)
        found = []
        if child_block_hash not in self.chain:
            log.debug("unknown block")
            proto.send_blockhashes(*[])
            return

        last = child_block_hash
        while len(found) < max_hashes:
            last = rlp.decode_lazy(self.chain.db.get(last))[0][0]
            if last:
                found.append(last)
            else:
                break

        log.debug("sending: found block_hashes", count=len(found))
        proto.send_blockhashes(*found)

    def on_receive_blockhashes(self, proto, blockhashes):
        if blockhashes:
            log.debug("on_receive_blockhashes", count=len(blockhashes), remote_id=proto)
        else:
            log.debug("recv 0 remote block hashes, signifying genesis block")
        self.synchronizer.receive_blockhashes(proto, blockhashes)

    # blocks ################

    def on_receive_getblocks(self, proto, blockhashes):
        log.debug("on_receive_getblocks", count=len(blockhashes))
        found = []
        for bh in blockhashes[:self.wire_protocol.max_getblocks_count]:
            try:
                found.append(self.chain.db.get(bh))
            except KeyError:
                log.debug("unknown block requested", block_hash=encode_hex(bh))
        if found:
            log.debug("found", count=len(found))
            proto.send_blocks(*found)

    def on_receive_blocks(self, proto, transient_blocks):
        log.debug("recv blocks", count=len(transient_blocks), remote_id=proto,
                  highest_number=max(x.header.number for x in transient_blocks))
        if transient_blocks:
            self.synchronizer.receive_blocks(proto, transient_blocks)

    def on_receive_newblock(self, proto, block, chain_difficulty):
        log.debug("recv newblock", block=block, remote_id=proto)
        self.synchronizer.receive_newblock(proto, block, chain_difficulty)
