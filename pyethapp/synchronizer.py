from gevent.event import AsyncResult
import gevent
import time
from eth_protocol import TransientBlock
from ethereum.slogging import get_logger
log = get_logger('eth.sync.task')


class SyncTask(object):

    """
    synchronizes a the chain starting from a given blockhash
    blockchain hash is fetched from a single peer (which led to the unknown blockhash)
    blocks are fetched from the best peers

    with missing block:
        fetch hashes
            until known block
    for hashes
        fetch blocks
            for each block
                chainservice.add_blocks() # blocks if queue is full
    """
    max_blocks_per_request = 256
    initial_blockhashes_per_request = 16
    max_blockhashes_per_request = 2048
    blocks_request_timeout = 8.  # 256 * ~2KB = 512KB
    blockhashes_request_timeout = 8.  # 32 * 2048 = 65KB

    def __init__(self, synchronizer, proto, blockhash, chain_difficulty):
        self.synchronizer = synchronizer
        self.chain = synchronizer.chain
        self.chainservice = synchronizer.chainservice
        self.initiator_proto = proto
        self.blockhash = blockhash
        self.chain_difficulty = chain_difficulty
        self.requests = dict()  # proto: Event
        gevent.spawn(self.run)

    def run(self):
        log.info('spawning new syntask')
        try:
            self.fetch_hashchain()
        except Exception as e:
            self.exit(success=False)
            raise e

    def run(self):
        log.info('spawning new syntask')
        self.fetch_hashchain()

    def exit(self, success=False):
        if not success:
            log.warn('syncing failed')
        else:
            log.debug('sucessfully synced')
        self.synchronizer.synctask_exited(success)

    def fetch_hashchain(self):
        log.debug('fetching hashchain')
        blockhashes_chain = [self.blockhash]  # youngest to oldest

        blockhash = self.blockhash
        assert blockhash not in self.chain

        # get block hashes until we found a known one
        max_blockhashes_per_request = self.initial_blockhashes_per_request
        while blockhash not in self.chain:
            # proto with highest_difficulty should be the proto we got the newblock from
            blockhashes_batch = []

            # try with protos
            protocols = self.synchronizer.protocols
            if not protocols:
                log.warn('no protocols available')
                return self.exit(success=False)

            for proto in protocols:
                assert proto not in self.requests
                assert not proto.is_stopped

                # request
                assert proto not in self.requests
                deferred = AsyncResult()
                self.requests[proto] = deferred
                proto.send_getblockhashes(blockhash, max_blockhashes_per_request)
                try:
                    blockhashes_batch = deferred.get(block=True,
                                                     timeout=self.blockhashes_request_timeout)
                except gevent.Timeout:
                    log.warn('syncing timed out')
                    continue
                finally:
                    # is also executed 'on the way out' when any other clause of the try statement
                    # is left via a break, continue or return statement.
                    del self.requests[proto]

                if not blockhashes_batch:
                    log.warn('empty getblockhashes result')
                    continue
                break

            for blockhash in blockhashes_batch:  # youngest to oldest
                assert isinstance(blockhash, str)
                if blockhash not in self.chain:
                    blockhashes_chain.append(blockhash)
                else:
                    log.debug('found known blockhash', blockhash=blockhash.encode('hex'),
                              is_genesis=bool(blockhash == self.chain.genesis.hash))
                    break
            max_blockhashes_per_request = self.max_blockhashes_per_request

        self.fetch_blocks(blockhashes_chain)

    def fetch_blocks(self, blockhashes_chain):
        # fetch blocks (no parallelism here)
        log.debug('fetching blocks', num=len(blockhashes_chain))
        assert blockhashes_chain
        blockhashes_chain.reverse()  # oldest to youngest
        num_blocks = len(blockhashes_chain)
        num_fetched = 0

        while blockhashes_chain:
            blockhashes_batch = blockhashes_chain[:self.max_blocks_per_request]
            t_blocks = []

            # try with protos
            protocols = self.synchronizer.protocols
            if not protocols:
                log.warn('no protocols available')
                return self.exit(success=False)

            for proto in protocols:
                assert proto not in self.requests
                assert not proto.is_stopped
                # request
                log.debug('requesting blocks', num=len(blockhashes_batch))
                deferred = AsyncResult()
                self.requests[proto] = deferred
                proto.send_getblocks(*blockhashes_batch)
                try:
                    t_blocks = deferred.get(block=True, timeout=self.blocks_request_timeout)
                except gevent.Timeout:
                    log.warn('getblocks timed out, trying next proto')
                    continue
                finally:
                    del self.requests[proto]
                if not t_blocks:
                    log.warn('empty getblocks reply, trying next proto')
                    continue
                elif not isinstance(t_blocks[0], TransientBlock):
                    log.warn('received unecpected data', data=repr(t_blocks))
                    continue
                # we have results
                if not [b.header.hash for b in t_blocks] == blockhashes_batch[:len(t_blocks)]:
                    log.warn('received wrong blocks, should ban peer')
                    continue
                break

            # add received t_blocks
            num_fetched += len(t_blocks)
            log.debug('received blocks', num=len(t_blocks), num_fetched=num_fetched,
                      total=num_blocks, missing=num_blocks - num_fetched)

            if not t_blocks:
                log.warn('failed to fetch blocks', missing=len(blockhashes_chain))
                return self.exit(success=False)

            ts = time.time()
            log.debug('adding blocks', qsize=self.chainservice.block_queue.qsize())
            for t_block in t_blocks:
                assert t_block.header.hash == blockhashes_chain.pop(0)
                self.chainservice.add_block(t_block, proto)  # this blocks if the queue is full
            log.debug('adding blocks done', took=time.time() - ts)

        # done
        last_block = t_block
        assert not len(blockhashes_chain)
        assert last_block.header.hash == self.blockhash
        log.debug('syncing finished')
        # at this point blocks are not in the chain yet, but in the add_block queue
        if self.chain_difficulty >= self.chain.head.chain_difficulty():
            self.chainservice.broadcast_newblock(last_block, self.chain_difficulty, origin=proto)

        self.exit(success=True)

    def receive_blocks(self, proto, t_blocks):
        log.debug('blocks received', proto=proto, num=len(t_blocks))
        if proto not in self.requests:
            log.debug('unexpected blocks')
            return
        self.requests[proto].set(t_blocks)

    def receive_blockhashes(self, proto, blockhashes):
        log.debug('blockhashes received', proto=proto, num=len(blockhashes))
        if proto not in self.requests:
            log.debug('unexpected blockashes')
            return
        self.requests[proto].set(blockhashes)


