from importlib import import_module
import inspect
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
import devp2p.crypto as crypto
from devp2p.app import BaseApp
import pyethereum.slogging as slogging
from config import config, load_config, set_config_param
from jsonrpc import JSONRPCServer

log = slogging.get_logger('app')
slogging.configure(config_string=':debug')


services = {}
for service in [NodeDiscovery, PeerManager, JSONRPCServer]:
    services[service.name] = service


@click.command()
@click.option('alt_config', '--Config', '-C', type=click.File(), help='Alternative config file')
@click.option('config_values', '-c', multiple=True, type=str,
              help='Single configuration parameters (<param>=<value>)')
def app(alt_config, config_values):
    if alt_config:
        load_config(alt_config)
    for config_value in config_values:
        try:
            set_config_param(config_value)
        except ValueError:
            raise BadParameter('Config parameter must be of the form "a.b.c=d" where "a.b.c" '
                               'specifies the parameter to set and d is a valid yaml value '
                               '(example: "-c jsonrpc.port=5000")')

    # create app
    app = BaseApp(config)

    # load contrib services
    contrib_modules = []
    for directory in config['app']['contrib_dirs']:
        sys.path.append(directory)
        for filename in os.listdir(directory):
            path = os.path.join(directory, filename)
            if os.path.isfile(path) and filename.endswith('.py'):
                contrib_modules.append(import_module(filename[:-3]))
    contrib_services = {}
    for module in contrib_modules:
        classes = inspect.getmembers(module, inspect.isclass)
        for _, cls in classes:
            if issubclass(cls, BaseService) and cls != BaseService:
                contrib_services[cls.name] = cls
    log.info('Loaded contrib services', services=sorted(contrib_services.keys()))
    replaced_services = set(services).intersection(set(contrib_services))
    if  len(replaced_services) > 0:
        log.warning('Replaced some built in services', services=replaced_services)
    services.update(contrib_services)

    # register services
    for name in config['app']['services']:
        try:
            service = services[name]
        except KeyError:
            log.warning('Attempted to register unkown service', service=name)
        else:
            service.register_with_app(app)

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
