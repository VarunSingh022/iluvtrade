"""AlphaLab's broker vocabulary, re-exported.

A venue connector must speak AlphaLab's types — that is the whole point of
:class:`~alphalab.broker.BrokerProtocol`. But the rule that every
``import alphalab`` lives in this package is what keeps the engine boundary one
directory rather than a convention, and ``tests/unit/test_engine_boundary.py``
enforces it.

So the vocabulary is re-exported here, unchanged, and a connector imports it
from the bridge. Nothing is wrapped, renamed or adapted: these *are* AlphaLab's
types, and a connector built on them satisfies AlphaLab's protocol exactly.
"""

# Imported from the packages, never their submodules: the public API is each
# package's ``__all__`` (AlphaLab ``docs/api/PUBLIC_API.md``), and moving a
# class between submodules is not a breaking change there. Several of these
# names (``OrderAccepted``, ``OrderSubmitted``, …) are also exported by
# ``alphalab.oms`` as *different* classes; the broker's are the ones meant.
from alphalab.broker import (
    BrokerAccount,
    BrokerConnected,
    BrokerDisconnected,
    BrokerEvent,
    BrokerExecution,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerProtocol,
    BrokerState,
    ConnectionStatus,
    ExecutionReceived,
    Heartbeat,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    OrderSubmitted,
    PaperBroker,
    ReconciliationReport,
    reconcile,
)
from alphalab.common import new_id as new_event_id
from alphalab.core import AssetType, OrderStatus, OrderType, Side, TimeInForce

__all__ = [
    "AssetType",
    "BrokerAccount",
    "BrokerConnected",
    "BrokerDisconnected",
    "BrokerEvent",
    "BrokerExecution",
    "BrokerOrder",
    "BrokerOrderStatus",
    "BrokerPosition",
    "BrokerProtocol",
    "BrokerState",
    "ConnectionStatus",
    "ExecutionReceived",
    "Heartbeat",
    "OrderAccepted",
    "OrderCancelled",
    "OrderRejected",
    "OrderStatus",
    "OrderSubmitted",
    "OrderType",
    "PaperBroker",
    "ReconciliationReport",
    "Side",
    "TimeInForce",
    "new_event_id",
    "reconcile",
]
