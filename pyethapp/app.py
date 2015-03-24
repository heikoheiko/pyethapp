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
from config import config, load_config
import click
log = slogging.get_logger('app')
slogging.configure(config_string=':debug')


@click.command()
@click.option('alt_config', '--config', type=click.File(), help='Alternative config file')
def app(alt_config):
    if alt_config:
        load_config(alt_config)

    # create app
    app = BaseApp(config)

    # register services
    # NodeDiscovery.register_with_app(app)
    # PeerManager.register_with_app(app)
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
