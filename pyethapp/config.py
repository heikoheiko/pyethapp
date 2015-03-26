"""
This module provides a "global" config dictionary and methods to load it from
YAML files. At first import it tries to load the config from ``config.yaml``
in the platform dependant application directory. If the file doesn't exist, it
puts an example config there (not functional at the moment).
"""

import errno
import os
import click
from devp2p import crypto
import yaml


config = {}


def load_config(f):
    """Load config from string or file like object `f`, discarding the one
    already in place.
    """
    config.clear()
    update_config(f)


def update_config(f):
    """Load another config and merge it with the existing one.

    In case of key clashes (only the root level is considered), the original
    values are replaced.
    """
    config.update(yaml.load(f))


def set_config_param(s):
    """Set a specific config parameter.

    :param s: a string of the form ``a.b.c=d`` which will set the value of
              ``config['a']['b']['b']`` to ``yaml.load(d)``
    :raises: :exc:`ValueError` if `s` is malformed or the value to set is not
             valid YAML
    """
    try:
        param, value = s.split('=', 1)
        keys = param.split('.')
    except ValueError:
        raise ValueError('Invalid config parameter')
    d = config
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    try:
        d[keys[-1]] = yaml.load(value)
    except yaml.parser.ParserError:
        raise ValueError('Invalid config value')


default_config = """
app:
    services:  # in order of registration (name of classes, not cls.name)
        #- NodeDiscovery
        - PeerManager
        - JSONRPCServer

    # The modules in the following directories are loaded at startup.
    # Instances of BaseService found in these modules are made available as
    # service and can be registered via "app.services". If a service has the
    # same name as a builtin service, the contributed one takes precedence.
    contrib_dirs: []

p2p:
    num_peers: 10
    bootstrap_nodes:
        # local bootstrap
        # - enode://6ed2fecb28ff17dec8647f08aa4368b57790000e0e9b33a7b91f32c41b6ca9ba21600e9a8c44248ce63a71544388c6745fa291f88f8b81e109ba3da11f7b41b9@127.0.0.1:30303
        # go_bootstrap
        # - enode://6cdd090303f394a1cac34ecc9f7cda18127eafa2a3a06de39f6d920b0e583e062a7362097c7c65ee490a758b442acd5c80c6fce4b148c6a391e946b45131365b@54.169.166.226:30303
        # cpp_bootstrap
        #- enode://4a44599974518ea5b0f14c31c4463692ac0329cb84851f3435e6d1b18ee4eae4aa495f846a0fa1219bd58035671881d44423876e57db2abd57254d0197da0ebe@5.1.83.226:30303
    bootstrap_host: enode://6cdd090303f394a1cac34ecc9f7cda18127eafa2a3a06de39f6d920b0e583e062a7362097c7c65ee490a758b442acd5c80c6fce4b148c6a391e946b45131365b@54.169.166.226
    bootstrap_port: 30303

    listen_host: 0.0.0.0
    listen_port: 30303
    privkey_hex: 65462b0520ef7d3df61b9992ed3bea0c56ead753be7c8b3614e0ce01e4cac41b

jsonrpc:
    port: 8545

"""


CONFIG_DIRECTORY = click.get_app_dir('pyethapp')
CONFIG_FILENAME = os.path.join(CONFIG_DIRECTORY, 'config.yaml')
CONTRIB_DIRECTORY = os.path.join(CONFIG_DIRECTORY, 'contrib')


if os.path.exists(CONFIG_FILENAME):
    with open(CONFIG_FILENAME) as f:
        load_config(f)
else:
    load_config(default_config)
    pubkey = crypto.privtopub(config['p2p']['privkey_hex'].decode('hex'))
    config['p2p']['node_id'] = crypto.sha3(pubkey)
    config['app']['contrib_dirs'].append(CONTRIB_DIRECTORY)
    # problem with the following code: default config specified here is
    # ignored if config file already exists -> annoying for development with
    # frequently changing default config. Also: comments are discarded
#   try:
#       os.makedirs(CONFIG_DIRECTORY)
#   except OSError as exc:
#       if exc.errno == errno.EEXIST:
#           pass
#       else:
#           raise
#   with open(CONFIG_FILENAME, 'wb') as f:
#       yaml.dump(config, f)
