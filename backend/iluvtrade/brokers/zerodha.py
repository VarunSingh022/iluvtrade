"""The Zerodha Kite Connect v3 connector.

Contracts verified against https://kite.trade/docs/connect/v3/ (user, orders and
portfolio sections). Nothing here is invented:

==========================================  ==========================================
Login                                       ``https://kite.zerodha.com/connect/login?v=3&api_key=…``
Session exchange                            ``POST /session/token``
Checksum                                    ``SHA-256(api_key + request_token + api_secret)``
Auth header                                 ``Authorization: token <api_key>:<access_token>``
Version header                              ``X-Kite-Version: 3``
Place / modify / cancel                     ``POST|PUT|DELETE /orders/{variety}[/{order_id}]``
Orders, trades                              ``GET /orders``, ``GET /trades``
Positions, holdings                         ``GET /portfolio/positions``, ``/portfolio/holdings``
Profile, margins                            ``GET /user/profile``, ``GET /user/margins``
Logout                                      ``DELETE /session/token``
==========================================  ==========================================

**Access tokens expire at 06:00 IST the next day** — a regulatory requirement,
not a configuration choice. :meth:`ZerodhaClient.token_expiry` computes that
instant so the UI can say when a connection will need reauthorizing instead of
discovering it at the first rejected order.

What is real and what is not
----------------------------

Every request shape, path, header, enum and checksum below is the documented
contract, and the mapping onto AlphaLab's
:class:`~alphalab.broker.protocol.BrokerProtocol` is complete and exercised by
tests against a local HTTP server that speaks the same protocol.

**It has never been run against Zerodha.** Doing so needs a Kite Connect
subscription, an app registration with a redirect URL, and a funded account —
none of which this build can conjure. ``docs/BROKERS.md`` records that as an
external dependency, and :data:`VERIFIED_AGAINST_LIVE_VENUE` is ``False`` so the
application can *say so in the UI* rather than implying a connection that has
been proven.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

# The broker vocabulary comes from the bridge, not from ``alphalab`` directly:
# every engine import in this application lives in one directory, and
# ``tests/unit/test_engine_boundary.py`` enforces it. These are AlphaLab's own
# types, re-exported unchanged, so the adapter below satisfies AlphaLab's
# ``BrokerProtocol`` exactly.
from iluvtrade.alphalab_bridge.broker import (
    BrokerAccount as AlphaBrokerAccount,
)
from iluvtrade.alphalab_bridge.broker import (
    BrokerConnected,
    BrokerDisconnected,
    BrokerEvent,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerState,
    ConnectionStatus,
    ExecutionReceived,
    Heartbeat,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    OrderStatus,
    OrderSubmitted,
    OrderType,
    Side,
    TimeInForce,
    new_event_id,
)
from iluvtrade.brokers.crypto import SecretString

logger = logging.getLogger("iluvtrade.brokers.zerodha")

__all__ = [
    "VERIFIED_AGAINST_LIVE_VENUE",
    "ZerodhaAdapter",
    "ZerodhaClient",
    "ZerodhaError",
    "ZerodhaSession",
    "login_url",
    "session_checksum",
]

#: This connector has never executed against Zerodha. See the module docstring.
VERIFIED_AGAINST_LIVE_VENUE = False

API_ROOT = "https://api.kite.trade"
LOGIN_ROOT = "https://kite.zerodha.com/connect/login"
KITE_VERSION = "3"

#: Tokens expire at 06:00 India Standard Time (UTC+05:30) the following day.
IST = timedelta(hours=5, minutes=30)
TOKEN_EXPIRY_LOCAL_TIME = time(6, 0)

#: AlphaLab side → Kite ``transaction_type``.
_SIDE_TO_KITE = {"buy": "BUY", "sell": "SELL"}

#: Kite order status → AlphaLab's order status.
#:
#: The target is ``alphalab.core.enums.OrderStatus``, not
#: ``BrokerOrderStatus``. The two are different vocabularies kept apart on
#: purpose: ``BrokerOrderStatus`` has exactly three members
#: (``PENDING_SUBMIT``, ``SUBMITTED``, ``PENDING_CANCEL``) and describes *our
#: side of the wire*, while ``OrderStatus`` describes what the venue says the
#: order is. ``BrokerOrder.status`` is typed as the union of both, so a status
#: from either is valid — but a filled order is ``OrderStatus.FILLED``, and
#: there is no ``BrokerOrderStatus.FILLED`` to reach for.
#:
#: Kite's interim states are mapped explicitly rather than defaulted: treating
#: "VALIDATION PENDING" as accepted would report an order as working that the
#: venue has not taken.
_STATUS_TO_ALPHALAB: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.FILLED,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "OPEN": OrderStatus.ACCEPTED,
    "TRIGGER PENDING": OrderStatus.ACCEPTED,
    "MODIFIED": OrderStatus.ACCEPTED,
    "PUT ORDER REQ RECEIVED": OrderStatus.PENDING,
    "VALIDATION PENDING": OrderStatus.PENDING,
    "OPEN PENDING": OrderStatus.PENDING,
    "MODIFY VALIDATION PENDING": OrderStatus.PENDING,
    "MODIFY PENDING": OrderStatus.PENDING,
    "CANCEL PENDING": OrderStatus.CANCEL_PENDING,
    "AMO REQ RECEIVED": OrderStatus.PENDING,
}


class ZerodhaError(RuntimeError):
    """A Kite API call failed. Never carries a credential."""

    def __init__(
        self, message: str, *, status_code: int | None = None, error_type: str = ""
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type

    @property
    def is_authentication_failure(self) -> bool:
        """Whether the connection needs reauthorizing rather than retrying."""

        return self.status_code == 403 or self.error_type == "TokenException"


def session_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    """``SHA-256(api_key + request_token + api_secret)``, per the Kite docs."""

    return hashlib.sha256((api_key + request_token + api_secret).encode("utf-8")).hexdigest()


def login_url(api_key: str, *, redirect_params: str | None = None) -> str:
    """The URL a user is sent to in order to authorize this app.

    The browser goes here; the secret never does. Kite redirects back to the
    **registered** redirect URL with a short-lived ``request_token``, which the
    server then exchanges. This is why the API secret is only ever on the server.
    """

    query: dict[str, str] = {"v": KITE_VERSION, "api_key": api_key}
    if redirect_params:
        query["redirect_params"] = redirect_params
    return f"{LOGIN_ROOT}?{urlencode(query)}"


def token_expiry(now: datetime | None = None) -> datetime:
    """When an access token issued at ``now`` will stop working.

    06:00 IST on the next calendar day, expressed in UTC. A token issued at
    05:00 IST still dies at 06:00 the *same* morning, which this handles: the
    expiry is the next occurrence of 06:00 IST, not "tomorrow".
    """

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    local = moment + IST
    candidate = datetime.combine(local.date(), TOKEN_EXPIRY_LOCAL_TIME, tzinfo=UTC)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate - IST


@dataclass(frozen=True)
class ZerodhaSession:
    """A live Kite session. The token is a :class:`SecretString`."""

    api_key: str
    access_token: SecretString
    user_id: str
    user_name: str
    email: str
    expires_at: datetime
    login_time: str = ""

    def auth_header(self) -> str:
        return f"token {self.api_key}:{self.access_token.reveal()}"

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def to_storable(self) -> dict[str, Any]:
        """The document encrypted at rest. Includes the token by necessity."""

        return {
            "api_key": self.api_key,
            "access_token": self.access_token.reveal(),
            "user_id": self.user_id,
            "user_name": self.user_name,
            "email": self.email,
            "expires_at": self.expires_at.isoformat(),
            "login_time": self.login_time,
        }

    @classmethod
    def from_storable(cls, payload: dict[str, Any]) -> ZerodhaSession:
        return cls(
            api_key=str(payload["api_key"]),
            access_token=SecretString(str(payload["access_token"])),
            user_id=str(payload.get("user_id", "")),
            user_name=str(payload.get("user_name", "")),
            email=str(payload.get("email", "")),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            login_time=str(payload.get("login_time", "")),
        )

    def __repr__(self) -> str:
        return f"ZerodhaSession(api_key={self.api_key!r}, user_id={self.user_id!r})"


class ZerodhaClient:
    """The effectful HTTP boundary. One method per documented endpoint.

    Separate from :class:`ZerodhaAdapter` on purpose: the adapter is pure (it
    maps AlphaLab state to calls and back), and everything that touches a socket
    is here, so the adapter is testable without a network and this class is
    testable against a local server.
    """

    def __init__(
        self,
        *,
        api_root: str = API_ROOT,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._root = api_root.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=httpx.Timeout(self._timeout))

    def _request(
        self,
        method: str,
        path: str,
        *,
        session: ZerodhaSession | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Kite-Version": KITE_VERSION}
        if session is not None:
            headers["Authorization"] = session.auth_header()

        owns_client = self._client is None
        client = self._http()
        try:
            response = client.request(
                method,
                f"{self._root}{path}",
                headers=headers,
                data=data,
                params=params,
            )
        except httpx.HTTPError as exc:
            # Unreachable and refused are different facts and must never be
            # conflated: one is retryable, the other means the order was seen.
            raise ZerodhaError(f"Zerodha could not be reached: {type(exc).__name__}") from exc
        finally:
            if owns_client:
                client.close()

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ZerodhaError(
                f"Zerodha returned a non-JSON response (HTTP {response.status_code}).",
                status_code=response.status_code,
            ) from exc

        if response.status_code >= 400 or body.get("status") != "success":
            raise ZerodhaError(
                str(body.get("message") or f"HTTP {response.status_code}"),
                status_code=response.status_code,
                error_type=str(body.get("error_type", "")),
            )
        payload: dict[str, Any] = body.get("data") or {}
        return payload

    # --- session ---------------------------------------------------------

    def exchange_request_token(
        self, *, api_key: str, api_secret: str, request_token: str
    ) -> ZerodhaSession:
        """``POST /session/token``. The only call that sees the API secret."""

        data = self._request(
            "POST",
            "/session/token",
            data={
                "api_key": api_key,
                "request_token": request_token,
                "checksum": session_checksum(api_key, request_token, api_secret),
            },
        )
        access_token = data.get("access_token")
        if not access_token:
            raise ZerodhaError("Zerodha did not return an access token.")
        return ZerodhaSession(
            api_key=api_key,
            access_token=SecretString(str(access_token)),
            user_id=str(data.get("user_id", "")),
            user_name=str(data.get("user_name", "")),
            email=str(data.get("email", "")),
            expires_at=token_expiry(),
            login_time=str(data.get("login_time", "")),
        )

    def logout(self, session: ZerodhaSession) -> None:
        """``DELETE /session/token``. Invalidates the token at the venue."""

        self._request(
            "DELETE",
            "/session/token",
            session=session,
            params={"api_key": session.api_key, "access_token": session.access_token.reveal()},
        )

    def profile(self, session: ZerodhaSession) -> dict[str, Any]:
        return self._request("GET", "/user/profile", session=session)

    def margins(self, session: ZerodhaSession) -> dict[str, Any]:
        return self._request("GET", "/user/margins", session=session)

    # --- orders ----------------------------------------------------------

    def place_order(
        self,
        session: ZerodhaSession,
        *,
        variety: str = "regular",
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        order_type: str,
        quantity: int,
        product: str,
        price: str | None = None,
        trigger_price: str | None = None,
        validity: str = "DAY",
        disclosed_quantity: int | None = None,
        tag: str | None = None,
    ) -> str:
        """``POST /orders/{variety}``. Returns the venue's ``order_id``.

        ``tag`` carries our own order identity (max 20 chars, per the docs), so a
        response lost in transit can be reconciled against ``GET /orders``
        instead of being resubmitted blindly.
        """

        body: dict[str, Any] = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "quantity": int(quantity),
            "product": product,
            "validity": validity,
        }
        if price is not None:
            body["price"] = price
        if trigger_price is not None:
            body["trigger_price"] = trigger_price
        if disclosed_quantity is not None:
            body["disclosed_quantity"] = int(disclosed_quantity)
        if tag:
            body["tag"] = tag[:20]

        data = self._request("POST", f"/orders/{variety}", session=session, data=body)
        order_id = data.get("order_id")
        if not order_id:
            raise ZerodhaError("Zerodha accepted the order but returned no order_id.")
        return str(order_id)

    def modify_order(
        self,
        session: ZerodhaSession,
        *,
        variety: str = "regular",
        order_id: str,
        quantity: int | None = None,
        price: str | None = None,
        trigger_price: str | None = None,
        order_type: str | None = None,
        validity: str | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if quantity is not None:
            body["quantity"] = int(quantity)
        if price is not None:
            body["price"] = price
        if trigger_price is not None:
            body["trigger_price"] = trigger_price
        if order_type is not None:
            body["order_type"] = order_type
        if validity is not None:
            body["validity"] = validity
        data = self._request("PUT", f"/orders/{variety}/{order_id}", session=session, data=body)
        return str(data.get("order_id", order_id))

    def cancel_order(
        self, session: ZerodhaSession, *, variety: str = "regular", order_id: str
    ) -> str:
        data = self._request("DELETE", f"/orders/{variety}/{order_id}", session=session)
        return str(data.get("order_id", order_id))

    def orders(self, session: ZerodhaSession) -> list[dict[str, Any]]:
        data = self._request("GET", "/orders", session=session)
        return list(data) if isinstance(data, list) else []

    def trades(self, session: ZerodhaSession) -> list[dict[str, Any]]:
        data = self._request("GET", "/trades", session=session)
        return list(data) if isinstance(data, list) else []

    def positions(self, session: ZerodhaSession) -> dict[str, Any]:
        return self._request("GET", "/portfolio/positions", session=session)

    def holdings(self, session: ZerodhaSession) -> list[dict[str, Any]]:
        data = self._request("GET", "/portfolio/holdings", session=session)
        return list(data) if isinstance(data, list) else []


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


@dataclass
class ZerodhaAdapter:
    """AlphaLab's :class:`BrokerProtocol`, implemented over Kite Connect.

    Pure in the protocol's sense: every method takes a
    :class:`~alphalab.broker.state.BrokerState` and returns the next one plus the
    events it produced. The adapter holds the session and the client — the
    *bindings* — and no order or position book of its own, which is what lets
    AlphaLab's reconciliation compare its book against the venue's rather than
    against a cache this object kept.

    The two dictionaries it does hold are not a book. ``_venue_ids`` maps our
    order identity to Kite's so a cancel can address the right order, and
    ``_seen_executions`` is the idempotency set ``apply_execution`` is required
    to maintain.
    """

    session: ZerodhaSession
    client: ZerodhaClient
    #: Product and variety are account-level trading decisions, not venue
    #: details, so they are configured rather than assumed. ``CNC`` is delivery
    #: equity; ``MIS`` would square off intraday, which is a different product
    #: and must never be chosen by default on someone's behalf.
    product: str = "CNC"
    variety: str = "regular"
    exchange: str = "NSE"
    #: OMS order id → venue order id, for cancel, modify and reconcile.
    _venue_ids: dict[str, str] = field(default_factory=dict)
    #: Venue execution ids already applied, so a redelivered fill is not counted
    #: twice. ``BrokerProtocol.apply_execution`` requires this idempotency, and
    #: Kite redelivers every trade on each ``GET /trades`` poll.
    _seen_executions: set[str] = field(default_factory=set)

    # --- connectivity ----------------------------------------------------

    def connect(
        self, state: BrokerState, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        """Prove the session works by reading the profile.

        Kite has no "connect" call — a session either authenticates a request or
        it does not. Reading ``/user/profile`` is the cheapest way to turn that
        into a yes/no before an order depends on it.
        """

        try:
            profile = self.client.profile(self.session)
        except ZerodhaError as exc:
            return (
                _with_status(state, ConnectionStatus.FAILED),
                (
                    BrokerDisconnected(
                        event_id=new_event_id(),
                        timestamp=timestamp,
                        broker_name=state.broker_name,
                        reason=str(exc),
                    ),
                ),
            )
        logger.info("Zerodha connected for user %s", profile.get("user_id", ""))
        return (
            _with_status(state, ConnectionStatus.CONNECTED),
            (
                BrokerConnected(
                    event_id=new_event_id(), timestamp=timestamp, broker_name=state.broker_name
                ),
            ),
        )

    def disconnect(
        self, state: BrokerState, reason: str, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        try:
            self.client.logout(self.session)
        except ZerodhaError as exc:
            # A failed logout still disconnects locally: the token expires at
            # 06:00 IST regardless, and refusing would strand the connection in
            # CONNECTED with no way back.
            logger.warning("Zerodha logout failed; disconnecting locally: %s", exc)
        return (
            _with_status(state, ConnectionStatus.DISCONNECTED),
            (
                BrokerDisconnected(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    broker_name=state.broker_name,
                    reason=reason,
                ),
            ),
        )

    def status(self, state: BrokerState) -> ConnectionStatus:
        """Current connectivity, with token expiry treated as disconnection.

        A session past 06:00 IST is dead whatever the stored state says, so
        reporting ``CONNECTED`` for it would be a stale state shown as live —
        which PHASE 14 forbids.
        """

        if self.session.is_expired():
            return ConnectionStatus.DISCONNECTED
        return state.connection_status

    def heartbeat(
        self, state: BrokerState, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        """Kite has no keep-alive; a cheap profile read is the honest substitute."""

        try:
            self.client.profile(self.session)
        except ZerodhaError as exc:
            return (
                _with_status(state, ConnectionStatus.FAILED),
                (
                    BrokerDisconnected(
                        event_id=new_event_id(),
                        timestamp=timestamp,
                        broker_name=state.broker_name,
                        reason=str(exc),
                    ),
                ),
            )
        return (
            replace(state, last_heartbeat=timestamp),
            (
                Heartbeat(
                    event_id=new_event_id(), timestamp=timestamp, broker_name=state.broker_name
                ),
            ),
        )

    # --- order flow ------------------------------------------------------

    def submit_order(
        self, state: BrokerState, order: BrokerOrder, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        """Place one order, tagging it with our OMS identity.

        ``tag`` carries ``oms_order_id`` (Kite allows 20 characters), so a
        response lost in transit can be reconciled against ``GET /orders``
        instead of being resubmitted blindly into a duplicate.
        """

        try:
            venue_id = self.client.place_order(
                self.session,
                variety=self.variety,
                tradingsymbol=order.symbol,
                exchange=self.exchange,
                transaction_type=_SIDE_TO_KITE[order.side.value],
                order_type=_kite_order_type(order),
                quantity=int(order.quantity),
                product=self.product,
                price=str(order.price) if order.order_type is OrderType.LIMIT else None,
                trigger_price=(
                    str(order.stop_price)
                    if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                    else None
                ),
                tag=order.oms_order_id[:20],
            )
        except ZerodhaError as exc:
            return (
                state,
                (
                    OrderRejected(
                        event_id=new_event_id(),
                        timestamp=timestamp,
                        broker_order_id=order.broker_order_id or order.oms_order_id,
                        reason=str(exc),
                    ),
                ),
            )

        self._venue_ids[order.oms_order_id] = venue_id
        accepted = replace(order, broker_order_id=venue_id, status=OrderStatus.ACCEPTED)
        return (
            replace(state, orders=state.orders.set(venue_id, accepted)),
            (
                OrderSubmitted(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    broker_order_id=venue_id,
                    oms_order_id=order.oms_order_id,
                ),
                OrderAccepted(
                    event_id=new_event_id(), timestamp=timestamp, broker_order_id=venue_id
                ),
            ),
        )

    def cancel_order(
        self, state: BrokerState, broker_order_id: str, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        venue_id = self._venue_ids.get(broker_order_id, broker_order_id)
        try:
            self.client.cancel_order(self.session, variety=self.variety, order_id=venue_id)
        except ZerodhaError as exc:
            return (
                state,
                (
                    OrderRejected(
                        event_id=new_event_id(),
                        timestamp=timestamp,
                        broker_order_id=venue_id,
                        reason=f"Cancel failed: {exc}",
                    ),
                ),
            )
        existing = state.orders.get(venue_id)
        orders = (
            state.orders.set(venue_id, replace(existing, status=OrderStatus.CANCELLED))
            if existing is not None
            else state.orders
        )
        return (
            replace(state, orders=orders),
            (
                OrderCancelled(
                    event_id=new_event_id(), timestamp=timestamp, broker_order_id=venue_id
                ),
            ),
        )

    def replace_order(
        self,
        state: BrokerState,
        broker_order_id: str,
        new_quantity: Decimal,
        new_price: Decimal,
        timestamp: float,
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        venue_id = self._venue_ids.get(broker_order_id, broker_order_id)
        try:
            self.client.modify_order(
                self.session,
                variety=self.variety,
                order_id=venue_id,
                quantity=int(new_quantity),
                price=str(new_price),
            )
        except ZerodhaError as exc:
            return (
                state,
                (
                    OrderRejected(
                        event_id=new_event_id(),
                        timestamp=timestamp,
                        broker_order_id=venue_id,
                        reason=f"Modify failed: {exc}",
                    ),
                ),
            )
        existing = state.orders.get(venue_id)
        orders = (
            state.orders.set(venue_id, replace(existing, quantity=new_quantity, price=new_price))
            if existing is not None
            else state.orders
        )
        return (
            replace(state, orders=orders),
            (
                OrderAccepted(
                    event_id=new_event_id(), timestamp=timestamp, broker_order_id=venue_id
                ),
            ),
        )

    def apply_execution(
        self, state: BrokerState, execution: BrokerExecution, timestamp: float
    ) -> tuple[BrokerState, tuple[BrokerEvent, ...]]:
        """Apply a fill the venue reported. Idempotent in ``execution_id``.

        The protocol requires this, and Kite makes it load-bearing rather than
        theoretical: ``GET /trades`` returns the whole day's trades on every
        poll, so without the guard each fill would be counted once per cycle.
        """

        if execution.execution_id in self._seen_executions:
            return state, ()
        self._seen_executions.add(execution.execution_id)
        return (
            replace(
                state,
                executions=state.executions.set(execution.execution_id, execution),
            ),
            (
                ExecutionReceived(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    execution_id=execution.execution_id,
                    broker_order_id=execution.broker_order_id,
                    fill_quantity=execution.fill_quantity,
                    fill_price=execution.fill_price,
                ),
            ),
        )

    def order_status(self, state: BrokerState, broker_order_id: str) -> BrokerOrder | None:
        venue_id = self._venue_ids.get(broker_order_id, broker_order_id)
        try:
            rows = self.client.orders(self.session)
        except ZerodhaError:
            return None
        for row in rows:
            if str(row.get("order_id")) == venue_id:
                return _order_from_kite(row)
        return None

    def account(self, state: BrokerState) -> AlphaBrokerAccount:
        """The account snapshot, from ``/user/margins``.

        Every figure is Kite's own. A cash balance this application derived would
        be a second answer to a question the venue already answers, and the two
        would disagree the first time a charge landed between polls.
        """

        margins = self.client.margins(self.session)
        equity = margins.get("equity") or {}
        available = equity.get("available") or {}
        utilised = equity.get("utilised") or {}
        live = _decimal(available.get("live_balance", available.get("cash")))
        used = _decimal(utilised.get("debits", utilised.get("exposure")))
        return AlphaBrokerAccount(
            account_id=self.session.user_id or state.broker_name,
            cash=live,
            equity=_decimal(equity.get("net", live)),
            buying_power=live,
            margin=used,
            available_funds=live,
            currency="INR",
            broker_id=state.broker_name,
        )

    def positions(self, state: BrokerState) -> Sequence[BrokerPosition]:
        """Net positions from ``/portfolio/positions``.

        The ``net`` set, not ``day``: ``net`` is the position actually carried,
        and that is what reconciliation must compare AlphaLab's book against.
        """

        payload = self.client.positions(self.session)
        rows = payload.get("net") or []
        out: list[BrokerPosition] = []
        for row in rows:
            quantity = _decimal(row.get("quantity"))
            if quantity == 0:
                continue
            last = _decimal(row.get("last_price"))
            out.append(
                BrokerPosition(
                    symbol=str(row.get("tradingsymbol", "")),
                    quantity=quantity,
                    average_price=_decimal(row.get("average_price")),
                    market_value=quantity * last,
                    unrealized_pnl=_decimal(row.get("unrealised", row.get("pnl"))),
                    realized_pnl=_decimal(row.get("realised")),
                    account_id=self.session.user_id,
                    market_price=last,
                )
            )
        return out

    # --- reconciliation input -------------------------------------------

    def executions_since(self, seen: set[str] | None = None) -> list[BrokerExecution]:
        """Trades the venue reports, as AlphaLab executions.

        Polling ``GET /trades`` is what this connector has. Kite's websocket
        carries quotes rather than order updates, and the postback webhook needs
        a publicly reachable URL this deployment does not assume; both are
        recorded as deferred in ``docs/BROKERS.md`` rather than stubbed here.
        """

        already = seen if seen is not None else self._seen_executions
        out: list[BrokerExecution] = []
        for row in self.client.trades(self.session):
            trade_id = str(row.get("trade_id", ""))
            if not trade_id or trade_id in already:
                continue
            out.append(
                BrokerExecution(
                    execution_id=trade_id,
                    broker_order_id=str(row.get("order_id", "")),
                    symbol=str(row.get("tradingsymbol", "")),
                    fill_quantity=_decimal(row.get("quantity")),
                    fill_price=_decimal(row.get("average_price", row.get("price"))),
                    # Kite does not itemise brokerage on a trade; charges arrive
                    # in the contract note. Reporting zero is the honest reading
                    # of "not stated here" — inventing an estimate would put a
                    # number AlphaLab's accounting would then treat as real.
                    commission=Decimal("0"),
                    timestamp=0.0,
                    account_id=self.session.user_id,
                    external_id=str(row.get("exchange_order_id", "")),
                )
            )
        return out


def _with_status(state: BrokerState, status: ConnectionStatus) -> BrokerState:
    return replace(state, connection_status=status)


def _kite_order_type(order: BrokerOrder) -> str:
    return {
        OrderType.MARKET: "MARKET",
        OrderType.LIMIT: "LIMIT",
        OrderType.STOP: "SL-M",
        OrderType.STOP_LIMIT: "SL",
    }[order.order_type]


def _order_from_kite(row: dict[str, Any]) -> BrokerOrder:
    """One Kite order row as an AlphaLab ``BrokerOrder``.

    An unrecognised status becomes ``PENDING`` rather than ``ACCEPTED``: if Kite
    adds a state this table does not know, treating the order as not-yet-working
    is the safe reading, and treating it as working is the one that loses money.
    """

    return BrokerOrder(
        broker_order_id=str(row.get("order_id", "")),
        oms_order_id=str(row.get("tag") or ""),
        symbol=str(row.get("tradingsymbol", "")),
        side=Side.BUY if str(row.get("transaction_type", "")).upper() == "BUY" else Side.SELL,
        order_type=(
            OrderType.LIMIT
            if str(row.get("order_type", "")).upper() == "LIMIT"
            else OrderType.MARKET
        ),
        quantity=_decimal(row.get("quantity")),
        price=_decimal(row.get("price")),
        filled_quantity=_decimal(row.get("filled_quantity")),
        average_fill_price=_decimal(row.get("average_price")),
        status=_STATUS_TO_ALPHALAB.get(str(row.get("status", "")).upper(), OrderStatus.PENDING),
        created_at=0.0,
        updated_at=0.0,
        tif=TimeInForce.DAY,
        stop_price=_decimal(row.get("trigger_price")),
    )
