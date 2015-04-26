"""
Support for simple yaml persisted config dicts.

Usual building of the configuration
 - with datadir (default or from option)
    - create dir if not available
        - create minimal config if not available
    - load config
 - App is initialized with an (possibly empty) config
    which is recursively updated (w/o overriding values) with its own default config
 - Services are initialized with app and
    recursively update (w/o overriding values) the config with their default config

todo:
    datadir


"""
import os
import click
from devp2p.utils import update_config_with_defaults
import yaml
import ethereum.slogging as slogging
from devp2p.service import BaseService
from devp2p.app import BaseApp
from accounts import mk_random_privkey

slogging.configure(config_string=':debug')
log = slogging.get_logger('config')

default_data_dir = click.get_app_dir('pyethapp')


def get_config_path(data_dir=default_data_dir):
    return os.path.join(data_dir, 'config.yaml')

default_config_path = get_config_path(default_data_dir)


def setup_data_dir(data_dir=None):
    if data_dir and not os.path.exists(data_dir):
        os.makedirs(data_dir)
        setup_required_config(data_dir)


required_config = dict(node=dict(privkey_hex=''), accounts=dict(privkeys_hex=[]))


def check_config(config, required_config=required_config):
    "check if values are set"
    for k, v in required_config.items():
        if not config.get(k):
            return False
        if isinstance(v, dict):
            if not check_config(config[k], v):
                return False
    return True


def setup_required_config(data_dir=default_data_dir):
    "writes minimal necessary config to data_dir"
    log.info('setup default config', path=data_dir)
    config_path = get_config_path(data_dir)
    assert not os.path.exists(config_path)
    setup_data_dir(data_dir)
    config = dict(node=dict(privkey_hex=mk_random_privkey().encode('hex')),
                  accounts=dict(privkeys_hex=[mk_random_privkey().encode('hex')]))
    write_config(config, config_path)


def get_default_config(services):
    "collect default_config from services"
    config = dict()
    assert isinstance(services, list)
    for s in services:
        assert isinstance(s, (BaseService, BaseApp)) or issubclass(s, (BaseService, BaseApp))
        update_config_with_defaults(config, s.default_config)
    return config


def _fix_accounts(path):
    config = yaml.load(open(path))
    eth_key = config.get('eth', {}).get('privkey_hex')
    if eth_key:
        print 'fixing config', path
        del config['eth']['privkey_hex']
        if not config['eth']:
            del config['eth']
        assert 'accounts' not in config
        config['accounts'] = dict(privkeys_hex=[eth_key])
        write_config(config)


def load_config(path=default_config_path):
    """Load config from string or file like object `path`."""
    log.info('loading config', path=path)
    if os.path.exists(path):
        if os.path.isdir(path):
            path = get_config_path(path)
        _fix_accounts(path)  # FIXME
        return yaml.load(open(path))
    return dict()


def write_config(config, path=default_config_path):
    """Load config from string or file like object `f`, discarding the one
    already in place.
    """
    assert path
    log.info('writing config', path=path)
    with open(path, 'wb') as f:
        yaml.dump(config, f)


def set_config_param(config, s, strict=True):
    """Set a specific config parameter.

    :param s: a string of the form ``a.b.c=d`` which will set the value of
              ``config['a']['b']['b']`` to ``yaml.load(d)``
    :param strict: if `True` will only override existing values.
    :raises: :exc:`ValueError` if `s` is malformed or the value to set is not
             valid YAML
    """
    # fixme add += support
    try:
        param, value = s.split('=', 1)
        keys = param.split('.')
    except ValueError:
        raise ValueError('Invalid config parameter')
    d = config
    for key in keys[:-1]:
        if strict and key not in d:
            raise KeyError('Unknown config option')
        d = d.setdefault(key, {})
    try:
        d[keys[-1]] = yaml.load(value)
    except yaml.parser.ParserError:
        raise ValueError('Invalid config value')
    return config


def dump_config(config):
    print yaml.dump(config)
