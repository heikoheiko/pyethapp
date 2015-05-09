# https://github.com/ethereum/go-ethereum/wiki/Blockpool
import time
from ethereum.utils import sha3
import rlp
from rlp.utils import encode_hex
from ethereum import processblock
from synchronizer import Synchronizer
from ethereum.slogging import get_logger
from ethereum.chain import Chain
from ethereum.blocks import Block, VerificationFailed
from ethereum.transactions import Transaction
from devp2p.service import WiredService
import eth_protocol
import gevent
import gevent.lock
from gevent.queue import Queue
log = get_logger('eth.chainservice')


# patch to get context switches between tx replay
processblock_apply_transaction = processblock.apply_transaction


def apply_transaction(block, tx):
    # import traceback
    # print traceback.print_stack()
    log.debug('apply_transaction ctx switch', at=time.time())
    gevent.sleep(0.001)
    return processblock_apply_transaction(block, tx)
processblock.apply_transaction = apply_transaction


rlp_hash_hex = lambda data: encode_hex(sha3(rlp.encode(data)))


class DuplicatesFilter(object):

    def __init__(self, max_items=128):
        self.max_items = max_items
        self.filter = list()

    def known(self, data):
        if data not in self.filter:
            self.filter.append(data)
            if len(self.filter) > self.max_items:
                self.filter.pop(0)
            return False
        else:
            self.filter.append(self.filter.pop(0))
            return True


