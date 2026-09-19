"""Application settings, resolved from the environment.

Secrets are read from the environment and never committed. ``.env.example``
documents every variable; ``.env`` is git-ignored.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Every knob the application reads, in one typed object."""

    model_config = SettingsConfigDict(
        env_prefix="ILUVTRADE_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"

    # --- persistence -----------------------------------------------------
    database_url: str = Field(default=f"sqlite:///{REPO_ROOT / 'var' / 'iluvtrade.db'}")
    #: Blob storage root. Raw uploads and canonical datasets are files, not rows
    #: (PHASE 17: market data does not belong in transactional tables).
    storage_root: Path = Field(default=REPO_ROOT / "var" / "storage")

    # --- security --------------------------------------------------------
    #: Used to derive the session-token HMAC and to encrypt broker credentials.
    #: Generated per-process when unset, which logs out every session on restart
    #: — correct for development, and refused outright in production below.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    session_ttl_seconds: int = 60 * 60 * 12
    #: Argon2id parameters. Deliberately explicit rather than library defaults,
    #: so a change is a reviewable diff.
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 64 * 1024
    argon2_parallelism: int = 4

    # --- ingestion limits (PHASE 4 security requirements) ----------------
    max_upload_bytes: int = 256 * 1024 * 1024
    max_fetch_bytes: int = 64 * 1024 * 1024
    fetch_timeout_seconds: float = 20.0
    fetch_max_redirects: int = 3
    #: Hostnames a dataset may be fetched from. Empty means no host is allowed:
    #: an allowlist that defaults to "everything" is not an allowlist.
    fetch_allowed_hosts: tuple[str, ...] = ()
    #: Loopback is refused by default. Tests enable it explicitly.
    fetch_allow_loopback: bool = False
    #: Ports a data source may be fetched from. Narrow by default so an
    #: allowlisted host cannot be used to reach an unrelated service on it
    #: (an SSH port, a database, an internal admin panel). Configurable
    #: because a real deployment may front its data on another port, and a
    #: control nobody can adjust gets disabled wholesale instead.
    fetch_allowed_ports: tuple[int, ...] = (80, 443, 8000, 8080, 8443)

    # --- jobs ------------------------------------------------------------
    backtest_worker_count: int = 2
    backtest_timeout_seconds: float = 300.0
    backtest_max_attempts: int = 1

    # --- trading ---------------------------------------------------------
    #: Live trading is off unless the deployment turns it on *and* the user
    #: enables it *and* confirms each session. Three independent gates.
    live_trading_enabled: bool = False

    # --- brokers ---------------------------------------------------------
    zerodha_api_key: str | None = None
    zerodha_api_secret: str | None = None

    @field_validator("storage_root")
    @classmethod
    def _ensure_storage_root(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object."""

    settings = Settings()
    if settings.is_production and not settings.secret_key:
        raise RuntimeError("ILUVTRADE_SECRET_KEY must be set in production.")
    return settings
