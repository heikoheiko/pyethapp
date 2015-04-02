
# patch broken gaslimit

import pyethereum.blocks


def calc_gaslimit(parent):
    return parent.gas_limit
pyethereum.blocks.calc_gaslimit = calc_gaslimit

# other
