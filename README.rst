===============================
pyethapp
===============================

.. image:: https://img.shields.io/travis/ethereum/pyethapp.svg
        :target: https://travis-ci.org/ethereum/pyethapp

.. image:: https://coveralls.io/repos/ethereum/pyethapp/badge.svg
        :target: https://coveralls.io/r/ethereum/pyethapp


.. image:: https://img.shields.io/pypi/v/pyethapp.svg
        :target: https://pypi.python.org/pypi/pyethapp

.. image:: https://readthedocs.org/projects/pyethapp/badge/?version=latest
        :target: https://readthedocs.org/projects/pyethapp/?badge=latest


Introduction
------------

pyethapp is the python based client implementing the Ethereum_ cryptoeconomic state machine.

Ethereum as a platform is focussed on enabling people to build new ideas using blockchain technology.

The python implementation aims to provide an easily hackable and extendable codebase.

pyethapp leverages two ethereum core components to implement the client:

* pyethereum_ - the core library, featuring the blockchain, the ethereum virtual machine, mining
* pydevp2p_ - the p2p networking library, featuring node discovery for and transport of multiple services over multiplexed and encrypted connections


.. _Ethereum: http://ethereum.org/
.. _pyethereum: https://github.com/ethereum/pyethereum
.. _pydevp2p: https://github.com/ethereum/pydevp2p


Installation and invocation
---------------------------

* git clone https://github.com/ethereum/pyethapp
* cd pyethapp
* python setup.py install
* pyethapp      (shows help)
* pyethapp run  (starts the client)

There is also Dockerfile in the repo.


Status
------

 * Working PoC9 prototype
 * interoperable with the go and cpp clients
 * jsonrpc (mostly)

