"""Strategy identity, versions and the implementations this deployment offers."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import strategies as implementations
from iluvtrade.api.deps import current_principal, db_session, require_trader
from iluvtrade.api.v1.schemas import (
    CreateStrategyRequest,
    CreateVersionRequest,
    StrategyResponse,
    StrategyVersionResponse,
    UpdateVersionRequest,
)
from iluvtrade.db.models.strategy import Strategy, StrategyVersion, StrategyVisibility
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped
from iluvtrade.strategies import service

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _version(version: StrategyVersion) -> StrategyVersionResponse:
    return StrategyVersionResponse(
        id=version.id,
        strategy_id=version.strategy_id,
        version=version.version,
        status=version.status.value,
        implementation_key=version.implementation_key,
        default_parameters=json.loads(version.default_parameters_json or "{}"),
        parameters_schema=json.loads(version.parameters_schema_json or "{}"),
        changelog=version.changelog,
        content_hash=version.content_hash,
        frozen=version.frozen,
        certification_status=version.certification_status.value,
        published_at=version.published_at,
        integrity_ok=service.verify_integrity(version),
    )


def _strategy(strategy: Strategy) -> StrategyResponse:
    return StrategyResponse(
        id=strategy.id,
        slug=strategy.slug,
        name=strategy.name,
        description=strategy.description,
        visibility=strategy.visibility.value,
        owner_user_id=strategy.owner_user_id,
        created_at=strategy.created_at,
        versions=[_version(v) for v in strategy.versions],
    )


@router.get("/implementations")
def list_implementations(_principal: Principal = Depends(current_principal)) -> list[dict]:
    """Every strategy implementation this deployment can run.

    A creator picks one of these; uploading arbitrary Python is deliberately not
    a feature. See :mod:`iluvtrade.alphalab_bridge.strategies`.
    """

    return implementations.describe()


@router.get("", response_model=list[StrategyResponse])
def list_strategies(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[StrategyResponse]:
    rows = session.execute(
        scoped(Strategy, principal.organization_id).order_by(Strategy.created_at.desc())
    ).scalars()
    return [_strategy(row) for row in rows]


@router.post("", response_model=StrategyResponse, status_code=201)
def create_strategy(
    payload: CreateStrategyRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> StrategyResponse:
    strategy = service.create_strategy(
        session,
        principal,
        name=payload.name,
        description=payload.description,
        visibility=StrategyVisibility(payload.visibility),
    )
    return _strategy(strategy)


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> StrategyResponse:
    return _strategy(require_owned(session, Strategy, strategy_id, principal.organization_id))


@router.post("/{strategy_id}/versions", response_model=StrategyVersionResponse, status_code=201)
def create_version(
    strategy_id: str,
    payload: CreateVersionRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> StrategyVersionResponse:
    return _version(
        service.create_version(
            session,
            principal,
            strategy_id=strategy_id,
            implementation_key=payload.implementation_key,
            parameters=payload.parameters,
            changelog=payload.changelog,
        )
    )


@router.patch("/versions/{version_id}", response_model=StrategyVersionResponse)
def update_version(
    version_id: str,
    payload: UpdateVersionRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> StrategyVersionResponse:
    """Edit a draft. A published version is frozen and returns 409."""

    return _version(
        service.update_draft(
            session,
            principal,
            version_id=version_id,
            implementation_key=payload.implementation_key,
            parameters=payload.parameters,
            changelog=payload.changelog,
        )
    )


@router.post("/versions/{version_id}/publish", response_model=StrategyVersionResponse)
def publish_version(
    version_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> StrategyVersionResponse:
    """Freeze a version. Its behaviour can never change afterwards."""

    return _version(service.publish(session, principal, version_id))


@router.get("/versions/{version_id}", response_model=StrategyVersionResponse)
def get_version(
    version_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> StrategyVersionResponse:
    return _version(require_owned(session, StrategyVersion, version_id, principal.organization_id))
