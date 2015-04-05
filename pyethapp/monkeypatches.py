
# patch broken gaslimit

import ethereum.blocks


def calc_gaslimit(parent):
    return parent.gas_limit
ethereum.blocks.calc_gaslimit = calc_gaslimit

# other
