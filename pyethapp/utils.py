import json
import ethereum
from ethereum.blocks import Block, genesis
from ethereum.db import EphemDB
import rlp

def _load_contrib_services(config):  # FIXME
    # load contrib services
    contrib_directory = os.path.join(config_directory, 'contrib')  # change to pyethapp/contrib
    contrib_modules = []
    for directory in config['app']['contrib_dirs']:
        sys.path.append(directory)
        for filename in os.listdir(directory):
            path = os.path.join(directory, filename)
            if os.path.isfile(path) and filename.endswith('.py'):
                contrib_modules.append(import_module(filename[:-3]))
    contrib_services = []
    for module in contrib_modules:
        classes = inspect.getmembers(module, inspect.isclass)
        for _, cls in classes:
            if issubclass(cls, BaseService) and cls != BaseService:
                contrib_services.append(cls)
    log.info('Loaded contrib services', services=sorted(contrib_services.keys()))
    return contrib_services


def load_contrib_services():  # FIXME: import from contrib
    return []


def load_block_tests(data, db):
    """Load blocks from json file.

    :param data: the data from the json file as dictionary
    :param db: the db in which the blocks will be stored
    :raises: :exc:`ValueError` if the file contains invalid blocks
    :raises: :exc:`KeyError` if the file is missing required data fields
    :returns: a list of blocks in an ephem db
    """
    b = genesis(db, data['pre'])
    gbh = data["genesisBlockHeader"]
    b.bloom = ethereum.utils.scanners['int256b'](gbh['bloom'])
    b.timestamp = ethereum.utils.scanners['int'](gbh['timestamp'])
    b.nonce = ethereum.utils.scanners['bin'](gbh['nonce'])
    b.extra_data = ethereum.utils.scanners['bin'](gbh['extraData'])
    b.gas_limit = ethereum.utils.scanners['int'](gbh['gasLimit'])
    b.gas_used = ethereum.utils.scanners['int'](gbh['gasUsed'])
    b.coinbase = ethereum.utils.decode_hex(gbh['coinbase'])
    b.difficulty = int(gbh['difficulty'])
    b.prevhash = ethereum.utils.scanners['bin'](gbh['parentHash'])
    b.mixhash = ethereum.utils.scanners['bin'](gbh['mixHash'])
    if (b.hash != ethereum.utils.scanners['bin'](gbh['hash']) or
        b.receipts.root_hash != ethereum.utils.scanners['bin'](gbh['receiptTrie']) or
        b.transactions.root_hash != ethereum.utils.scanners['bin'](gbh['transactionsTrie']) or
        b.state.root_hash != ethereum.utils.scanners['bin'](gbh['stateRoot']) or
        ethereum.utils.sha3rlp(b.uncles) != ethereum.utils.scanners['bin'](gbh['uncleHash']) or
        not b.header.check_pow()):
        raise ValueError('Invalid genesis block')
    blocks = [b]
    for blk in data['blocks']:
        rlpdata = ethereum.utils.decode_hex(blk['rlp'][2:])
        blocks.append(rlp.decode(rlpdata, Block, db=db, parent=blocks[-1]))
    return blocks
