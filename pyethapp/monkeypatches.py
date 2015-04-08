
# patch broken gaslimit

import ethereum.blocks


def calc_gaslimit(parent):
    return parent.gas_limit
ethereum.blocks.calc_gaslimit = calc_gaslimit

# other, set username

import os
user = os.getlogin()
# fixme config ...
