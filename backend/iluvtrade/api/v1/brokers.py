"""Broker accounts and the authorization flow.

No route here returns a credential. Responses are built from
:class:`~iluvtrade.brokers.service.ConnectionView`, which has no field capable of
carrying one — ``tests/security/test_broker_secrets.py`` asserts that against
every response body.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session, rate_limit, require_trader
from iluvtrade.api.v1.schemas import AuthorizeZerodhaRequest, CreateBrokerAccountRequest
from iluvtrade.brokers import service
from iluvtrade.brokers.zerodha import VERIFIED_AGAINST_LIVE_VENUE
from iluvtrade.db.models.broker import BrokerAccount, BrokerKind
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned

router = APIRouter(prefix="/brokers", tags=["brokers"])


@router.get("/supported")
def supported(_principal: Principal = Depends(current_principal)) -> list[dict]:
    """Which venues this deployment has a connector for, and how real each is.

    ``verified_against_live_venue`` is surfaced so the UI can tell the truth
    about the Zerodha connector rather than implying a proven integration.
    """

    return [
        {
            "broker": BrokerKind.PAPER.value,
            "name": "Paper (AlphaLab simulator)",
            "verified_against_live_venue": True,
            "notes": "AlphaLab's own execution simulator. Fully exercised locally.",
        },
        {
            "broker": BrokerKind.ZERODHA.value,
            "name": "Zerodha (Kite Connect v3)",
            "verified_against_live_venue": VERIFIED_AGAINST_LIVE_VENUE,
            "notes": (
                "Implemented to the published Kite Connect v3 contracts and tested against "
                "a local server speaking the same protocol. It has never been run against "
                "Zerodha itself, which needs a Kite Connect subscription and a registered "
                "app. Access tokens expire at 06:00 IST daily."
            ),
        },
    ]


@router.get("")
def list_accounts(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return [view.to_dict() for view in service.list_accounts(session, principal)]


@router.post("", status_code=201)
def create_account(
    payload: CreateBrokerAccountRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    account = service.create_account(
        session,
        principal,
        broker=BrokerKind(payload.broker),
        label=payload.label,
        base_currency=payload.base_currency,
    )
    session.flush()
    return service.view(account, account.connection).to_dict()


@router.post(
    "/{account_id}/zerodha/login-url",
    dependencies=[Depends(rate_limit("broker_auth"))],
)
def zerodha_login_url(
    account_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    """The Kite URL to send the user's browser to.

    The API key is public and belongs in this URL. The API secret never leaves
    the server and is used only to sign the session exchange.
    """

    return {"login_url": service.begin_zerodha_authorization(session, principal, account_id)}


@router.post(
    "/{account_id}/zerodha/authorize",
    dependencies=[Depends(rate_limit("broker_auth"))],
)
def authorize_zerodha(
    account_id: str,
    payload: AuthorizeZerodhaRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    """Exchange the one-time ``request_token`` for an access token.

    The access token is encrypted before it is stored and is never returned.
    """

    view = service.authorize_zerodha(
        session, principal, account_id=account_id, request_token=payload.request_token
    )
    return view.to_dict()


@router.post("/{account_id}/disconnect")
def disconnect(
    account_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    """Revoke the connection and destroy the stored credential."""

    return service.disconnect(session, principal, account_id).to_dict()


@router.get("/{account_id}")
def get_account(
    account_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    account = require_owned(session, BrokerAccount, account_id, principal.organization_id)
    return service.view(account, account.connection).to_dict()
