from decorator import decorator
import inspect
import gevent
import gevent.wsgi
import gevent.queue
from tinyrpc.dispatch import RPCDispatcher
from tinyrpc.dispatch import public as public_
from tinyrpc.exc import BadRequestError, MethodNotFoundError
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol, JSONRPCInvalidParamsError
from tinyrpc.server.gevent import RPCServerGreenlets
from tinyrpc.transports.wsgi import WsgiServerTransport
from ethereum.utils import is_numeric, is_string, int_to_big_endian, encode_hex, decode_hex
import ethereum.slogging as slogging
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


class LoggingDispatcher(RPCDispatcher):

    """A dispatcher that logs every RPC method call."""

    def __init__(self):
        super(LoggingDispatcher, self).__init__()
        self.logger = log.debug

    def dispatch(self, request):
        self.logger('RPC call', method=request.method, args=request.args,
                    kwargs=request.kwargs, id=request.unique_id)
        res = super(LoggingDispatcher, self).dispatch(request)
        try:
            self.logger('RPC result', id=res.unique_id, result=res.result)
        except AttributeError:
            self.logger('RPC error', id=res.unique_id, error=res.error)
        return res


class JSONRPCServer(BaseService):

    """Service providing an HTTP server with JSON RPC interface.

    Other services can extend the JSON RPC interface by creating a
    :class:`Subdispatcher` and registering it via
    `Subdispatcher.register(self.app.services.json_rpc_server)`.

    Alternatively :attr:`dispatcher` can be extended directly (see
    https://tinyrpc.readthedocs.org/en/latest/dispatch.html).
    """

    name = 'jsonrpc'

    def __init__(self, app):
        log.debug('initializing JSONRPCServer')
        BaseService.__init__(self, app)
        self.app = app

        self.dispatcher = LoggingDispatcher()
        # register sub dispatchers
        for subdispatcher in (Web3, Net, Compilers, DB, Chain):
            subdispatcher.register(self)

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


class Subdispatcher(object):

    """A JSON RPC subdispatcher which can be registered at JSONRPCService.

    :cvar prefix: common prefix shared by all rpc methods implemented by this
                  subdispatcher
    :cvar required_services: a list of names of services the subdispatcher
                             is built on and will be made available as
                             instance variables
    """

    prefix = ''
    required_services = []

    @classmethod
    def register(cls, json_rpc_service):
        """Register a new instance at ``json_rpc_service.dispatcher``.

        If one of the required services is not available, log this as warning
        but don't fail.
        """
        dispatcher = cls()
        for service_name in cls.required_services:
            try:
                service = json_rpc_service.app.services[service_name]
            except KeyError:
                log.warning('No {} registered. Some RPC methods will not be '
                            'available'.format(service_name))
                return
            setattr(dispatcher, service_name, service)
        json_rpc_service.dispatcher.register_instance(dispatcher, cls.prefix)


def quantity_decoder(data):
    """Decode `data` representing a quantity."""
    if not is_string(data):
        success = False
    elif not data.startswith('0x'):
        success = False  # must start with 0x prefix
    elif len(data) > 3 and data[2] == '0':
        success = False  # must not have leading zeros (except `0x0`)
    else:
        data = data[2:]
        # ensure even length
        if len(data) % 2 == 1:
            data = '0' + data
        try:
            return int(data, 16)
        except TypeError:
            success = False
    assert not success
    raise BadRequestError('Invalid quantity encoding')


def data_decoder(data):
    """Decode `data` representing unformatted data."""
    if not data.startswith('0x'):
        success = False  # must start with 0x prefix
    elif len(data) % 2 != 0:
        success = False  # must be even length
    else:
        try:
            # TODO: use py3 compatible version (e.g. rlp.utils.decode_hex)
            return data[2:].decode('hex')
        except TypeError:
            success = False
    assert not success
    raise BadRequestError('Invalid data encoding')


def data_encoder(data):
    """Encode unformatted binary `data`."""
    return '0x' + data.encode('hex')


def quantity_encoder(i):
    """Encode interger quantity `data`."""
    assert is_numeric(i)
    data = int_to_big_endian(i)
    return '0x' + (encode_hex(data).lstrip('0') or '0')


def address_decoder(data):
    """Decode an address from hex with 0x prefix to 20 bytes."""
    addr = data_decoder(data)
    if len(addr) != 20:
        raise BadRequestError('Addresses must be 40 bytes long')
    return addr


def block_id_decoder(data):
    """Decode a block identifier (either an integer or 'latest', 'earliest' or 'pending')."""
    if data in ('latest', 'earliest', 'pending'):
        return data
    else:
        return quantity_decoder(data)

