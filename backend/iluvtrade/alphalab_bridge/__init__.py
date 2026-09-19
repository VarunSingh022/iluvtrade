"""The one boundary between iluvtrade and AlphaLab.

**Every** import of ``alphalab`` in this application lives in this package.
``tests/unit/test_engine_boundary.py`` walks the source tree and fails if one
appears anywhere else, so the rule is enforced rather than merely stated.

What the boundary is for
------------------------

AlphaLab is the authority on market data, strategy dispatch, allocation, risk,
the OMS, execution, portfolio accounting and analytics. iluvtrade is the
authority on who is asking, what they are permitted to run, and what happened.
The bridge translates between the two vocabularies and does nothing else:

=============================  ==========================================
:mod:`.market`                 canonical dataset rows → ``MarketDataset``
:mod:`.runconfig`              application settings → ``RunConfig``
:mod:`.engine`                 drive ``BacktestEngine`` / ``TradingSession``
:mod:`.results`                ``BacktestResult`` → plain JSON
:mod:`.broker`                 AlphaLab's ``BrokerProtocol``, re-exported
=============================  ==========================================

What the bridge must never do
-----------------------------

Compute a P&L, net a position, decide a fill, apply a commission, or reimplement
any metric AlphaLab's analytics already produces. If a number is needed and
AlphaLab does not expose it, the answer is to identify the missing seam — not to
compute it here, where it would become a second accounting authority nothing
reconciles against.
"""

from alphalab import __version__ as ALPHALAB_VERSION

__all__ = ["ALPHALAB_VERSION"]
