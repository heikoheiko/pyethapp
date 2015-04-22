import time
import gevent
import gipc
import random
from devp2p.service import BaseService
from devp2p.app import BaseApp
from ethereum import utils
from ethereum.slogging import get_logger
from repoze.lru import lru_cache
log = get_logger('pow')

import pyethash
mkcache = pyethash.mkcache_bytes
EPOCH_LENGTH = pyethash.EPOCH_LENGTH
get_cache_size = pyethash.get_cache_size
get_full_size = pyethash.get_full_size
hashimoto_light = lambda s, c, h, n: pyethash.hashimoto_light(s, c, h, utils.big_endian_to_int(n))


@lru_cache(5)
def get_cache_memoized(seedhash, size):
    return mkcache(size, seedhash)


def get_seed(block_number):
    seed = b'\x00' * 32
    for i in range(block_number // EPOCH_LENGTH):
        seed = utils.sha3(seed)
    return seed


def check_pow(self, mining_hash, nonce, mixhash, block_number, debugmode=False):
    """Check if the proof-of-work of the block is valid.

    :param nonce: if given the proof of work function will be evaluated
                  with this nonce instead of the one already present in
                  the header
    :returns: `True` or `False`
    """
    if len(mixhash) != 32 or len(nonce) != 8:
        raise ValueError("Bad mixhash or nonce length")

    seed = get_seed(block_number)
    current_cache_size = get_cache_size(block_number)
    cache = get_cache_memoized(seed, current_cache_size)
    current_full_size = get_full_size(self.number)
    mining_output = hashimoto_light(current_full_size, cache, mining_hash, nonce)
    diff = self.difficulty
    if mining_output['mix digest'] != mixhash:
        return False
    return utils.big_endian_to_int(mining_output['result']) <= 2**256 / (diff or 1)


def mine_cb(progress):
    "called any cb_steps, return 0 if it should continue"
    return 0


def mine(mining_hash, block_number, difficulty, cb=mine_cb, cb_steps=100):
    """
    emulate cethash api, which stops if callback returns not 0
    """
    log.debug('mining on new block', number=block_number)
    assert isinstance(block_number, int)
    sz = get_cache_size(block_number)
    cache = get_cache_memoized(get_seed(block_number), sz)
    fsz = get_full_size(block_number)
    start_nonce = nonce = random.randint(0, 2**256 - 1)
    TT64M1 = 2**64 - 1
    target = utils.zpad(utils.int_to_big_endian(2**256 // (difficulty or 1)), 32)
    while True:
        for i in range(cb_steps):
            znonce = utils.zpad(utils.int_to_big_endian((nonce + i) & TT64M1), 8)
            o = hashimoto_light(fsz, cache, mining_hash, znonce)
            if o["result"] <= target:
                mixhash = o["mix digest"]
                return mixhash
        if cb(nonce - start_nonce) != 0:
            break


class Miner(gevent.Greenlet):

    def __init__(self, mining_hash, block_number, difficulty, nonce_callback, cpu_pct=100):
        self.mining_hash = mining_hash
        self.block_number = block_number
        self.difficulty = difficulty
        self.nonce_callback = nonce_callback
        self.cpu_pct = cpu_pct
        self.last = time.time()
        self.is_stopped = False
        super(Miner, self).__init__()

    def _run(self):
        nonce = mine(self.mining_hash, self.block_number, self.difficulty, cb=self.callback)
        if nonce:
            self.nonce_callback(nonce)

    def callback(self, progress):
        elapsed = time.time() - self.last
        delay = elapsed * (1 - self.cpu_pct / 100.)
        gevent.sleep(delay + 0.001)
        if self.is_stopped:
            return -1
        return 0

    def stop(self):
        self.is_stopped = True


class PoWWorker(object):

    """
    communicates with the parent process using: tuple(str_cmd, dict_kargs)
    """

    def __init__(self, cpipe):
        self.cpipe = cpipe
        self.miner = None
        self.cpu_pct = 100

    def send_found_nonce(self, nonce):
        self.cpipe.put(('found_nonce', dict(nonce=nonce)))

    def recv_set_cpu_pct(self, cpu_pct):
        self.cpu_pct = max(0, min(100, cpu_pct))
        if self.miner:
            self.miner.cpu_pct = self.cpu_pct

    def recv_mine(self, mining_hash, block_number, difficulty):
        "restarts the miner"
        log.debug('received new new mining task')
        assert isinstance(block_number, int)
        if self.miner:
            self.miner.stop()
            self.miner.join()
        self.miner = Miner(mining_hash, block_number, difficulty, self.send_found_nonce,
                           self.cpu_pct)
        self.miner.start()

    def run(self):
        while True:
            cmd, kargs = self.cpipe.get()
            assert isinstance(kargs, dict)
            getattr(self, 'recv_' + cmd)(**kargs)


def powworker_process(cpipe):
    "entry point in forked sub processes, setup env"
    gevent.get_hub().SYSTEM_ERROR = BaseException  # stop on any exception
    PoWWorker(cpipe).run()


# parent process defined below ##############################################3

class PoWService(BaseService):

    name = 'pow'
    default_config = dict()

    def __init__(self, app):
        super(PoWService, self).__init__(app)
        self.cpipe, self.ppipe = gipc.pipe(duplex=True)
        self.worker_process = gipc.start_process(target=powworker_process, args=(self.cpipe, ))

    def mine_tasks(self):
        for i in range(10):
            gevent.sleep(1)
            self.ppipe.put(
                ('mine', dict(mining_hash=utils.sha3(str(i)), block_number=0, difficulty=10**i)))

    def recv_found_nonce(self, nonce):
        print 'nonce found %r' % nonce

    def _run(self):
        gevent.spawn(self.mine_tasks)  # dummy
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
