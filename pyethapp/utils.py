import ethereum
from ethereum.blocks import Block, genesis
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
    scanners = ethereum.utils.scanners
    initial_alloc = {}
    for address, acct_state in data['pre'].items():
        address = ethereum.utils.decode_hex(address)
        balance = scanners['int256b'](acct_state['balance'][2:])
        nonce = acct_state['nonce'][2:]
        if nonce != '0':
            nonce = scanners['int256b'](nonce),
        initial_alloc[address] = {
            'balance': balance,
            'code': acct_state['code'],
            'nonce': nonce,
            'storage': acct_state['storage']
        }
    genesis(db, initial_alloc)  # builds the state trie
    genesis_block = rlp.decode(ethereum.utils.decode_hex(data['genesisRLP'][2:]), Block, db=db)
    blocks = [genesis_block]
    for blk in data['blocks']:
        rlpdata = ethereum.utils.decode_hex(blk['rlp'][2:])
        blocks.append(rlp.decode(rlpdata, Block, db=db, parent=blocks[-1]))
    return blocks
