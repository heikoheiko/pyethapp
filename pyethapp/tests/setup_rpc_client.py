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
import sys
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol
from tinyrpc.transports.http import HttpPostClientTransport
from tinyrpc import RPCClient
from pyethapp.jsonrpc import quantity_encoder

try:
    client = sys.argv[1]
except KeyError:
    client = 'python'
port = {
    'python': 8545,
    'go': 8545
}[client]
print 'client:', client, 'port:', port

protocol = JSONRPCProtocol()
transport = HttpPostClientTransport('http://127.0.0.1:{}'.format(port))


def call(method, *args, **kwargs):
    print_comm = kwargs.pop('print_comm', True)
    request = protocol.create_request(method, args, kwargs)
    reply = transport.send_message(request.serialize())
    if print_comm:
        print "Request:"
        print json.dumps(json.loads(request.serialize()), indent=2)
        print "Reply:"
        print reply
    return protocol.parse_reply(reply).result


def find_block(condition):
    """Query all blocks one by one and return the first one for which
    `condition(block)` evaluates to `True`.
    """
    i = 0
    while True:
        block = call('eth_getBlockByNumber', quantity_encoder(i), True, print_comm=False)
        if condition(block):
            return block
        i += 1