log = get_logger('eth.sync')


class Synchronizer(object):

    """
    handles the synchronization of blocks

    there is only one synctask active at a time
    in order to deal with the worst case of initially syncing the wrong chain,
        a checkpoint blockhash can be specified and synced via force_sync

    received blocks are given to chainservice.add_block
    which has a fixed size queue, the synchronization blocks if the queue is full

    on_status:
        if peer.head.chain_difficulty > chain.head.chain_difficulty
            fetch peer.head and handle as newblock
    on_newblock:
        if block.parent:
            add
        else:
            sync
    on_blocks/on_blockhashes:
        if synctask:
            handle to requester
        elif unknown and has parent:
            add to chain
        else:
            drop
    """

    def __init__(self, chainservice, force_sync=None):
        """
        @param: force_sync None or tuple(blockhash, chain_difficulty)
                helper for long initial syncs to get on the right chain
                used with first status_received
        """
        self.chainservice = chainservice
        self.force_sync = force_sync
        self.chain = chainservice.chain
        self._protocols = dict()  # proto: chain_difficulty
        self.synctask = None

    def synctask_exited(self, success=False):
        # note: synctask broadcasts best block
        if success:
            self.force_sync = None
        self.synctask = None

    @property
    def protocols(self):
        "return protocols which are not stopped sorted by highest chain_difficulty"
        # filter and cleanup
        self._protocols = dict((p, cd) for p, cd in self._protocols.items() if not p.is_stopped)
        return sorted(self._protocols.keys(), key=lambda p: self._protocols[p], reverse=True)

    def receive_newblock(self, proto, t_block, chain_difficulty):
        "called if there's a newblock announced on the network"
        log.debug('newblock', proto=proto, block=t_block, chain_difficulty=chain_difficulty,
                  client=proto.peer.remote_client_version)

        # memorize proto with difficulty
        self._protocols[proto] = chain_difficulty

        if self.chainservice.knows_block(block_hash=t_block.header.hash):
            log.debug('known block')
            return

        # check pow
        if not t_block.header.check_pow():
            log.warn('check pow failed, should ban!')
            return

        expected_difficulty = self.chain.head.chain_difficulty() + t_block.header.difficulty
        if chain_difficulty >= self.chain.head.chain_difficulty():
            # broadcast duplicates filtering is done in eth_service
            log.debug('sufficient difficulty, broadcasting',
                      client=proto.peer.remote_client_version, chain_difficulty=chain_difficulty,
                      expected_difficulty=expected_difficulty)
            self.chainservice.broadcast_newblock(t_block, chain_difficulty, origin=proto)
        else:
            # any criteria for which blocks/chains not to add?
            log.debug('insufficient difficulty, dropping', client=proto.peer.remote_client_version,
                      chain_difficulty=chain_difficulty, expected_difficulty=expected_difficulty)
            return

        # unknown and pow check and highest difficulty

        # check if we have parent
        if self.chainservice.knows_block(block_hash=t_block.header.prevhash):
            log.debug('adding block')
            self.chainservice.add_block(t_block, proto)
        else:
            log.debug('missing parent')
            if not self.synctask:
                self.synctask = SyncTask(self, proto, t_block.header.hash, chain_difficulty)
            else:
                log.debug('existing task, discarding')

    def receive_status(self, proto, blockhash, chain_difficulty):
        "called if a new peer is connected"
        log.debug('status received', proto=proto, chain_difficulty=chain_difficulty)

        # memorize proto with difficulty
        self._protocols[proto] = chain_difficulty

        if self.force_sync and not self.synctask:
            blockhash, chain_difficulty = self.force_sync
            log.debug('starting forced syctask', blockhash=blockhash.encode('hex'))
            self.synctask = SyncTask(self, proto, blockhash, chain_difficulty)

        elif chain_difficulty > self.chain.head.chain_difficulty():
            log.debug('sufficient difficulty')
            if blockhash not in self.chain and not self.synctask:
                self.synctask = SyncTask(self, proto, blockhash, chain_difficulty)
            else:
                log.debug('existing task, discarding')

    def receive_blocks(self, proto, t_blocks):
        log.debug('blocks received', proto=proto, num=len(t_blocks))
        if self.synctask:
            self.synctask.receive_blocks(proto, t_blocks)
        else:
            log.warn('no synctask, not expecting blocks')

    def receive_blockhashes(self, proto, blockhashes):
        log.debug('blockhashes received', proto=proto, num=len(blockhashes))
        if self.synctask:
            self.synctask.receive_blockhashes(proto, blockhashes)
        else:
            log.warn('no synctask, not expecting blockhashes')
