
from rlp.utils import encode_hex
from ethereum.slogging import get_logger
import time
log = get_logger('eth.sync')


class HashChainTask(object):

    """
    - get hashes chain until we see a known block hash
    """

    NUM_HASHES_PER_REQUEST = 2000
    timeout = 2.

    def __init__(self, chain, proto, block_hash):
        self.chain = chain
        self.proto = proto
        self.hash_chain = []  # [youngest, ..., oldest]
        self.request(block_hash)

    def request(self, block_hash):
        log.debug('requesting block_hashes', proto=self.proto, start=encode_hex(block_hash))
        self.proto.send_getblockhashes(block_hash, self.NUM_HASHES_PER_REQUEST)

    def received_block_hashes(self, block_hashes):
        log.debug('HashChainTask.received_block_hashes', num=len(block_hashes), proto=self.proto)
        if block_hashes and self.chain.genesis.hash == block_hashes[-1]:
            log.debug('has different chain starting from genesis')
        for bh in block_hashes:
            if bh in self.chain or bh == self.chain.genesis.hash:
                log.debug('matching block hash found',
                          hash=encode_hex(bh), num_to_fetch=len(self.hash_chain))
                if not self.hash_chain:
                    log.debug('matching block hash found, not missing anymore',
                              block_num=self.chain.get(bh).number)
                return list(reversed(self.hash_chain))
            self.hash_chain.append(bh)
        if len(block_hashes) == 0:
            return list(reversed(self.hash_chain))
        self.request(bh)


class SynchronizationTask(object):

    """
    Created if we receive a unknown block w/o known parent. Possibly from a different branch.

    - get hashes chain until we see a known block hash
    - request missing blocks

    - once synced
        - rerequest blocks that lacked a reference before syncing
    """
    NUM_BLOCKS_PER_REQUEST = 200
    timeout = 2.

    def __init__(self, chain, proto, block_hash):
        self.chain = chain
        self.proto = proto
        self.block_hash = block_hash
        self.hash_chain = []  # [oldest to youngest]
        log.debug('syncing', proto=self.proto, hash=encode_hex(block_hash))
        self.hash_chain_task = HashChainTask(self.chain, self.proto, block_hash)
        self.last_response = time.time()

    @property
    def did_timeout(self):
        b = time.time() - self.last_response > self.timeout
        if b:
            log.debug('did timeout', proto=self.proto)
        return b

    def received_block_hashes(self, block_hashes):
        if block_hashes:  # requires success
            self.last_response = time.time()
        res = self.hash_chain_task.received_block_hashes(block_hashes)
        if res:
            self.hash_chain = res + [self.block_hash]
            log.debug('completed hash chain', proto=self.proto, num=len(res))
            self.request_blocks()

    def received_blocks(self, transient_blocks):
        if transient_blocks:  # requires success
            self.last_response = time.time()
        log.debug('blocks received', proto=self.proto, num=len(
            transient_blocks), missing=len(self.hash_chain))
        for tb in transient_blocks:
            if len(self.hash_chain) and self.hash_chain[0] == tb.header.hash:
                self.hash_chain.pop(0)
            else:
                log.debug('received unexpected block', proto=self.proto, block=tb)
                return False
        if self.hash_chain:
            # still blocks to fetch
            log.debug('still missing blocks', proto=self.proto, num=len(self.hash_chain))
            self.request_blocks()
        else:  # done
            return True

    def request_blocks(self):
        log.debug('requesting missing blocks', proto=self.proto,
                  requested=self.NUM_BLOCKS_PER_REQUEST, missing=len(self.hash_chain))
        blockhashes = self.hash_chain[:self.NUM_BLOCKS_PER_REQUEST]
        self.proto.send_getblocks(*blockhashes)


class Synchronizer(object):

    """"
    Cases:
        on "recv_Status": received unknown head_hash w/ sufficient difficulty
        on "recv_Blocks": received block w/o parent (new block mined, competing chain discovered)

    Naive Strategy:
        assert we see a block for which we have no parent
        assume that the sending proto knows the parent
        if we have not yet syncer for this unknown block:
            create new syncer
            sync direction genesis until we see known block_hash
            sync also (re)requests the block we missed, so it can be added on top of the
                synced chain
        else
            do nothing
            syncing (if finished) will be started with the next broadcasted block w/ missing parent
    """

    def __init__(self, chain):
        self.chain = chain
        self.synchronization_tasks = {}  # proto > syncer # syncer.unknown_hash as marker for task

    def stop_synchronization(self, proto):
        log.debug('sync stopped', proto=proto)
        if proto in self.synchronization_tasks:
            del self.synchronization_tasks[proto]

    def _cleanup(self):
        for proto, t in self.synchronization_tasks.items():
            if t.did_timeout:
                log.debug('deleting timed out task', proto=proto)
                del self.synchronization_tasks[proto]

    def synchronize_unknown_block(self, proto, block_hash, force=False, t_block=None):
        "Case: block with unknown parent. Fetches unknown ancestors and this block"
        log.debug('sync unknown', proto=proto, block=encode_hex(block_hash))
        self._cleanup()
        # check if we are just one block away, if so request the missing block only
        if t_block and t_block.header.number - self.chain.head.number == 2:
            # we are probably only missing one block in between
            assert t_block.header.hash == block_hash
            log.debug('trying to sync INBETWEEN', proto=proto, hash=encode_hex(block_hash))
            proto.send_getblocks(*[t_block.header.prevhash, block_hash])
            # and also do the ordinary syncing

        if block_hash == self.chain.genesis.hash or block_hash in self.chain:
            log.debug('known_hash, skipping', proto=proto, hash=encode_hex(block_hash))
            return
        if proto is None:
            return
        if force or proto not in self.synchronization_tasks:
            log.debug('new sync task', proto=proto)
            self.synchronization_tasks[proto] = SynchronizationTask(self.chain, proto, block_hash)
        else:
            elapsed = time.time() - self.synchronization_tasks[proto].last_response
            assert elapsed < SynchronizationTask.timeout
            log.debug('existing synctask', proto=proto, elapsed=elapsed)

    def synchronize_status(self, proto, block_hash, total_difficulty):
        "Case: unknown head with sufficient difficulty"
        log.debug('sync status', proto=proto,  hash=block_hash.encode('hex'),
                  total_difficulty=total_difficulty)
        self._cleanup()
        # guesstimate the max difficulty difference possible for a sucessfully competing chain
        # worst case if skip it: we are on a stale chain until the other catched up
        # assume difficulty is constant
        num_blocks_behind = 7
        avg_uncles_per_block = 4
        max_diff = self.chain.head.difficulty * \
            num_blocks_behind * (1 + avg_uncles_per_block)
        if total_difficulty + max_diff > self.chain.head.difficulty:
            log.debug('sufficient difficulty, syncing', proto=proto)
            self.synchronize_unknown_block(proto, block_hash)
        else:
            log.debug('insufficient difficulty, not syncing', proto=proto)

    def received_block_hashes(self, proto, block_hashes):
        log.debug("received_block_hashes", proto=proto, num=len(
            block_hashes), tasks=self.synchronization_tasks)

        if proto in self.synchronization_tasks:
            log.debug("Synchronizer.received_block_hashes", proto=proto, num=len(block_hashes))
            self.synchronization_tasks[proto].received_block_hashes(block_hashes)

    def received_blocks(self, proto, transient_blocks):
        if proto in self.synchronization_tasks:
            res = self.synchronization_tasks[proto].received_blocks(transient_blocks)
            if res is True:
                log.debug("Synchronizer.received_blocks: chain synced", proto=proto)
                del self.synchronization_tasks[proto]
