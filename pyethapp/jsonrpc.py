from decorator import decorator
import inspect
import gevent
import gevent.wsgi
import gevent.queue
from gevent.event import Event
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol
from tinyrpc.transports.wsgi import WsgiServerTransport
from tinyrpc.server.gevent import RPCServerGreenlets
from tinyrpc.dispatch import RPCDispatcher, public
import pyethereum.utils
import pyethereum.slogging as slogging
from devp2p.service import BaseService

log = slogging.get_logger('jsonrpc')
slogging.configure(config_string=':debug')


class JSONRPCServer(BaseService):
    """Service providing a JSON RPC server."""

    name = 'jsonrpc'

    def __init__(self, app):
        log.debug('initializing JSONRPCServer')
        BaseService.__init__(self, app)
        self.app = app
        self.dispatcher = RPCDispatcher()
        for subdispatcher in (Web3, Net, Compilers):
            self.dispatcher.register_instance(subdispatcher(), subdispatcher.prefix)
        transport = WsgiServerTransport(queue_class=gevent.queue.Queue)

        # start wsgi server as a background-greenlet
        self.wsgi_server = gevent.wsgi.WSGIServer(('127.0.0.1', 5000), transport.handle)

        self.rpc_server = RPCServerGreenlets(
            transport,
            JSONRPCProtocol(),
            self.dispatcher
        )

    def _run(self):
        log.info('starting JSONRPCServer')
        # in the main greenlet, run our rpc_server
        self.wsgi_thread = gevent.spawn(self.wsgi_server.serve_forever)
        self.rpc_server.serve_forever()

    def stop(self):
        log.info('stopping JSONRPCServer')
        self.wsgi_thread.kill()


def hex_decoder(data):
    """Decode `data` from hex with `0x` prefix.
    
    :raises: :exc:`ValueErro` if `data` is not properly encoded.
    """
    if not data.startswith('0x'):
        success = False
    else:
        try:
            return data[2:].decode('hex')
        except TypeError:
            success = False
    assert not success
    raise ValueError('Invalid hex encoding')


def hex_encoder(data):
    """Encode `data` in hex with `0x` prefix."""
    return '0x' + data.encode('hex')


def decode_arg(name, decoder):
    """Create a decorator that applies `decoder` to argument `name`."""
    @decorator
    def new_f(f, *args, **kwargs):
        call_args = inspect.getcallargs(f, *args, **kwargs)
        call_args[name] = decoder(call_args[name])
        return f(**call_args)
    return new_f


def encode_res(encoder):
    """Create a decorator that applies `encoder` to the return value of the
    decorated function.
    """
    @decorator
    def new_f(f, *args, **kwargs):
        res = f(*args, **kwargs)
        return encoder(res)
    return new_f


class Web3(object):

    prefix = 'web3_'

    @public
    @decode_arg('data', hex_decoder)
    @encode_res(hex_encoder)
    def sha3(self, data):
        return pyethereum.utils.sha3(data)

    @public
    def clientVersion(self):
        raise NotImplementedError("RPC method not implemented yet")


class Net(object):

    prefix = 'net_'

    @public
    def version(self):
        raise NotImplementedError("RPC method not implemented yet")

    @public
    def listening(self):
        raise NotImplementedError("RPC method not implemented yet")

    @public
    @encode_res(hex_encoder)
    def peerCount(self):
        raise NotImplementedError("RPC method not implemented yet")


class Compilers(object):
    """Subdispatcher for compiler related RPC methods."""

    prefix = 'eth_'
    potentially_available = ['serpent', 'solidity']

    def __init__(self):
        self.compilers_ = None

    @property
    def compilers(self):
        if self.compilers_ is None:
            self.compilers_ = {}
            try:
                import serpent
                self.compilers_['serpent'] = serpent.compile
                self.compilers_['lll'] = serpent.compile_lll
            except ImportError:
                pass
            try:
                import solidity
                self.compilers_['solidity'] = solidity.compile
            except ImportError:
                pass
        return self.compilers_

    @public
    def getCompilers(self):
        return self.compilers.keys()

    @public
    @encode_res(hex_encoder)
    def compileSolidity(self, code):
        try:
            return self.compilers['solidity'](code)
        except KeyError:
            raise TypeError('Compiler not found')

    @public
    @encode_res(hex_encoder)
    def compileSerpent(self, code):
        try:
            return self.compilers['serpent'](code)
        except KeyError:
            raise TypeError('Compiler not found')

    @public
    @encode_res(hex_encoder)
    def compileLLL(self, code):
        try:
            return self.compilers['lll'](code)
        except KeyError:
            raise TypeError('Compiler not found')
