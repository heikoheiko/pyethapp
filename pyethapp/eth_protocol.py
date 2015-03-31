from devp2p.protocol import BaseProtocol
from pyethereum.transactions import Transaction
from pyethereum.blocks import Block, BlockHeader
import rlp
from pyethereum import slogging
log = slogging.get_logger('protocol.eth')


class ETHProtocol(BaseProtocol):

    """
    DEV Ethereum Wire Protocol
    https://github.com/ethereum/wiki/wiki/Ethereum-Wire-Protocol
    """
    protocol_id = 1
    name = 'eth'
    version = 58
    network_id = 0

    def __init__(self, peer):
        # required by P2PProtocol
        self.config = peer.config
        BaseProtocol.__init__(self, peer)

    class status(BaseProtocol.command):

        """
        protocolVersion: The version of the Ethereum protocol this peer implements. 30 at present.
        networkID: The network version of Ethereum for this peer. 0 for the official testnet.
        totalDifficulty: Total Difficulty of the best chain. Integer, as found in block header.
        latestHash: The hash of the block with the highest validated total difficulty.
        GenesisHash: The hash of the Genesis block.
        """
        cmd_id = 0

        structure = [
            ('eth_version', rlp.sedes.big_endian_int),
            ('network_id', rlp.sedes.big_endian_int),
            ('total_difficulty', rlp.sedes.big_endian_int),
            ('chain_head_hash', rlp.sedes.binary),
            ('genesis_hash', rlp.sedes.binary)]

        def create(self, proto, total_difficulty, chain_head_hash, genesis_hash):
            return [proto.version, proto.network_id, total_difficulty, chain_head_hash, genesis_hash]

    class transactions(BaseProtocol.command):

        """
        Specify (a) transaction(s) that the peer should make sure is included on its transaction
        queue. The items in the list (following the first item 0x12) are transactions in the
        format described in the main Ethereum specification. Nodes must not resend the same
        transaction to a peer in the same session. This packet must contain at least one (new)
        transaction.
        """
        cmd_id = 2
        structure = [('transactions', rlp.sedes.CountableList(Transaction))]

        # todo: bloomfilter: so we don't send tx to the originating peer

    class getblockhashes(BaseProtocol.command):

        """
        Requests a BlockHashes message of at most maxBlocks entries, of block hashes from
        the blockchain, starting at the parent of block hash. Does not require the peer
        to give maxBlocks hashes - they could give somewhat fewer.
        """
        cmd_id = 3

        structure = [
            ('child_block_hash', rlp.sedes.binary),
            ('max_blocks', rlp.sedes.big_endian_int),
        ]

        def create(self, proto, child_block_hash, max_blocks):
            return [child_block_hash, max_blocks]

    class blockhashes(BaseProtocol.command):

        """
        Gives a series of hashes of blocks (each the child of the next). This implies that
        the blocks are ordered from youngest to oldest.
        """
        cmd_id = 4
        structure = [('block_hashes', rlp.sedes.CountableList(rlp.sedes.binary))]

    class getblocks(BaseProtocol.command):

        """
        Requests a Blocks message detailing a number of blocks to be sent, each referred to
        by a hash. Note: Don't expect that the peer necessarily give you all these blocks
        in a single message - you might have to re-request them.
        """
        cmd_id = 5
        structure = [('block_hashes', rlp.sedes.CountableList(rlp.sedes.binary))]

    class blocks(BaseProtocol.command):
        cmd_id = 6
        structure = [('blocks', rlp.sedes.CountableList(Block))]

        @classmethod
        def decode_payload(cls, rlp_data):
            # convert to dict
            block_data = rlp.decode_lazy(rlp_data)
            assert len(block_data) == 1
            blocks = []
            for block in block_data[0]:
                blocks.append(TransientBlock(block))
            data = [blocks]
            return dict((cls.structure[i][0], v) for i, v in enumerate(data))

    class newblock(BaseProtocol.command):

        """
        NewBlock [+0x07, [blockHeader, transactionList, uncleList], totalDifficulty]
        Specify a single block that the peer should know about.
        The composite item in the list (following the message ID) is a block in
        the format described in the main Ethereum specification.
        """
        cmd_id = 7
        structure = [('block', Block), ('total_difficulty', rlp.sedes.big_endian_int)]

        # todo: bloomfilter: so we don't send block to the originating peer

        @classmethod
        def decode_payload(cls, rlp_data):
            # convert to dict
            ll = rlp.decode_lazy(rlp_data)
            assert len(ll) == 2
            transient_block = TransientBlock(ll[0])
            difficulty = rlp.sedes.big_endian_int.deserialize(ll[1])
            data = [transient_block, difficulty]
            return dict((cls.structure[i][0], v) for i, v in enumerate(data))


class TransientBlock(object):
    """A partially decoded, unvalidated block."""

    def __init__(self, block_data):
        self.header = BlockHeader.deserialize(block_data[0])
        self.transaction_list = block_data[1]
        self.uncles = block_data[2]

    def to_block(self, db, parent=None):
        """Convert the transient block to a :class:`pyethereum.blocks.Block`"""
        tx_list = rlp.sedes.CountableList(Transaction).deserialize(self.transaction_list)
        uncles = rlp.sedes.CountableList(BlockHeader).deserialize(self.uncles)
        return Block(self.header, tx_list, uncles, db=db, parent=parent)
