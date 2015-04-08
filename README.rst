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

pyethapp is the python based client for the Ethereum_ blockchain 2.0 system.

Ethereum as a platform is focussed on enabling people to build new ideas using blockchain technology.

The python implementation aims to provide an easily hackable and extendable codebase.

pyetapp leverages two ethereum core components to implement the client:

* pyethereum_ - the core library, featuring the blockchain, the ethereum virtual machine, mining
* pydevp2p_ - the p2p networking library, featuring node discovery for and transport of multiple services over multiplexed and encrypted connections


.. _Ethereum: http://ethereum.org/
.. _pyethereum: https://github.com/ethereum/pyethereum
.. _pydevp2p: https://github.com/ethereum/pydevp2p


Status
------

Working prototype, which is interoperable with the go and cpp clients.
