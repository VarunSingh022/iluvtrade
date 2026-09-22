"""iluvtrade — the application platform around AlphaLab.

Three responsibilities live in this repository and they do not overlap:

* **Platform** (:mod:`iluvtrade.platform`) owns users, organizations, sessions,
  authorization, audit and notifications. It is the authority on *who* is asking.
* **RedDesk** (:mod:`iluvtrade.reddesk`) owns the marketplace: listings,
  purchases, entitlements and creator payouts. It is the authority on *what a
  user is permitted to run*.
* **AlphaLab** is an installed dependency (``alphalab==3.5.0``) and is the
  authority on every quantitative and trading semantic: market data, strategy
  dispatch, allocation, risk, OMS, execution, portfolio accounting and
  analytics. This package never reimplements any of it.

:mod:`iluvtrade.alphalab_bridge` is the single place where this application is
allowed to import from ``alphalab``. Everything else goes through it, so the
boundary is one directory rather than a convention.
"""

__version__ = "0.1.0"