def block_hash_decoder(data):
    """Decode a block hash."""
    decoded = data_decoder(data)
    if len(decoded) != 32:
        raise BadRequestError('Block hashes must be 32 bytes long')
    return decoded


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


class Web3(Subdispatcher):

    """Subdispatcher for some generic RPC methods."""

    prefix = 'web3_'

    @public
    @decode_arg('data', data_decoder)
    @encode_res(data_encoder)
    def sha3(self, data):
        return ethereum.utils.sha3(data)

    @public
    def clientVersion(self):
        raise MethodNotFoundError()


class Net(Subdispatcher):

    """Subdispatcher for network related RPC methods."""

    prefix = 'net_'
    required_services = ['peermanager']

    @public
    def version(self):
        raise MethodNotFoundError()

    @public
    def listening(self):
        raise MethodNotFoundError()

    @public
    @encode_res(quantity_encoder)
    def peerCount(self):
        return len(self.peermanager.peers)


class Compilers(Subdispatcher):

    """Subdispatcher for compiler related RPC methods."""

    prefix = 'eth_'
    required_services = []

    def __init__(self):
        super(Compilers, self).__init__()
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
    @encode_res(data_encoder)
    def compileSolidity(self, code):
        try:
            return self.compilers['solidity'](code)
        except KeyError:
            raise MethodNotFoundError()

    @public
    @encode_res(data_encoder)
    def compileSerpent(self, code):
        try:
            return self.compilers['serpent'](code)
        except KeyError:
            raise MethodNotFoundError()

    @public
    @encode_res(data_encoder)
    def compileLLL(self, code):
        try:
            return self.compilers['lll'](code)
        except KeyError:
            raise MethodNotFoundError()


class DB(Subdispatcher):

    """Subdispatcher providing database related RPC methods."""

    prefix = 'db_'
    required_services = ['db']

    @public
    def putString(self, db_name, key, value):
        self.db.put(db_name + key, value)
        return True

    @public
    def getString(self, db_name, key):
        try:
            return self.db.get(db_name + key)
        except KeyError:
            return ''

    @public
    @decode_arg('value', data_decoder)
    def putHex(self, db_name, key, value):
        self.db.put(db_name + key, value)
        return True

    @public
    @encode_res(data_encoder)
    def getHex(self, db_name, key):
        try:
            return self.db.get(db_name + key)
        except KeyError:
            return ''


class Chain(Subdispatcher):

    """Subdispatcher for methods to query the block chain."""

    prefix = 'eth_'
    required_services = ['chain']

    def get_block(self, block_id):
        """Return the block identified by `block_id`.

        :param block_id: either the block number as integer or 'pending',
                         'earliest' or 'latest'
        :raises: `ValueError` if the block does not exist
        """
        if block_id == 'pending':
            assert False  # TODO
        if block_id == 'latest':
            return self.chain.chain.head
        if block_id == 'earliest':
            return self.chain.chain.genesis
        try:
            if is_numeric(block_id):
                # by number
                hash_ = self.chain.chain.index.get_block_by_number(block_id)
            else:
                # by hash
                assert is_string(block_id)
                hash_ = block_id
            return self.chain.chain.get(hash_)
        except KeyError:
            raise BadRequestError('Unknown block')

    @public
    @encode_res(quantity_encoder)
    def blockNumber(self):
        return self.chain.chain.head.number

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getBalance(self, address, block_id):
        block = self.get_block(block_id)
        return block.get_balance(address)

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('index', quantity_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(data_encoder)
    def getStorageAt(self, address, index, block_id):
        block = self.get_block(block_id)
        i = block.get_storage_data(address, index)
        assert is_numeric(i)
        return int_to_big_endian(i)

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getTransactionCount(self, address, block_id):
        block = self.get_block(block_id)
        return block.get_nonce(address)

    @public
    @decode_arg('block_hash', block_hash_decoder)
    @encode_res(quantity_encoder)
    def getBlockTransactionCountByHash(self, block_hash):
        block = self.get_block(block_hash)
        return block.transaction_count

    @public
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getBlockTransactionCountByNumber(self, block_id):
        block = self.get_block(block_id)
        return block.transaction_count

    @public
    @decode_arg('block_hash', block_hash_decoder)
    @encode_res(quantity_encoder)
    def getUncleCountByBlockHash(self, block_hash):
        block = self.get_block(block_hash)
        return len(block.uncles)

    @public
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getUncleCountByBlockNumber(self, block_id):
        block = self.get_block(block_id)
        return len(block.uncles)

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(data_encoder)
    def getCode(self, block_id):
        block = self.get_block(block_id)
        return block.get_code()
