import yaml
import sys
import os
import signal
import gevent
from gevent.event import Event
from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
import devp2p.crypto as crypto
from devp2p.app import BaseApp
from jsonrpc import JSONRPCServer
import pyethereum.slogging as slogging
from config import config, load_config, set_config_param
import click
from click import BadParameter
log = slogging.get_logger('app')
slogging.configure(config_string=':debug')


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

    # register services
    # NodeDiscovery.register_with_app(app)
    PeerManager.register_with_app(app)
    JSONRPCServer.register_with_app(app)

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
