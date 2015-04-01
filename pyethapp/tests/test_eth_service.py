from pyethereum.db import EphemDB
from pyethapp import eth_service
from pyethapp import eth_protocol
from pyethereum import slogging
slogging.configure(config_string=':info')


class AppMock(object):

    class Services(object):
        pass

    def __init__(self):
        self.config = dict()
        self.services = self.Services()
        self.services.db = EphemDB()


class PeerMock(object):

    def __init__(self, app):
        self.config = app.config
        self.send_packet = lambda x: x

blk_rlp = (
    "f90207f901fef901f9a018632409b5181b4b6508d4b2b2a5463f814ac47bb580c1fe545b4e0"
    "c029c36d8a01dcc4de8dec75d7aab85b567b6ccd41ad312451b948a7413f0a142fd40d49347"
    "94b8a2bef22b002a4d23206bd737310d0358c66d63a07ee7071f0538e10385f65e5bac1275a"
    "61da60b9c81013b48e1ff43fc12a1c037a056e81f171bcc55a6ff8345e692c0f86e5b48e01b"
    "996cadc001622fb5e363b421a056e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc00"
    "1622fb5e363b421b90100000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000830f4c27823647832fefd88084551c7b5080a06bdda1da3ac7e8f6be01b4d05d417"
    "5f0b5d2a84fef43716c1f16c71d9a32193d881c2ea8eea335e950c0c08502595559e2")


def test_receive_newblock():
    app = AppMock()
    eth = eth_service.ChainService(app)
    proto = eth_protocol.ETHProtocol(PeerMock(app), eth)
    d = eth_protocol.ETHProtocol.newblock.decode_payload(blk_rlp.decode('hex'))
    eth.on_receive_newblock(proto, **d)
