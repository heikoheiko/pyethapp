from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
from devp2p.app import BaseApp
from pyethapp.eth_service import ChainService
from pyethapp.jsonrpc import JSONRPCServer
from pyethapp.db_service import DBService
from pyethapp import config
import tempfile
import copy

base_services = [DBService, NodeDiscovery, PeerManager, ChainService, JSONRPCServer]


def test_mk_privkey():
    for i in range(512):
        config.mk_privkey_hex()


def test_default_config():
    conf = config.get_default_config([BaseApp] + base_services)
    assert 'p2p' in conf
    assert 'app' not in conf


def test_save_load_config():
    conf = config.get_default_config([BaseApp] + base_services)
    assert conf

    # from fn or base path
    config.write_config(conf)
    conf2 = config.load_config()
    print 'b', conf
    print 'a', conf2
    assert conf == conf2

    path = tempfile.mktemp()
    assert path
    config.write_config(conf, path=path)
    conf3 = config.load_config(path=path)
    assert conf == conf3


def test_update_config():
    conf = config.get_default_config([BaseApp] + base_services)
    conf_copy = copy.deepcopy(conf)
    conf2 = config.update_config_with_defaults(conf, dict(p2p=dict(new_key=1)))
    assert conf is conf2
    assert conf == conf2
    assert conf_copy != conf


def test_set_config_param():
    conf = config.get_default_config([BaseApp] + base_services)
    assert conf['p2p']['min_peers']
    conf = config.set_config_param(conf, 'p2p.min_peers=3')  # strict
    assert conf['p2p']['min_peers'] == 3
    try:
        conf = config.set_config_param(conf, 'non.existent=1')
    except KeyError:
        errored = True
    assert errored
    conf = config.set_config_param(conf, 'non.existent=1', strict=False)
    assert conf['non']['existent'] == 1
