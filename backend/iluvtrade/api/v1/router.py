"""Every v1 router, mounted under one prefix."""

from fastapi import APIRouter

from iluvtrade.api.v1 import (
    auth,
    backtests,
    brokers,
    datasets,
    organizations,
    portfolio,
    reddesk,
    strategies,
    trading,
)

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth.router)
api_v1.include_router(organizations.router)
api_v1.include_router(datasets.router)
api_v1.include_router(strategies.router)
api_v1.include_router(backtests.router)
api_v1.include_router(reddesk.router)
api_v1.include_router(brokers.router)
api_v1.include_router(trading.router)
api_v1.include_router(portfolio.router)

__all__ = ["api_v1"]
