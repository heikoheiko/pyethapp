
import monkeypatches
import sys
import os
import signal
import click
from click import BadParameter
import gevent
from gevent.event import Event
from devp2p.service import BaseService
from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
from devp2p.app import BaseApp
from eth_service import ChainService
import ethereum.slogging as slogging
from config import load_config, set_config_param, dump_config
from jsonrpc import JSONRPCServer
from db_service import DBService

slogging.configure(config_string=':debug')
log = slogging.get_logger('app')


base_services = [DBService, NodeDiscovery, PeerManager, ChainService, JSONRPCServer]


class EthApp(BaseApp):

    default_config = dict(BaseApp.default_config)


@click.command()
@click.option('alt_config', '--Config', '-C', type=click.File(), help='Alternative config file')
@click.option('config_values', '-c', multiple=True, type=str,
              help='Single configuration parameters (<param>=<value>)')
@click.option('show_config', '--show-config', '-s', multiple=False, type=bool,
              help='Show the configuration')
def app(alt_config, config_values, show_config):

    if alt_config:  # specified config file
        config = load_config(alt_config)
    else:
        config = load_config()  # load config from default config dir
    # override values with values from cmd line
    for config_value in config_values:
        try:
            set_config_param(config, config_value)
            # check if this is part of the default config
        except ValueError:
            raise BadParameter('Config parameter must be of the form "a.b.c=d" where "a.b.c" '
                               'specifies the parameter to set and d is a valid yaml value '
                               '(example: "-c jsonrpc.port=5000")')
    if show_config:
        dump_config(config)

    # create app
    app = EtApp(config)

    # register services
    services = base_services + load_contrib_services(config)
    for service in services:
        assert isinstance(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)

    # stop on every unhandled exception!
    gevent.get_hub().SYSTEM_ERROR = BaseException  # (KeyboardInterrupt, SystemExit, SystemError)

    # start app
    app.start()

    # wait for interupt
    evt = Event()
    gevent.signal(signal.SIGQUIT, evt.set)
    gevent.signal(signal.SIGTERM, evt.set)
    gevent.signal(signal.SIGINT, evt.set)
    evt.wait()

    # finally stop
    app.stop()

if __name__ == '__main__':
    #  python app.py 2>&1 | less +F
    app()
