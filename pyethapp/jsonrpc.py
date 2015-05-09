import time
from copy import deepcopy
from decorator import decorator
from collections import Iterable
import inspect
import ethereum.blocks
from ethereum.utils import (is_numeric, is_string, int_to_big_endian, big_endian_to_int,
                            encode_hex, decode_hex, sha3, zpad)
import ethereum.slogging as slogging
from ethereum.slogging import LogRecorder
from ethereum.transactions import Transaction
from ethereum import processblock
import gevent
import gevent.wsgi
import gevent.queue
import rlp
from tinyrpc.dispatch import RPCDispatcher
from tinyrpc.dispatch import public as public_
from tinyrpc.exc import BadRequestError, MethodNotFoundError
from tinyrpc.protocols.jsonrpc import JSONRPCProtocol, JSONRPCInvalidParamsError
from tinyrpc.server.gevent import RPCServerGreenlets
from tinyrpc.transports.wsgi import WsgiServerTransport
from devp2p.service import BaseService
from eth_protocol import ETHProtocol
from ethereum.utils import denoms

logger = log = slogging.get_logger('jsonrpc')
slogging.configure(config_string=':debug')

# defaults
default_startgas = 100 * 1000
default_gasprice = 100 * 1000 * denoms.wei


def _fail_on_error_dispatch(self, request):
    method = self.get_method(request.method)
    # we found the method
    result = method(*request.args, **request.kwargs)
    return request.respond(result)
RPCDispatcher._dispatch = _fail_on_error_dispatch


# route logging messages


class WSGIServerLogger(object):

    _log = slogging.get_logger('jsonrpc.wsgi')

    @classmethod
    def log(cls, msg):
        cls._log.debug(msg.strip())
    write = log

    @classmethod
    def log_error(cls, msg, *args):
        cls._log.error(msg % args)

gevent.wsgi.WSGIHandler.log_error = WSGIServerLogger.log_error


# hack to return the correct json rpc error code if the param count is wrong
# (see https://github.com/mbr/tinyrpc/issues/19)

public_methods = dict()


def public(f):
    public_methods[f.__name__] = inspect.getargspec(f)

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
        if isinstance(request, Iterable):
            request_list = request
        else:
            request_list = [request]
        for req in request_list:
            self.logger('RPC call', method=req.method, args=req.args, kwargs=req.kwargs,
                        id=req.unique_id)
        response = super(LoggingDispatcher, self).dispatch(request)
        if isinstance(response, Iterable):
            response_list = response
        else:
            response_list = [response]
        for res in response_list:
            if hasattr(res, 'result'):
                self.logger('RPC result', id=res.unique_id, result=res.result)
            else:
                self.logger('RPC error', id=res.unique_id, error=res.error)
        return response


