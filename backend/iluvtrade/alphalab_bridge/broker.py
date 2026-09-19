"""AlphaLab's broker vocabulary, re-exported.

A venue connector must speak AlphaLab's types — that is the whole point of
:class:`~alphalab.broker.protocol.BrokerProtocol`. But the rule that every
``import alphalab`` lives in this package is what keeps the engine boundary one
directory rather than a convention, and ``tests/unit/test_engine_boundary.py``
enforces it.

So the vocabulary is re-exported here, unchanged, and a connector imports it
from the bridge. Nothing is wrapped, renamed or adapted: these *are* AlphaLab's
types, and a connector built on them satisfies AlphaLab's protocol exactly.
"""

from alphalab.broker.account import BrokerAccount
from alphalab.broker.events import (
    BrokerConnected,
    BrokerDisconnected,
    BrokerEvent,
    ExecutionReceived,
    Heartbeat,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    OrderSubmitted,
)
from alphalab.broker.execution import BrokerExecution
from alphalab.broker.order import BrokerOrder, BrokerOrderStatus
from alphalab.broker.paper import PaperBroker
from alphalab.broker.position import BrokerPosition
from alphalab.broker.protocol import BrokerProtocol
from alphalab.broker.reconciliation import ReconciliationReport, reconcile
from alphalab.broker.state import BrokerState, ConnectionStatus
from alphalab.common.ids import new_id as new_event_id
from alphalab.core.enums import AssetType, OrderStatus, OrderType, Side, TimeInForce

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