class ChainService(WiredService):

    """
    Manages the chain and requests to it.
    """
    # required by BaseService
    name = 'chain'
    default_config = dict(eth=dict(network_id=0))

    # required by WiredService
    wire_protocol = eth_protocol.ETHProtocol  # create for each peer

    # initialized after configure:
    chain = None
    genesis = None
    synchronizer = None
    config = None
    block_queue_size = 1024
    transaction_queue_size = 1024
    processed_gas = 0
    processed_elapsed = 0

    def __init__(self, app):
        self.config = app.config
        self.db = app.services.db
        assert self.db is not None
        super(ChainService, self).__init__(app)
        log.info('initializing chain')
        coinbase = app.services.accounts.coinbase
        self.chain = Chain(self.db, new_head_cb=self._on_new_head, coinbase=coinbase)
        log.info('chain at', number=self.chain.head.number)
        self.synchronizer = Synchronizer(self, force_sync=None)

        self.block_queue = Queue(maxsize=self.block_queue_size)
        self.transaction_queue = Queue(maxsize=self.transaction_queue_size)
        self.add_blocks_lock = False
        self.add_transaction_lock = gevent.lock.Semaphore()
        self.broadcast_filter = DuplicatesFilter()
        self.on_new_head_cbs = []
        self.on_new_head_candidate_cbs = []

    @property
    def is_syncing(self):
        return self.synchronizer.synctask is not None

    def _on_new_head(self, block):
        for cb in self.on_new_head_cbs:
            cb(block)
        self._on_new_head_candidate()  # we implicitly have a new head_candidate

    def _on_new_head_candidate(self):
        for cb in self.on_new_head_candidate_cbs:
            cb(self.chain.head_candidate)

    def add_transaction(self, tx, origin=None):
        assert isinstance(tx, Transaction)
        log.debug('add_transaction', locked=self.add_transaction_lock.locked())
        self.add_transaction_lock.acquire()
        success = self.chain.add_transaction(tx)
        self.add_transaction_lock.release()
        if success:
            self._on_new_head_candidate()
            self.broadcast_transaction(tx, origin=origin)  # asap

    def add_block(self, t_block, proto):
        "adds a block to the block_queue and spawns _add_block if not running"
        self.block_queue.put((t_block, proto))  # blocks if full
        if not self.add_blocks_lock:
            self.add_blocks_lock = True  # need to lock here (ctx switch is later)
            gevent.spawn(self._add_blocks)

    def add_mined_block(self, block):
        log.debug('adding mined block', block=block)
        assert block.check_pow()
        if self.chain.add_block(block):
            log.info('added', block=block, ts=time.time())
            assert block == self.chain.head
            self.broadcast_newblock(block, chain_difficulty=block.chain_difficulty())

    def knows_block(self, block_hash):
        "if block is in chain or in queue"
        if block_hash in self.chain:
            return True
        # check if queued or processed
        for i in range(len(self.block_queue.queue)):
            if block_hash == self.block_queue.queue[i][0].header.hash:
                return True
        return False

    def _add_blocks(self):
        log.debug('add_blocks', qsize=self.block_queue.qsize(),
                  add_tx_lock=self.add_transaction_lock.locked())
        assert self.add_blocks_lock is True
        self.add_transaction_lock.acquire()
        try:
            while not self.block_queue.empty():
                t_block, proto = self.block_queue.peek()  # peek: knows_block while processing
                if t_block.header.hash in self.chain:
                    log.warn('known block', block=t_block)
                    self.block_queue.get()
                    continue
                if t_block.header.prevhash not in self.chain:
                    log.warn('missing parent', block=t_block)
                    self.block_queue.get()
                    continue
                # FIXME, this is also done in validation and in synchronizer for new_blocks
                if not t_block.header.check_pow():
                    log.warn('invalid pow', block=t_block, FIXME='ban node')
                    self.block_queue.get()
                    continue
                try:  # deserialize
                    st = time.time()
                    block = t_block.to_block(db=self.chain.db)
                    elapsed = time.time() - st
                    log.debug('deserialized', elapsed='%.4fs' % elapsed,
                              gas_used=block.gas_used, gpsec=self.gpsec(block.gas_used, elapsed))
                except processblock.InvalidTransaction as e:
                    log.warn('invalid transaction', block=t_block, error=e, FIXME='ban node')
                    self.block_queue.get()
                    continue
                except VerificationFailed as e:
                    log.warn('verification failed', error=e, FIXME='ban node')
                    self.block_queue.get()
                    continue

                if self.chain.add_block(block):
                    log.info('added', block=block, ts=time.time())
                self.block_queue.get()  # remove block from queue (we peeked only)
                gevent.sleep(0.001)
        finally:
            self.add_blocks_lock = False
            self.add_transaction_lock.release()

    def gpsec(self, gas_spent=0, elapsed=0):
        self.processed_gas += gas_spent
        self.processed_elapsed += elapsed
        return int(self.processed_gas / (0.001 + self.processed_elapsed))

    def broadcast_newblock(self, block, chain_difficulty=None, origin=None):
        if not chain_difficulty:
            assert block.hash in self.chain
            chain_difficulty = block.chain_difficulty()
        assert isinstance(block, (eth_protocol.TransientBlock, Block))
        if self.broadcast_filter.known(block.header.hash):
            log.debug('already broadcasted block')
        else:
            log.debug('broadcasting newblock', origin=origin)
            bcast = self.app.services.peermanager.broadcast
            bcast(eth_protocol.ETHProtocol, 'newblock', args=(block, chain_difficulty),
                  exclude_peers=[origin.peer] if origin else [])

    def broadcast_transaction(self, tx, origin=None):
        assert isinstance(tx, Transaction)
        if self.broadcast_filter.known(tx.hash):
            log.debug('already broadcasted tx')
        else:
            log.debug('broadcasting tx', origin=origin)
            bcast = self.app.services.peermanager.broadcast
            bcast(eth_protocol.ETHProtocol, 'transactions', args=(tx,),
                  exclude_peers=[origin.peer] if origin else [])

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
        if network_id != self.config['eth'].get('network_id', proto.network_id):
            log.warn("invalid network id", remote_network_id=network_id,
                     expected_network_id=self.config['eth'].get('network_id', proto.network_id))
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
        for tx in transactions:
            self.add_transaction(tx, origin=proto)

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
            try:
                last = rlp.decode_lazy(self.chain.db.get(last))[0][0]
            except KeyError:
                # this can happen if we started a chain download, which did not complete
                # should not happen if the hash is part of the canonical chain
                log.warn('KeyError in getblockhashes', hash=last)
                break
            if last:
                found.append(last)
            else:
                break

        log.debug("sending: found block_hashes", count=len(found))
        proto.send_blockhashes(*found)

    def on_receive_blockhashes(self, proto, blockhashes):
        if blockhashes:
            log.debug("on_receive_blockhashes", count=len(blockhashes), remote_id=proto,
                      first=encode_hex(blockhashes[0]), last=encode_hex(blockhashes[-1]))
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
        blk_number = max(x.header.number for x in transient_blocks) if transient_blocks else 0
        log.debug("recv blocks", count=len(transient_blocks), remote_id=proto,
                  highest_number=blk_number)
        if transient_blocks:
            self.synchronizer.receive_blocks(proto, transient_blocks)

    def on_receive_newblock(self, proto, block, chain_difficulty):
        log.debug("recv newblock", block=block, remote_id=proto)
        self.synchronizer.receive_newblock(proto, block, chain_difficulty)
