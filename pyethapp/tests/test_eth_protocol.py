from pyethapp.eth_protocol import ETHProtocol, TransientBlock
from devp2p.service import WiredService
from devp2p.app import BaseApp
from pyethereum import tester
import rlp
tester.disable_logging()


class PeerMock(object):
    packets = []
    config = dict()

    def send_packet(self, packet):
        self.packets.append(packet)


def setup():
    peer = PeerMock()
    proto = ETHProtocol(peer, WiredService(BaseApp()))
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
    for block in _d['blocks']:
        assert isinstance(block, TransientBlock)
        assert isinstance(block.transactions, rlp.LazyList)
        assert isinstance(block.uncles, rlp.LazyList)
        # assert that transactions and uncles have not been decoded
        assert len(block.transactions._elements) == 0
        assert len(block.uncles._elements) == 0

    # newblock
    approximate_difficulty = chain.blocks[-1].difficulty * 3
    proto.send_newblock(block=chain.blocks[-1], total_difficulty=approximate_difficulty)
    packet = peer.packets.pop()
    proto.receive_newblock_callbacks.append(cb)
    proto._receive_newblock(packet)

    _p, _d = cb_data.pop()
    assert 'block' in _d
    assert 'total_difficulty' in _d
    assert _d['total_difficulty'] == approximate_difficulty
    assert _d['block'].header == chain.blocks[-1].header
    assert isinstance(_d['block'].transactions, rlp.LazyList)
    assert isinstance(_d['block'].uncles, rlp.LazyList)
    # assert that transactions and uncles have not been decoded
    assert len(_d['block'].transactions._elements) == 0
    assert len(_d['block'].uncles._elements) == 0
