from devp2p.service import BaseService
from ethereum.slogging import get_logger
from ethereum.utils import privtopub  # this is different  than the one used in devp2p.crypto
from ethereum.utils import privtoaddr
from ethereum.utils import sha3
import random
log = get_logger('accounts')


def mk_privkey(seed):
    return sha3(seed)


def mk_random_privkey():
    k = hex(random.getrandbits(256))[2:-1].zfill(64)
    assert len(k) == 64
    return k.decode('hex')


class Account(object):

    def __init__(self, privkey):
        self.privkey = privkey

    @property
    def pubkey(self):
        return privtopub(self.privkey)

    @property
    def address(self):
        a = privtoaddr(self.privkey)
        assert len(a) == 20
        assert isinstance(a, bytes)
        return a

    def __repr__(self):
        return '<Account(%s)>' % self.address.encode('hex')


class AccountsService(BaseService):

    name = 'accounts'
    default_config = dict(accounts=dict(privkeys_hex=[]))

    def __init__(self, app):
        super(AccountsService, self).__init__(app)
        self.accounts = [Account(pk.decode('hex'))
                         for pk in app.config['accounts']['privkeys_hex']]
        if not self.accounts:
            log.warn('no accounts configured')
        else:
            log.info('found account(s)', coinbase=self.coinbase.encode('hex'),
                     accounts=self.accounts)

    @property
    def coinbase(self):
        return self.accounts[0].address

    def sign_tx(self, sender, tx):
        # should be moved to Account where individual rules can be implemented
        assert sender in self
        a = self[sender]
        log.info('signing tx', tx=tx, account=a)
        tx.sign(a.privkey)

    def __contains__(self, address):
        assert len(address) == 20
        return address in [a.address for a in self.accounts]

    def __getitem__(self, address):
        assert len(address) == 20
        for a in self.accounts:
            if a.address == address:
                return a
        raise KeyError
