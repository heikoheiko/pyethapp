"""Provides a simple way of testing JSON RPC commands.

Intended usage:

    $ ipython -i pyethapp/tests/setup_rpc_client.py
    In [0]: call('web3_sha3', '0x')
    Request:
    {'id': 4,
     'jsonrpc': '2.0',
     'method': 'web3_sha3',
     'params': ['0x']}
    Reply:
    {'id': 4,
     'jsonrpc': '2.0',
     'result': '0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'}
    Out[0]: u'0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
"""
import json
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol
from tinyrpc.transports.http import HttpPostClientTransport
from pyethapp.jsonrpc import quantity_encoder, quantity_decoder
from pyethapp.jsonrpc import address_encoder, data_encoder, data_decoder
from pyethapp.jsonrpc import default_gasprice, default_startgas


z_address = '\x00' * 20


class JSONRPCClient(object):
    protocol = JSONRPCProtocol()

    def __init__(self, port=4000, print_communication=True):
        self.transport = HttpPostClientTransport('http://127.0.0.1:{}'.format(port))
        self.print_communication = print_communication

    def call(self, method, *args, **kwargs):
        request = self.protocol.create_request(method, args, kwargs)
        reply = self.transport.send_message(request.serialize())
        if self.print_communication:
            print "Request:"
            print json.dumps(json.loads(request.serialize()), indent=2)
            print "Reply:"
            print reply
        return self.protocol.parse_reply(reply).result

    __call__ = call

    def find_block(self, condition):
        """Query all blocks one by one and return the first one for which
        `condition(block)` evaluates to `True`.
        """
        i = 0
        while True:
            block = self.call('eth_getBlockByNumber', quantity_encoder(i), True, print_comm=False)
            if condition(block):
                return block
            i += 1

    def eth_send_Transaction(self, nonce=None, sender='', to='', value=0, data='',
                             gasprice=default_gasprice, startgas=default_startgas,
                             v=None, r=None, s=None):
        encoders = dict(nonce=quantity_encoder, sender=address_encoder, to=data_encoder,
                        value=quantity_encoder, gasprice=quantity_encoder,
                        startgas=quantity_encoder, data=data_encoder,
                        v=quantity_encoder, r=quantity_encoder, s=quantity_encoder)
        data = {k: encoders[k](v) for k, v in locals().items()
                if k not in ('self', 'encoders') and v is not None}
        data['from'] = data.pop('sender')
        assert data.get('from') or (v and r and s)

        res = self.call('eth_send_Transaction', data)
        return data_decoder(res)


def tx_example():
    """
    unsigned txs is signed on the server which needs to know
    the secret key associated with the sending account
    it can be added in the config
    """
    from pyethapp.accounts import mk_privkey, privtoaddr
    secret_seed = 'wow'
    sender = privtoaddr(mk_privkey(secret_seed))
    res = JSONRPCClient().eth_send_Transaction(sender=sender, to=z_address, value=1000)
    if len(res) == 20:
        print 'contract created @', res.encode('hex')
    else:
        assert len(res) == 32
        print 'tx hash', res.encode('hex')


def signed_tx_example():
    from ethereum.transactions import Transaction
    from pyethapp.accounts import mk_privkey, privtoaddr
    secret_seed = 'wow'
    privkey = mk_privkey(secret_seed)
    sender = privtoaddr(privkey)
    # fetch nonce
    nonce = quantity_decoder(
        JSONRPCClient().call('eth_getTransactionCount', address_encoder(sender), 'pending'))
    # create transaction
    tx = Transaction(nonce, default_gasprice, default_startgas, to=z_address, value=100, data='')
    tx.sign(privkey)
    tx_dict = tx.to_dict()
    tx_dict.pop('hash')
    res = JSONRPCClient().eth_send_Transaction(**tx_dict)
    if len(res) == 20:
        print 'contract created @', res.encode('hex')
    else:
        assert len(res) == 32
        print 'tx hash', res.encode('hex')


if __name__ == '__main__':
    call = JSONRPCClient()
    # signed_tx_example()
    # tx_example()