class JSONRPCServer(BaseService):

    """Service providing an HTTP server with JSON RPC interface.

    Other services can extend the JSON RPC interface by creating a
    :class:`Subdispatcher` and registering it via
    `Subdispatcher.register(self.app.services.json_rpc_server)`.

    Alternatively :attr:`dispatcher` can be extended directly (see
    https://tinyrpc.readthedocs.org/en/latest/dispatch.html).
    """

    name = 'jsonrpc'
    default_config = dict(jsonrpc=dict(listen_port=4000, listen_host='127.0.0.1'))

    @classmethod
    def subdispatcher_classes(cls):
        return (Web3, Net, Compilers, DB, Chain, Miner, FilterManager)

    def __init__(self, app):
        log.debug('initializing JSONRPCServer')
        BaseService.__init__(self, app)
        self.app = app

        self.dispatcher = LoggingDispatcher()
        # register sub dispatchers
        for subdispatcher in self.subdispatcher_classes():
            subdispatcher.register(self)

        transport = WsgiServerTransport(queue_class=gevent.queue.Queue)
        # start wsgi server as a background-greenlet
        self.listen_port = app.config['jsonrpc']['listen_port']
        self.listen_host = app.config['jsonrpc']['listen_host']
        self.wsgi_server = gevent.wsgi.WSGIServer((self.listen_host, self.listen_port),
                                                  transport.handle, log=WSGIServerLogger)
        self.rpc_server = RPCServerGreenlets(
            transport,
            JSONRPCProtocol(),
            self.dispatcher
        )
        self.default_block = 'latest'

    def _run(self):
        log.info('starting JSONRPCServer', port=self.listen_port)
        # in the main greenlet, run our rpc_server
        self.wsgi_thread = gevent.spawn(self.wsgi_server.serve_forever)
        self.rpc_server.serve_forever()

    def stop(self):
        log.info('stopping JSONRPCServer')
        self.wsgi_thread.kill()

    def get_block(self, block_id=None):
        """Return the block identified by `block_id`.

        This method also sets :attr:`default_block` to the value of `block_id`
        which will be returned if, at later calls, `block_id` is not provided.

        Subdispatchers using this function have to ensure sure that a
        chainmanager is registered via :attr:`required_services`.

        :param block_id: either the block number as integer or 'pending',
                        'earliest' or 'latest', or `None` for the default
                        block
        :returns: the requested block
        :raises: :exc:`KeyError` if the block does not exist
        """
        assert 'chain' in self.app.services
        chain = self.app.services.chain.chain
        if block_id is None:
            block_id = self.default_block
        else:
            self.default_block = block_id
        if block_id == 'pending':
            return self.app.services.chain.chain.head_candidate
        if block_id == 'latest':
            return chain.head
        if block_id == 'earliest':
            block_id = 0
        if is_numeric(block_id):
            # by number
            hash_ = chain.index.get_block_by_number(block_id)
        else:
            # by hash
            assert is_string(block_id)
            hash_ = block_id
        return chain.get(hash_)


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

        The subdispatcher will be able to access all required services as well
        as the app object as attributes.

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
        dispatcher.app = json_rpc_service.app
        dispatcher.json_rpc_server = json_rpc_service
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
        except ValueError:
            success = False
    assert not success
    raise BadRequestError('Invalid quantity encoding')


def quantity_encoder(i):
    """Encode interger quantity `data`."""
    assert is_numeric(i)
    data = int_to_big_endian(i)
    return '0x' + (encode_hex(data).lstrip('0') or '0')


def data_decoder(data):
    """Decode `data` representing unformatted data."""
    if not data.startswith('0x'):
        data = '0x' + data
    if len(data) % 2 != 0:
        raise BadRequestError('Invalid data encoding, must be even length')
    try:
        return decode_hex(data[2:])
    except TypeError:
        raise BadRequestError('Invalid data hex encoding', data[2:])


def data_encoder(data, length=None):
    """Encode unformatted binary `data`.

    If `length` is given, the result will be padded like this: ``quantity_encoder(255, 3) ==
    '0x0000ff'``.
    """
    s = encode_hex(data)
    if length is None:
        return '0x' + s
    else:
        return '0x' + s.rjust(length * 2, '0')


def address_decoder(data):
    """Decode an address from hex with 0x prefix to 20 bytes."""
    addr = data_decoder(data)
    if len(addr) not in (20, 0):
        raise BadRequestError('Addresses must be 20 or 0 bytes long')
    return addr


def address_encoder(address):
    assert len(address) in (20, 0)
    return '0x' + encode_hex(address)


def block_id_decoder(data):
    """Decode a block identifier as expected from :meth:`JSONRPCServer.get_block`."""
    if data in (None, 'latest', 'earliest', 'pending'):
        return data
    else:
        return quantity_decoder(data)


def block_hash_decoder(data):
    """Decode a block hash."""
    decoded = data_decoder(data)
    if len(decoded) != 32:
        raise BadRequestError('Block hashes must be 32 bytes long')
    return decoded


def tx_hash_decoder(data):
    """Decode a transaction hash."""
    decoded = data_decoder(data)
    if len(decoded) != 32:
        raise BadRequestError('Transaction hashes must be 32 bytes long')
    return decoded


def bool_decoder(data):
    if not isinstance(data, bool):
        raise BadRequestError('Parameter must be boolean')
    return data


