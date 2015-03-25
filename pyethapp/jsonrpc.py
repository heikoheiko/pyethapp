from decorator import decorator
import inspect
import gevent
import gevent.wsgi
import gevent.queue
from gevent.event import Event
from tinyrpc.dispatch import RPCDispatcher
from tinyrpc.dispatch import public as public_
from tinyrpc.exc import BadRequestError, MethodNotFoundError
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol, JSONRPCInvalidParamsError
from tinyrpc.server.gevent import RPCServerGreenlets
from tinyrpc.transports.wsgi import WsgiServerTransport
import pyethereum.utils
import pyethereum.slogging as slogging
from devp2p.service import BaseService

log = slogging.get_logger('jsonrpc')
slogging.configure(config_string=':debug')


# hack to return the correct json rpc error code if the param count is wrong
# (see https://github.com/mbr/tinyrpc/issues/19)
def public(f):
    def new_f(*args, **kwargs):
        try:
            inspect.getcallargs(f, *args, **kwargs)
        except TypeError:
            raise JSONRPCInvalidParamsError()
        else:
            return f(*args, **kwargs)
    new_f.func_name = f.func_name
    new_f.func_doc = f.func_doc
    return public_(new_f)


class JSONRPCServer(BaseService):
    """Service providing a JSON RPC server."""

    name = 'jsonrpc'

    def __init__(self, app):
        log.debug('initializing JSONRPCServer')
        BaseService.__init__(self, app)
        self.app = app

        self.dispatcher = RPCDispatcher()
        # register sub dispatchers
        self.dispatcher.register_instance(Web3(), Web3.prefix)
        self.dispatcher.register_instance(Net(self.app.services.peermanager), Net.prefix)
        self.dispatcher.register_instance(Compilers(), Compilers.prefix)

        transport = WsgiServerTransport(queue_class=gevent.queue.Queue)
        # start wsgi server as a background-greenlet
        self.port = app.config['jsonrpc']['port']
        self.wsgi_server = gevent.wsgi.WSGIServer(('127.0.0.1', self.port), transport.handle)
        self.rpc_server = RPCServerGreenlets(
            transport,
            JSONRPCProtocol(),
            self.dispatcher
        )

    def _run(self):
        log.info('starting JSONRPCServer', port=self.port)
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
    raise BadRequestError('Invalid hex encoding')


def hex_encoder(data):
    """Encode binary or numerical `data` in hex with `0x` prefix."""
    if pyethereum.utils.is_numeric(data):
        data = pyethereum.utils.int_to_big_endian(data)
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
        #if len(data) == 0:
        #    raise BadRequestError('Data must not be empty')
        return pyethereum.utils.sha3(data)

    @public
    def clientVersion(self):
        raise MethodNotFoundError()


class Net(object):

    prefix = 'net_'

    def __init__(self, peermanager):
        self.peermanager = peermanager

    @public
    def version(self):
        raise MethodNotFoundError()

    @public
    def listening(self):
        raise MethodNotFoundError()

    @public
    @encode_res(hex_encoder)
    def peerCount(self):
        return len(self.peermanager.peers)


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
            raise MethodNotFoundError()

    @public
    @encode_res(hex_encoder)
    def compileSerpent(self, code):
        try:
            return self.compilers['serpent'](code)
        except KeyError:
            raise MethodNotFoundError()

    @public
    @encode_res(hex_encoder)
    def compileLLL(self, code):
        try:
            return self.compilers['lll'](code)
        except KeyError:
            raise MethodNotFoundError()
