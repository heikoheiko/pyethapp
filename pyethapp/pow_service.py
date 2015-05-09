import time
import gevent
import gipc
import random
from devp2p.service import BaseService
from devp2p.app import BaseApp
from ethereum.ethpow import mine, TT64M1
from ethereum.slogging import get_logger
log = get_logger('pow')
log_sub = get_logger('pow.subprocess')


class Miner(gevent.Greenlet):

    rounds = 100
    max_elapsed = 1.

    def __init__(self, mining_hash, block_number, difficulty, nonce_callback,
                 hashrate_callback, cpu_pct=100):
        self.mining_hash = mining_hash
        self.block_number = block_number
        self.difficulty = difficulty
        self.nonce_callback = nonce_callback
        self.hashrate_callback = hashrate_callback
        self.cpu_pct = cpu_pct
        self.last = time.time()
        self.is_stopped = False
        super(Miner, self).__init__()

    def _run(self):
        nonce = random.randint(0, TT64M1)
        while not self.is_stopped:
            log_sub.debug('starting mining round')
            st = time.time()
            bin_nonce, mixhash = mine(self.block_number, self.difficulty, self.mining_hash,
                                      start_nonce=nonce, rounds=self.rounds)
            elapsed = time.time() - st
            if bin_nonce:
                log_sub.debug('nonce found')
                self.nonce_callback(bin_nonce, mixhash, self.mining_hash)
                break
            delay = elapsed * (1 - self.cpu_pct / 100.)
            hashrate = int(self.rounds // (elapsed + delay))
            self.hashrate_callback(hashrate)
            log_sub.debug('sleeping', delay=delay, elapsed=elapsed, rounds=self.rounds)
            gevent.sleep(delay + 0.001)
            nonce += self.rounds
            # adjust
            adjust = elapsed / self.max_elapsed
            self.rounds = int(self.rounds / adjust)

        log_sub.debug('mining task finished', is_stopped=self.is_stopped)

    def stop(self):
        self.is_stopped = True
        self.join()


class PoWWorker(object):

    """
    communicates with the parent process using: tuple(str_cmd, dict_kargs)
    """

    def __init__(self, cpipe, cpu_pct):
        self.cpipe = cpipe
        self.miner = None
        self.cpu_pct = cpu_pct

    def send_found_nonce(self, bin_nonce, mixhash, mining_hash):
        log_sub.debug('sending nonce')
        self.cpipe.put(('found_nonce', dict(bin_nonce=bin_nonce, mixhash=mixhash,
                                            mining_hash=mining_hash)))

    def send_hashrate(self, hashrate):
        log_sub.debug('sending hashrate')
        self.cpipe.put(('hashrate', dict(hashrate=hashrate)))

    def recv_set_cpu_pct(self, cpu_pct):
        self.cpu_pct = max(0, min(100, cpu_pct))
        if self.miner:
            self.miner.cpu_pct = self.cpu_pct

    def recv_mine(self, mining_hash, block_number, difficulty):
        "restarts the miner"
        log_sub.debug('received new new mining task')
        assert isinstance(block_number, int)
        if self.miner:
            self.miner.stop()
        self.miner = Miner(mining_hash, block_number, difficulty, self.send_found_nonce,
                           self.send_hashrate, self.cpu_pct)
        self.miner.start()

    def run(self):
        while True:
            cmd, kargs = self.cpipe.get()
            assert isinstance(kargs, dict)
            getattr(self, 'recv_' + cmd)(**kargs)


def powworker_process(cpipe, cpu_pct):
    "entry point in forked sub processes, setup env"
    gevent.get_hub().SYSTEM_ERROR = BaseException  # stop on any exception
    PoWWorker(cpipe, cpu_pct).run()


# parent process defined below ##############################################3

class PoWService(BaseService):

    name = 'pow'
    default_config = dict(pow=dict(activated=False, cpu_pct=100))

    def __init__(self, app):
        super(PoWService, self).__init__(app)
        cpu_pct = self.app.config['pow']['cpu_pct']
        self.cpipe, self.ppipe = gipc.pipe(duplex=True)
        self.worker_process = gipc.start_process(
            target=powworker_process, args=(self.cpipe, cpu_pct))
        self.app.services.chain.on_new_head_candidate_cbs.append(self.on_new_head_candidate)
        self.hashrate = 0

    @property
    def active(self):
        return self.app.config['pow']['activated']

    def on_new_head_candidate(self, block):
        log.debug('new head candidate', block_number=block.number,
                  mining_hash=block.mining_hash.encode('hex'), activated=self.active)
        if self.active and not self.app.services.chain.is_syncing:
            self.ppipe.put(('mine', dict(mining_hash=block.mining_hash,
                                         block_number=block.number, difficulty=block.difficulty)))

    def recv_hashrate(self, hashrate):
        log.debug('hashrate updated', hashrate=hashrate)
        self.hashrate = hashrate

    def recv_found_nonce(self, bin_nonce, mixhash, mining_hash):
        log.debug('nonce found', mining_hash=mining_hash.encode('hex'))
        blk = app.services.chain.chain.head_candidate
        if blk.mining_hash != mining_hash:
            log.debug('mining_hash does not match')
            return
        blk.mixhash = mixhash
        blk.nonce = bin_nonce
        app.services.chain.add_mined_block(blk)

    def _run(self):
        self.on_new_head_candidate(self.app.services.chain.chain.head_candidate)
        while True:
            cmd, kargs = self.ppipe.get()
            assert isinstance(kargs, dict)
            getattr(self, 'recv_' + cmd)(**kargs)

    def stop(self):
        self.worker_process.terminate()
        self.worker_process.join()
        super(PoWService, self).stop()

if __name__ == "__main__":
    app = BaseApp()
    PoWService.register_with_app(app)
    app.start()