def block_encoder(block, include_transactions=False, pending=False, is_header=False):
    """Encode a block as JSON object.

    :param block: a :class:`ethereum.blocks.Block`
    :param include_transactions: if true transactions are included, otherwise
                                 only their hashes
    :param pending: if `True` the block number of transactions, if included, is set to `None`
    :param is_header: if `True` the following attributes are ommitted: size, totalDifficulty,
                      transactions and uncles (thus, the function can be called with a block
                      header instead of a full block)
    :returns: a json encodable dictionary
    """
    assert not (include_transactions and is_header)
    d = {
        'number': quantity_encoder(block.number),
        'hash': data_encoder(block.hash),
        'parentHash': data_encoder(block.prevhash),
        'nonce': data_encoder(block.nonce),
        'sha3Uncles': data_encoder(block.uncles_hash),
        'logsBloom': data_encoder(int_to_big_endian(block.bloom), 256),
        'transactionsRoot': data_encoder(block.tx_list_root),
        'stateRoot': data_encoder(block.state_root),
        'difficulty': quantity_encoder(block.difficulty),
        'extraData': data_encoder(block.extra_data),
        'gasLimit': quantity_encoder(block.gas_limit),
        'gasUsed': quantity_encoder(block.gas_used),
        'timestamp': quantity_encoder(block.timestamp),
    }
    if not is_header:
        d['totalDifficulty'] = quantity_encoder(block.chain_difficulty())
        d['size'] = quantity_encoder(len(rlp.encode(block)))
        d['uncles'] = [data_encoder(u.hash) for u in block.uncles]
        if include_transactions:
            d['transactions'] = []
            for i, tx in enumerate(block.get_transactions()):
                d['transactions'].append(tx_encoder(tx, block, i, pending))
        else:
            d['transactions'] = [data_encoder(tx.hash) for tx in block.get_transactions()]
    if not pending:
        d['miner'] = data_encoder(block.coinbase)
        d['nonce'] = data_encoder(block.nonce)
    else:
        d['miner'] = '0x0000000000000000000000000000000000000000'
        d['nonce'] = '0x0000000000000000'
    return d


def tx_encoder(transaction, block, i, pending):
    """Encode a transaction as JSON object.

    `transaction` is the `i`th transaction in `block`. `pending` specifies if
    the block is pending or already mined.
    """
    return {
        'hash': data_encoder(transaction.hash),
        'nonce': quantity_encoder(transaction.nonce),
        'blockHash': data_encoder(block.hash),
        'blockNumber': quantity_encoder(block.number) if not pending else None,
        'transactionIndex': quantity_encoder(i),
        'from': data_encoder(transaction.sender),
        'to': data_encoder(transaction.to),
        'value': quantity_encoder(transaction.value),
        'gasPrice': quantity_encoder(transaction.gasprice),
        'gas': quantity_encoder(transaction.startgas),
        'input': data_encoder(transaction.data),
    }


def loglist_encoder(loglist):
    """Encode a list of log"""
    l = []
    if len(loglist) > 0 and loglist[0] is None:
        assert all(element is None for element in l)
        return l
    result = []
    for log, index, block in loglist:
        result.append({
            'logIndex': quantity_encoder(index),
            'transactionIndex': None,
            'transactionHash': None,
            'blockHash': data_encoder(block.hash),
            'blockNumber': quantity_encoder(block.number),
            'address': address_encoder(log.address),
            'data': data_encoder(log.data),
            'topics': [data_encoder(int_to_big_endian(topic), 32) for topic in log.topics]
        })
    return result


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
        return sha3(data)

    @public
    def clientVersion(self):
        return self.app.client_version


class Net(Subdispatcher):

    """Subdispatcher for network related RPC methods."""

    prefix = 'net_'
    required_services = ['discovery', 'peermanager']

    @public
    def version(self):
        return str(self.discovery.protocol.version)

    @public
    def listening(self):
        return self.peermanager.num_peers() < self.peermanager.config['p2p']['min_peers']

    @public
    @encode_res(quantity_encoder)
    def peerCount(self):
        return self.peermanager.num_peers()


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


