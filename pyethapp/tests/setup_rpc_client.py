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
import ast
from pprint import pprint
import sys
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol
from tinyrpc.transports.http import HttpPostClientTransport
from tinyrpc import RPCClient

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
    request = protocol.create_request(method, args, kwargs)
    reply = transport.send_message(request.serialize())
    print "Request:"
    pprint(ast.literal_eval(request.serialize()), width=1)
    print "Reply:"
    pprint(ast.literal_eval(reply), width=1)
    return protocol.parse_reply(reply).result
