import yaml
import io
import sys
import signal
import gevent
from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
import devp2p.crypto as crypto
from devp2p.app import BaseApp
from jsonrpc import JSONRPCServer
import pyethereum.slogging as slogging
log = slogging.get_logger('app')
slogging.configure(config_string=':debug')


def run_app():
    # config

    # read config
    sample_config = """
p2p:
    num_peers: 10
    bootstrap_nodes:
        # local bootstrap
        # - enode://6ed2fecb28ff17dec8647f08aa4368b57790000e0e9b33a7b91f32c41b6ca9ba21600e9a8c44248ce63a71544388c6745fa291f88f8b81e109ba3da11f7b41b9@127.0.0.1:30303
        # go_bootstrap
        - enode://6cdd090303f394a1cac34ecc9f7cda18127eafa2a3a06de39f6d920b0e583e062a7362097c7c65ee490a758b442acd5c80c6fce4b148c6a391e946b45131365b@54.169.166.226:30303
        # cpp_bootstrap
        #- enode://4a44599974518ea5b0f14c31c4463692ac0329cb84851f3435e6d1b18ee4eae4aa495f846a0fa1219bd58035671881d44423876e57db2abd57254d0197da0ebe@5.1.83.226:30303

    listen_host: 0.0.0.0
    listen_port: 30303
    privkey_hex: 65462b0520ef7d3df61b9992ed3bea0c56ead753be7c8b3614e0ce01e4cac41b
    """
    if len(sys.argv) == 1:
        config = yaml.load(io.BytesIO(sample_config))
        pubkey = crypto.privtopub(config['p2p']['privkey_hex'].decode('hex'))
        config['p2p']['node_id'] = crypto.sha3(pubkey)
    else:
        fn = sys.argv[1]
        log.info('loading config from', fn=fn)
        config = yaml.load(open(fn))

    print config
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


if __name__ == '__main__':
    run_app()
