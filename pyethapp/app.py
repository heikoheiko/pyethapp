
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
import config as konfig
import utils
from jsonrpc import JSONRPCServer
from db_service import DBService
from pyethapp import __version__

slogging.configure(config_string=':debug')
log = slogging.get_logger('app')


services = [DBService, NodeDiscovery, PeerManager, ChainService, JSONRPCServer]
services += utils.load_contrib_services()


class EthApp(BaseApp):

    client_version = 'pyethapp/v%s/%s/%s/%s' % (__version__,
                                                sys.platform,
                                                'py%d.%d.%d' % sys.version_info[:3],
                                                os.getlogin())  # FIXME: for development only
    default_config = dict(BaseApp.default_config)
    default_config['client_version'] = client_version


@click.command()
@click.option('alt_config', '--Config', '-C', type=click.File(), help='Alternative config file')
@click.option('config_values', '-c', multiple=True, type=str,
              help='Single configuration parameters (<param>=<value>)')
@click.option('data_dir', '--data-dir', '-d', multiple=False, type=str,
              help='data directory')
@click.option('log_config', '--log_config', '-l', multiple=False, type=str,
              help='log_config string: e.g. ":info,eth:debug')
@click.argument('command', required=False)
def app(command, alt_config, config_values, data_dir, log_config):
    """
    Welcome to ethapp version:{}

    invocation:
        ethapp [options] run        # starts the client

    options:
        ethapp [options] config     # shows the config

    """.format(EthApp.client_version)

    # configure logging
    log_config = log_config or ':info'
    slogging.configure(log_config)

    # data dir default or from cli option
    data_dir = data_dir or konfig.default_data_dir
    konfig.setup_data_dir(data_dir)  # if not available, sets up data_dir and required config
    log.info('using data in', path=data_dir)

    # prepare configuration
    # config files only contain required config (privkeys) and config different from the default
    if alt_config:  # specified config file
        config = konfig.load_config(alt_config)
    else:  # load config from default or set data_dir
        config = konfig.load_config(data_dir)

    config['data_dir'] = data_dir

    # add default config
    konfig.update_config_with_defaults(config, konfig.get_default_config([EthApp] + services))

    # override values with values from cmd line
    for config_value in config_values:
        try:
            konfig.set_config_param(config, config_value)
            # check if this is part of the default config
        except ValueError:
            raise BadParameter('Config parameter must be of the form "a.b.c=d" where "a.b.c" '
                               'specifies the parameter to set and d is a valid yaml value '
                               '(example: "-c jsonrpc.port=5000")')
    if command == 'config':
        konfig.dump_config(config)
        sys.exit(0)

    if command != 'run':
        sys.exit(0)

    # create app
    app = EthApp(config)

    # register services
    for service in services:
        assert issubclass(service, BaseService)
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
