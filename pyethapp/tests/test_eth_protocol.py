from pyethapp.eth_protocol import ETHProtocol
from pyethereum import tester
tester.disable_logging()


class PeerMock(object):
    packets = []
    config = dict()

    def send_packet(self, packet):
        self.packets.append(packet)


def setup():
    peer = PeerMock()
    proto = ETHProtocol(peer=peer)
    chain = tester.state()
    cb_data = []

    def cb(proto, data):
        cb_data.append((proto, data))
    return peer, proto, chain, cb_data, cb


def test_status():
    peer, proto, chain, cb_data, cb = setup()
    genesis = head = chain.blocks[-1]

    # test status
    proto.send_status(total_difficulty=head.difficulty, chain_head_hash=head.hash,
                      genesis_hash=genesis.hash)
    packet = peer.packets.pop()
    proto.receive_status_callbacks.append(cb)
    proto._receive_status(packet)

    _p, _d = cb_data.pop()
    assert _p == proto
    assert isinstance(_d, dict)
    assert _d['total_difficulty'] == head.difficulty
    print _d
    assert _d['chain_head_hash'] == head.hash
    assert _d['genesis_hash'] == genesis.hash
    assert 'eth_version' in _d
    assert 'network_id' in _d


def test_blocks():
    peer, proto, chain, cb_data, cb = setup()

    # test blocks
    chain.mine(n=2)
    proto.send_blocks(blocks=chain.blocks)
    packet = peer.packets.pop()
    proto.receive_blocks_callbacks.append(cb)
    proto._receive_blocks(packet)

    _p, _d = cb_data.pop()
    assert 'blocks' in _d
    assert isinstance(_d['blocks'], list)
    # assert isinstance(_d['blocks'][0], pyethapp.eth_protocol.TransientBlock)

    # newblock
    proto.send_blocks(block=chain.blocks[-1], total_difficulty=chain.blocks[-1].chain_difficulty())
    packet = peer.packets.pop()
    proto.receive_blocks_callbacks.append(cb)
    proto._receive_blocks(packet)

    _p, _d = cb_data.pop()
    assert 'block' in _d
    assert 'total_difficulty' in _d
