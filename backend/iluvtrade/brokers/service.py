"""Broker account and connection lifecycle.

The rule this module keeps: **a credential never leaves the server.** It arrives
once, in the request that authorizes a connection; it is encrypted immediately;
and no function here returns it. What callers get back is
:class:`ConnectionView`, which carries state, identity and a four-character hint
and nothing else — so an API route cannot leak a token by serializing whatever
it was handed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session as DbSession

from iluvtrade.brokers import zerodha
from iluvtrade.brokers.crypto import CredentialError, decrypt_credentials, encrypt_credentials
from iluvtrade.config import get_settings
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.broker import (
    BrokerAccount,
    BrokerConnection,
    BrokerKind,
    ConnectionState,
)
from iluvtrade.db.models.platform import Role
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped

__all__ = [
    "BrokerError",
    "ConnectionView",
    "authorize_zerodha",
    "begin_zerodha_authorization",
    "create_account",
    "disconnect",
    "list_accounts",
    "view",
]

#: A connection may be re-authorized this many times before it is refused, to
#: blunt a credential-stuffing loop against the venue.
MAX_AUTHORIZATION_ATTEMPTS = 20


class BrokerError(ValueError):
    """The broker operation was refused. Never carries a credential."""


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """Everything about a connection that is safe to send to a browser."""

    account_id: str
    broker: str
    label: str
    state: str
    venue_account_id: str | None
    venue_user_name: str | None
    base_currency: str
    credential_hint: str | None
    token_expires_at: datetime | None
    connected_at: datetime | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    #: Whether this connector has ever been exercised against the real venue.
    #: Surfaced so the UI can say so rather than implying a proven integration.
    verified_against_live_venue: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "broker": self.broker,
            "label": self.label,
            "state": self.state,
            "venue_account_id": self.venue_account_id,
            "venue_user_name": self.venue_user_name,
            "base_currency": self.base_currency,
            "credential_hint": self.credential_hint,
            "token_expires_at": (
                self.token_expires_at.isoformat() if self.token_expires_at else None
            ),
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "last_error": self.last_error,
            "verified_against_live_venue": self.verified_against_live_venue,
        }


def view(account: BrokerAccount, connection: BrokerConnection | None) -> ConnectionView:
    """Project an account and its connection into the safe shape."""

    return ConnectionView(
        account_id=account.id,
        broker=account.broker.value,
        label=account.label,
        state=(connection.state.value if connection else ConnectionState.DISCONNECTED.value),
        venue_account_id=account.venue_account_id,
        venue_user_name=account.venue_user_name,
        base_currency=account.base_currency,
        credential_hint=connection.credential_hint if connection else None,
        token_expires_at=connection.token_expires_at if connection else None,
        connected_at=connection.connected_at if connection else None,
        last_heartbeat_at=connection.last_heartbeat_at if connection else None,
        last_error=connection.last_error if connection else None,
        verified_against_live_venue=(
            False if account.broker is BrokerKind.ZERODHA else account.broker is BrokerKind.PAPER
        ),
    )


def create_account(
    session: DbSession,
    principal: Principal,
    *,
    broker: BrokerKind,
    label: str,
    base_currency: str = "INR",
) -> BrokerAccount:
    """Register a broker account for this organization. No credentials yet."""

    principal.require(Role.TRADER)
    existing = session.execute(
        scoped(BrokerAccount, principal.organization_id).where(
            BrokerAccount.broker == broker, BrokerAccount.label == label
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BrokerError(f"A {broker.value} account labelled {label!r} already exists.")

    account = BrokerAccount(
        organization_id=principal.organization_id,
        broker=broker,
        label=label.strip() or broker.value,
        base_currency=base_currency,
        created_by_user_id=principal.user_id,
    )
    session.add(account)
    session.flush()
    session.add(
        BrokerConnection(
            organization_id=principal.organization_id,
            broker_account_id=account.id,
            state=(
                ConnectionState.CONNECTED
                if broker is BrokerKind.PAPER
                else ConnectionState.DISCONNECTED
            ),
            connected_at=utcnow() if broker is BrokerKind.PAPER else None,
        )
    )
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="broker.account.created",
        resource_type="broker_account",
        resource_id=account.id,
        actor_user_id=principal.user_id,
        payload={"broker": broker.value, "label": label},
    )
    session.flush()
    return account


def list_accounts(session: DbSession, principal: Principal) -> list[ConnectionView]:
    accounts = session.execute(
        scoped(BrokerAccount, principal.organization_id).order_by(BrokerAccount.created_at)
    ).scalars()
    return [view(account, account.connection) for account in accounts]


def begin_zerodha_authorization(session: DbSession, principal: Principal, account_id: str) -> str:
    """Return the Kite login URL the user's browser is sent to.

    The API **key** is public and appears in this URL by design. The API
    **secret** stays on the server and is only used to compute the session
    checksum in :func:`authorize_zerodha`.
    """

    principal.require(Role.TRADER)
    settings = get_settings()
    if not settings.zerodha_api_key or not settings.zerodha_api_secret:
        raise BrokerError(
            "This deployment has no Zerodha API credentials configured. An administrator "
            "must set ILUVTRADE_ZERODHA_API_KEY and ILUVTRADE_ZERODHA_API_SECRET, which "
            "requires a Kite Connect subscription and a registered app."
        )
    account = require_owned(session, BrokerAccount, account_id, principal.organization_id)
    if account.broker is not BrokerKind.ZERODHA:
        raise BrokerError("That account is not a Zerodha account.")

    connection = account.connection
    if connection is not None:
        connection.state = ConnectionState.AUTHORIZING
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="broker.authorization.started",
        resource_type="broker_account",
        resource_id=account.id,
        actor_user_id=principal.user_id,
    )
    return zerodha.login_url(settings.zerodha_api_key)


def authorize_zerodha(
    session: DbSession,
    principal: Principal,
    *,
    account_id: str,
    request_token: str,
    client: zerodha.ZerodhaClient | None = None,
) -> ConnectionView:
    """Exchange a ``request_token`` for an access token and store it encrypted.

    ``request_token`` is single-use and short-lived, which is why it is safe to
    carry in a redirect query string and the access token never would be.
    """

    principal.require(Role.TRADER)
    settings = get_settings()
    if not settings.zerodha_api_key or not settings.zerodha_api_secret:
        raise BrokerError("This deployment has no Zerodha API credentials configured.")

    account = require_owned(session, BrokerAccount, account_id, principal.organization_id)
    connection = account.connection
    if connection is None:
        raise BrokerError("That account has no connection record.")
    if connection.authorization_attempts >= MAX_AUTHORIZATION_ATTEMPTS:
        raise BrokerError(
            "This connection has been re-authorized too many times. Create a new broker "
            "account, or contact support."
        )

    connection.authorization_attempts += 1
    try:
        venue_session = (client or zerodha.ZerodhaClient()).exchange_request_token(
            api_key=settings.zerodha_api_key,
            api_secret=settings.zerodha_api_secret,
            request_token=request_token,
        )
    except zerodha.ZerodhaError as exc:
        connection.state = ConnectionState.ERROR
        connection.last_error = str(exc)[:1000]
        audit.record(
            session,
            organization_id=principal.organization_id,
            action="broker.authorization.failed",
            resource_type="broker_account",
            resource_id=account.id,
            actor_user_id=principal.user_id,
            outcome="failure",
            payload={"reason": str(exc)},
        )
        raise BrokerError(f"Zerodha refused the authorization: {exc}") from exc

    connection.encrypted_credentials = encrypt_credentials(
        venue_session.to_storable(), connection_id=connection.id
    )
    connection.credential_hint = venue_session.access_token.hint
    connection.token_expires_at = venue_session.expires_at
    connection.state = ConnectionState.CONNECTED
    connection.connected_at = utcnow()
    connection.last_error = None
    account.venue_account_id = venue_session.user_id or None
    account.venue_user_name = venue_session.user_name or None

    audit.record(
        session,
        organization_id=principal.organization_id,
        action="broker.connected",
        resource_type="broker_account",
        resource_id=account.id,
        actor_user_id=principal.user_id,
        payload={
            "broker": "zerodha",
            "venue_account_id": venue_session.user_id,
            "token_expires_at": venue_session.expires_at.isoformat(),
        },
    )
    session.flush()
    return view(account, connection)


def load_session(
    session: DbSession, organization_id: str, account_id: str
) -> zerodha.ZerodhaSession:
    """Decrypt a stored Zerodha session, refusing an expired or absent one."""

    account = require_owned(session, BrokerAccount, account_id, organization_id)
    connection = account.connection
    if connection is None or not connection.encrypted_credentials:
        raise BrokerError("That broker account is not connected.")
    if connection.state is not ConnectionState.CONNECTED:
        raise BrokerError(f"That broker connection is {connection.state.value}.")

    try:
        payload = decrypt_credentials(connection.encrypted_credentials, connection_id=connection.id)
    except CredentialError as exc:
        connection.state = ConnectionState.ERROR
        connection.last_error = str(exc)[:1000]
        raise BrokerError(str(exc)) from exc

    venue_session = zerodha.ZerodhaSession.from_storable(payload)
    if venue_session.is_expired():
        connection.state = ConnectionState.EXPIRED
        notifications.notify(
            session,
            organization_id=organization_id,
            kind="broker.token_expired",
            severity="warning",
            title="Broker session expired",
            body=(
                f"The Zerodha session for {account.label} expired at "
                f"{venue_session.expires_at.isoformat()}. Zerodha invalidates access tokens "
                "at 06:00 IST daily; reconnect to continue trading."
            ),
            resource_type="broker_account",
            resource_id=account.id,
        )
        raise BrokerError(
            "The Zerodha session has expired. Zerodha invalidates access tokens at 06:00 "
            "IST every day; reconnect the account."
        )
    return venue_session


def disconnect(
    session: DbSession, principal: Principal, account_id: str, *, reason: str = "user requested"
) -> ConnectionView:
    """Revoke a connection and destroy the stored credential."""

    principal.require(Role.TRADER)
    account = require_owned(session, BrokerAccount, account_id, principal.organization_id)
    connection = account.connection
    if connection is None:
        raise BrokerError("That account has no connection record.")

    if account.broker is BrokerKind.ZERODHA and connection.encrypted_credentials:
        try:
            venue_session = load_session(session, principal.organization_id, account_id)
            zerodha.ZerodhaClient().logout(venue_session)
        except (BrokerError, zerodha.ZerodhaError):
            # Telling the venue is best-effort; forgetting the credential is not.
            # A failure here must not leave the token in our database.
            pass

    connection.encrypted_credentials = None
    connection.credential_hint = None
    connection.token_expires_at = None
    connection.state = ConnectionState.REVOKED
    connection.connected_at = None
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="broker.disconnected",
        resource_type="broker_account",
        resource_id=account.id,
        actor_user_id=principal.user_id,
        payload={"reason": reason},
    )
    session.flush()
    return view(account, connection)