class Miner(Subdispatcher):

    prefix = 'eth_'

    @public
    def mining(self):
        return self.app.services.pow.active

    @public
    @encode_res(quantity_encoder)
    def hashrate(self):
        return self.app.services.pow.hashrate

    @public
    @encode_res(address_encoder)
    def coinbase(self):
        return self.app.services.accounts.coinbase

    @public
    @encode_res(quantity_encoder)
    def gasPrice(self):
        return 1

    @public
    def accounts(self):
        return [address_encoder(account.address) for account in self.app.services.accounts]


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

    @public
    def protocolVersion(self):
        return str(ETHProtocol.version)

    @public
    @encode_res(quantity_encoder)
    def blockNumber(self):
        return self.chain.chain.head.number

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getBalance(self, address, block_id=None):
        block = self.json_rpc_server.get_block(block_id)
        return block.get_balance(address)

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('index', quantity_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(data_encoder)
    def getStorageAt(self, address, index, block_id=None):
        block = self.json_rpc_server.get_block(block_id)
        i = block.get_storage_data(address, index)
        assert is_numeric(i)
        return int_to_big_endian(i)

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(quantity_encoder)
    def getTransactionCount(self, address, block_id=None):
        block = self.json_rpc_server.get_block(block_id)
        return block.get_nonce(address)

    @public
    @decode_arg('block_hash', block_hash_decoder)
    def getBlockTransactionCountByHash(self, block_hash):
        try:
            block = self.json_rpc_server.get_block(block_hash)
        except KeyError:
            return None
        else:
            return quantity_encoder(block.transaction_count)

    @public
    @decode_arg('block_id', block_id_decoder)
    def getBlockTransactionCountByNumber(self, block_id):
        try:
            block = self.json_rpc_server.get_block(block_id)
        except KeyError:
            return None
        else:
            return quantity_encoder(block.transaction_count)

    @public
    @decode_arg('block_hash', block_hash_decoder)
    def getUncleCountByBlockHash(self, block_hash):
        try:
            block = self.json_rpc_server.get_block(block_hash)
        except KeyError:
            return None
        else:
            return quantity_encoder(len(block.uncles))

    @public
    @decode_arg('block_id', block_id_decoder)
    def getUncleCountByBlockNumber(self, block_id):
        try:
            block = self.json_rpc_server.get_block(block_id)
        except KeyError:
            return None
        else:
            return quantity_encoder(len(block.uncles))

    @public
    @decode_arg('address', address_decoder)
    @decode_arg('block_id', block_id_decoder)
    @encode_res(data_encoder)
    def getCode(self, address, block_id=None):
        block = self.json_rpc_server.get_block(block_id)
        return block.get_code(address)

    @public
    @decode_arg('block_hash', block_hash_decoder)
    @decode_arg('include_transactions', bool_decoder)
    def getBlockByHash(self, block_hash, include_transactions):
        try:
            block = self.json_rpc_server.get_block(block_hash)
        except KeyError:
            return None
        return block_encoder(block, include_transactions)

    @public
    @decode_arg('block_id', block_id_decoder)
    @decode_arg('include_transactions', bool_decoder)
    def getBlockByNumber(self, block_id, include_transactions):
        try:
            block = self.json_rpc_server.get_block(block_id)
        except KeyError:
            return None
        return block_encoder(block, include_transactions, pending=block_id == 'pending')

    @public
    @decode_arg('tx_hash', tx_hash_decoder)
    def getTransactionByHash(self, tx_hash):
        try:
            tx, block, index = self.chain.chain.index.get_transaction(tx_hash)
        except KeyError:
            return None
        return tx_encoder(tx, block, index, False)

    @public
    @decode_arg('block_hash', block_hash_decoder)
    @decode_arg('index', quantity_decoder)
    def getTransactionByBlockHashAndIndex(self, block_hash, index):
        block = self.json_rpc_server.get_block(block_hash)
        try:
            tx = block.get_transaction(index)
        except IndexError:
            return None
        return tx_encoder(tx, block, index, False)

    @public
    @decode_arg('block_id', block_id_decoder)
    @decode_arg('index', quantity_decoder)
    def getTransactionByBlockNumberAndIndex(self, block_id, index):
        block = self.json_rpc_server.get_block(block_id)
        try:
            tx = block.get_transaction(index)
        except IndexError:
            return None
        return tx_encoder(tx, block, index, block_id == 'pending')

    @public
    @decode_arg('block_hash', block_hash_decoder)
    @decode_arg('index', quantity_decoder)
    def getUncleByBlockHashAndIndex(self, block_hash, index):
        try:
            block = self.json_rpc_server.get_block(block_hash)
            uncle = block.uncles[index]
        except (IndexError, KeyError):
            return None
        return block_encoder(uncle, is_header=True)

    @public
    @decode_arg('block_number', quantity_decoder)
    @decode_arg('index', quantity_decoder)
    def getUncleByBlockNumberAndIndex(self, block_number, index):
        try:
            block = self.json_rpc_server.get_block(block_number)
            uncle = block.uncles[index]
        except (IndexError, KeyError):
            return None
        return block_encoder(uncle, is_header=True)

    @public
    def getWork(self):
        print 'Sending work...'
        h = self.chain.chain.head_candidate
        return [
            encode_hex(h.header.mining_hash),
            encode_hex(h.header.seed),
            encode_hex(zpad(int_to_big_endian(2**256 // h.header.difficulty), 32))
        ]

    @public
    def test(self, nonce):
        print 80808080808
        return nonce

    @public
    @decode_arg('nonce', data_decoder)
    @decode_arg('mining_hash', data_decoder)
    @decode_arg('mix_digest', data_decoder)
    def submitWork(self, nonce, mining_hash, mix_digest):
        print 'submitting work'
        h = self.chain.chain.head_candidate
        print 'header: %s' % encode_hex(rlp.encode(h))
        if h.header.mining_hash != mining_hash:
            return False
        print 'mining hash: %s' % encode_hex(mining_hash)
        print 'nonce: %s' % encode_hex(nonce)
        print 'mixhash: %s' % encode_hex(mix_digest)
        print 'seed: %s' % encode_hex(h.header.seed)
        h.header.nonce = nonce
        h.header.mixhash = mix_digest
        if not h.header.check_pow():
            print 'PoW check false'
            return False
        print 'PoW check true'
        self.chain.chain.add_block(h)
        self.chain.broadcast_newblock(h)
        print 'Added: %d' % h.header.number
        return True

    @public
    @encode_res(data_encoder)
    def coinbase(self):
        return self.app.services.accounts.coinbase

    @public
    def sendTransaction(self, data):
        """
        extend spec to support v,r,s signed transactions
        """
        if not isinstance(data, dict):
            raise BadRequestError('Transaction must be an object')

        def get_data_default(key, decoder, default=None):
            if key in data:
                return decoder(data[key])
            return default

        to = get_data_default('to', address_decoder, b'')
        startgas = get_data_default('gas', quantity_decoder, default_startgas)
        gasprice = get_data_default('gasPrice', quantity_decoder, default_gasprice)
        value = get_data_default('value', quantity_decoder, 0)
        data_ = get_data_default('data', data_decoder, b'')
        v = signed = get_data_default('v', quantity_decoder, 0)
        r = get_data_default('r', quantity_decoder, 0)
        s = get_data_default('s', quantity_decoder, 0)
        nonce = get_data_default('nonce', quantity_decoder, None)
        sender = get_data_default('from', address_decoder, self.app.services.accounts.coinbase)

        # create transaction
        if signed:
            assert nonce is not None, 'signed but no nonce provided'
            assert v and r and s
        else:
            nonce = self.app.services.chain.chain.head_candidate.get_nonce(sender)

        tx = Transaction(nonce, gasprice, startgas, to, value, data_, v, r, s)
        if not signed:
            assert sender in self.app.services.accounts, 'no account for sender'
            self.app.services.accounts.sign_tx(sender, tx)
        self.app.services.chain.add_transaction(tx, origin=None)

        log.debug('decoded tx', tx=tx.to_dict())

        if to == b'':  # create
            return address_encoder(processblock.mk_contract_address(tx.sender, nonce))
        else:
            return data_encoder(tx.hash)

    @public
    @decode_arg('block_id', block_id_decoder)
    @encode_res(data_encoder)
    def call(self, data, block_id=None):
        block = self.json_rpc_server.get_block(block_id)
        state_root_before = block.state_root

        # rebuild block state before finalization
        if block.has_parent():
            parent = block.get_parent()
            test_block = block.init_from_parent(parent, block.coinbase,
                                                timestamp=block.timestamp)
            for tx in block.get_transactions():
                success, output = processblock.apply_transaction(test_block, tx)
                assert success
        else:
            original = block.snapshot()
            original['journal'] = deepcopy(original['journal'])  # do not alter original journal
            test_block = ethereum.blocks.genesis(block.db)
            test_block.revert(original)

        # validate transaction
        if not isinstance(data, dict):
            raise BadRequestError('Transaction must be an object')
        to = address_decoder(data['to'])
        try:
            startgas = quantity_decoder(data['gas'])
        except KeyError:
            startgas = block.gas_limit - block.gas_used
        try:
            gasprice = quantity_decoder(data['gasPrice'])
        except KeyError:
            gasprice = 0
        try:
            value = quantity_decoder(data['value'])
        except KeyError:
            value = 0
        try:
            data_ = data_decoder(data['data'])
        except KeyError:
            data_ = b''
        try:
            sender = address_decoder(data['from'])
        except KeyError:
            sender = '\x00' * 20

        # apply transaction
        nonce = test_block.get_nonce(sender)
        tx = Transaction(nonce, gasprice, startgas, to, value, data_)
        tx.sender = sender

        try:
            success, output = processblock.apply_transaction(test_block, tx)
        except processblock.InvalidTransaction as e:
            success = False
        assert block.state_root == state_root_before

        if success:
            return output
        else:
            return False


class Filter(object):

    """A filter for logs.

    :ivar blocks: a list of block numbers
    :ivar addresses: a list of contract addresses or None to not consider
                     addresses
    :ivar topics: a list of topics or `None` to not consider topics
    :ivar pending: if `True` also look for logs from the current pending block
    :ivar latest: if `True` also look for logs from the current head
    :ivar blocks_done: a list of blocks that don't have to be searched again
    :ivar logs: a list of (:class:`ethereum.processblock.Log`, int, :class:`ethereum.blocks.Block`)
                triples that have been found
    :ivar new_logs: same as :attr:`logs`, but is reset at every access
    """

    def __init__(self, chain, blocks=None, addresses=None, topics=None,
                 pending=False, latest=False):
        self.chain = chain
        if blocks is not None:
            self.blocks = blocks
        else:
            self.blocks = []
        self.addresses = addresses
        self.topics = topics
        if self.topics:
            for topic in self.topics:
                assert topic is None or is_numeric(topic)
        self.pending = pending
        self.latest = latest

        self.blocks_done = set()
        self._logs = {}
        self._new_logs = {}

    def __repr__(self):
        return '<Filter(addresses=%r, topics=%r)>' % (self.addresses, self.topics)

    def check(self):
        """Check for new logs."""
        blocks_to_check = self.blocks[:]
        if self.pending:
            blocks_to_check.append(self.chain.head_candidate)
        if self.latest:
            blocks_to_check.append(self.chain.head)
        # go through all receipts of all blocks
        logger.debug('blocks to check', blocks=blocks_to_check)
        for block in blocks_to_check:
            if block in self.blocks_done:
                continue
            receipts = block.get_receipts()
            logger.debug('receipts', block=block, receipts=receipts)
            for receipt in receipts:
                for i, log in enumerate(receipt.logs):
                    logger.debug('log', log=log, topics=self.topics)
                    if self.topics is not None:
                        # compare topics one by one
                        topic_match = True
                        if len(log.topics) < len(self.topics):
                            topic_match = False
                        for filter_topic, log_topic in zip(self.topics, log.topics):
                            if filter_topic is not None and filter_topic != log_topic:
                                topic_match = False
                        if not topic_match:
                            continue
                    # check for address
                    if self.addresses is not None and log.address not in self.addresses:
                        continue
                    # still here, so match was successful => add to log list
                    assert False, 'we never find filters!'
                    id_ = ethereum.utils.sha3rlp(log)
                    self._logs[id_] = (log, i, block)
                    self._new_logs[id_] = (log, i, block)
        # don't check blocks again, that have been checked already and won't change anymore
        self.blocks_done |= set(blocks_to_check)
        self.blocks_done -= set([self.chain.head_candidate])

    @property
    def logs(self):
        self.check()
        return self._logs.values()

    @property
    def new_logs(self):
        self.check()
        ret = self._new_logs.copy()
        self._new_logs = {}
        return ret.values()


class NewBlockFilter(object):

    """A filter for new blocks.
    :ivar pending: if `True` also look for logs from the current pending block
    :ivar latest: if `True` also look for logs from the current head
    :ivar blocks_done: a list of blocks that don't have to be searched again
    """

    def __init__(self, chainservice, pending=False, latest=False):
        self.chainservice = chainservice
        self.blocks_done = set()
        assert latest or pending
        self.pending = pending
        self.latest = latest
        self.new_block_event = gevent.event.Event()
        self._new_block_cb = lambda b: self.new_block_event.set()
        self.chainservice.on_new_head_cbs.append(self._new_block_cb)

    def __repr__(self):
        return '<NewBlockFilter(latest=%r, pending=%r)>' % (self.latest, self.pending)

    def __del__(self):
        self.chainservice.on_new_head_cbs.remove(self._new_block_cb)

    def check(self):
        "returns changed block or None"
        if self.pending:
            block = self.chainservice.chain.head_candidate
        else:
            block = self.chainservice.chain.head
        if block not in self.blocks_done:
            self.blocks_done.add(block)
            return block
        # wait for event
        if self.new_block_event.is_set():
            self.new_block_event.clear()
        log.debug('NewBlockFilter, waiting for event', ts=time.time())
        if self.new_block_event.wait(timeout=0.9):
            log.debug('NewBlockFilter, got new_block event', ts=time.time())
            return self.check()


class FilterManager(Subdispatcher):

    prefix = 'eth_'
    required_services = ['chain']

    def __init__(self):
        self.filters = {}
        self.next_id = 0

    @public
    @encode_res(quantity_encoder)
    def newFilter(self, filter_dict):
        if not isinstance(filter_dict, dict):
            raise BadRequestError('Filter must be an object')
        b0 = self.json_rpc_server.get_block(
            block_id_decoder(filter_dict.get('fromBlock', 'latest')))
        b1 = self.json_rpc_server.get_block(
            block_id_decoder(filter_dict.get('toBlock', 'latest')))
        if b1.number < b0.number:
            raise BadRequestError('fromBlock must be prior or equal to toBlock')
        address = filter_dict.get('address', None)
        if is_string(address):
            addresses = [address_decoder(address)]
        elif isinstance(address, Iterable):
            addresses = [address_decoder(addr) for addr in address]
        elif address is None:
            addresses = None
        else:
            raise JSONRPCInvalidParamsError('Parameter must be address or list of addresses')
        if 'topics' in filter_dict:
            topics = []
            for topic in filter_dict['topics']:
                if topic is not None:
                    topics.append(big_endian_to_int(data_decoder(topic)))
                else:
                    topics.append(None)
        else:
            topics = None

        blocks = [b1]
        while blocks[-1] != b0:
            blocks.append(blocks[-1].get_parent())
        filter_ = Filter(self.chain.chain, reversed(blocks), addresses, topics)
        self.filters[self.next_id] = filter_
        self.next_id += 1
        return self.next_id - 1

    @public
    @encode_res(quantity_encoder)
    def newBlockFilter(self, s):
        if s not in ('latest', 'pending'):
            raise JSONRPCInvalidParamsError('Parameter must be either "latest" or "pending"')
        pending = (s == 'pending')
        latest = (s == 'latest')
        filter_ = NewBlockFilter(self.chain, pending=pending, latest=latest)
        self.filters[self.next_id] = filter_
        self.next_id += 1
        return self.next_id - 1

    @public
    @decode_arg('id_', quantity_decoder)
    def uninstallFilter(self, id_):
        try:
            self.filters.pop(id_)
            return True
        except KeyError:
            return False

    @public
    @decode_arg('id_', quantity_decoder)
    def getFilterChanges(self, id_):
        if id_ not in self.filters:
            raise BadRequestError('Unknown filter')
        filter_ = self.filters[id_]
        logger.debug('filter found', filter=filter_)
        if isinstance(filter_, NewBlockFilter):
            # For filters created with eth_newBlockFilter the return are block hashes
            # (DATA, 32 Bytes), e.g. ["0x3454645634534..."].
            r = filter_.check()
            if r:
                logger.debug('returning newblock', ts=time.time())
                return [data_encoder(r.hash)]
            else:
                return []
        else:
            return loglist_encoder(filter_.new_logs)

    @public
    @decode_arg('id_', quantity_decoder)
    @encode_res(loglist_encoder)
    def getFilterLogs(self, id_):
        if id_ not in self.filters:
            raise BadRequestError('Unknown filter')
        filter_ = self.filters[id_]
        if filter_.pending or filter_.latest:
            raise NotImplementedError()
            return [None] * len(filter_.logs)
        else:
            return self.filters[id_].logs

    # ########### Trace ############
    def _get_block_before_tx(self, txhash):
        tx, blk, i = self.app.services.chain.chain.index.get_transaction(txhash)
        # get the state we had before this transaction
        test_blk = ethereum.blocks.Block.init_from_parent(blk.get_parent(),
                                                          blk.coinbase,
                                                          extra_data=blk.extra_data,
                                                          timestamp=blk.timestamp,
                                                          uncles=blk.uncles)
        pre_state = test_blk.state_root
        for i in range(blk.transaction_count):
            tx_lst_serialized, sr, _ = blk.get_transaction(i)
            if sha3(rlp.encode(tx_lst_serialized)) == tx.hash:
                break
            else:
                pre_state = sr
        test_blk.state.root_hash = pre_state
        return test_blk, tx, i

    def _get_trace(self, txhash):
        try:  # index
            test_blk, tx, i = self._get_block_before_tx(txhash)
        except (KeyError, TypeError):
            raise Exception('Unknown Transaction  %s' % txhash)

        # collect debug output FIXME set loglevel trace???
        recorder = LogRecorder()
        # apply tx (thread? we don't want logs from other invocations)
        self.app.services.chain.add_transaction_lock.acquire()
        processblock.apply_transaction(test_blk, tx)  # FIXME deactivate tx context switch or lock
        self.app.services.chain.add_transaction_lock.release()
        return dict(tx=txhash, trace=recorder.pop_records())

    @public
    @decode_arg('txhash', data_decoder)
    def trace_transaction(self, txhash, exclude=[]):
        assert len(txhash) == 32
        assert set(exclude) in set(('stack', 'memory', 'storage'))
        t = self._get_trace(txhash)
        for key in exclude:
            for l in t['trace']:
                if key in l:
                    del l[key]
        return t

    @public
    @decode_arg('blockhash', data_decoder)
    def trace_block(self, blockhash, exclude=[]):
        txs = []
        blk = self.app.services.chain.chain.get(blockhash)
        for i in range(blk.transaction_count):
            tx_lst_serialized, sr, _ = blk.get_transaction(i)
            txhash_hex = sha3(rlp.encode(tx_lst_serialized)).encode('hex')
            txs.append(self.trace_transaction(txhash_hex, exclude))
        return txs


if __name__ == '__main__':
    import inspect
    from devp2p.app import BaseApp

    # deactivate service availability check
    for cls in JSONRPCServer.subdispatcher_classes():
        cls.required_services = []

    app = BaseApp(JSONRPCServer.default_config)
    jrpc = JSONRPCServer(app)
    dispatcher = jrpc.dispatcher

    def show_methods(dispatcher, prefix=''):
        # https://github.com/micheles/decorator/blob/3.4.1/documentation.rst
        for name, method in dispatcher.method_map.items():
            print prefix + name, inspect.formatargspec(public_methods[name])
        for sub_prefix, subdispatcher_list in dispatcher.subdispatchers.items():
            for sub in subdispatcher_list:
                show_methods(sub, prefix + sub_prefix)

    show_methods(dispatcher)
