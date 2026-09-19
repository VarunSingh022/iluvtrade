"""Application settings, resolved from the environment.

Secrets are read from the environment and never committed. ``.env.example``
documents every variable; ``.env`` is git-ignored.

Three environments, one object
------------------------------

``development``, ``test`` and ``production`` are the same :class:`Settings`
class with different values — not three code paths — so there is no setting
that exists in one environment and is unreachable in another.

What separates them is :meth:`Settings.deployment_problems`, which enumerates
the combinations that are unsafe *in production* and is called at startup.
A production deployment that is misconfigured fails to start. It does not
start with a warning: a warning in a log nobody reads is how an application
ends up serving traffic with a per-process secret key, logging everyone out on
every restart and leaving every stored broker credential unreadable.

Development and test are deliberately permissive — an ephemeral secret key is
*correct* there — so the same defaults that make a laptop work are the ones
production refuses.
"""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The shortest secret key production accepts. 32 characters of
#: ``secrets.token_urlsafe`` is ~192 bits; anything a human typed by hand is
#: almost certainly shorter and is refused rather than silently accepted.
MIN_PRODUCTION_SECRET_LENGTH = 32

#: Values that look like a secret but are a placeholder someone forgot to
#: replace. Matched case-insensitively against the whole key.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me",
        "changeme",
        "secret",
        "development",
        "dev",
        "test",
        "please-change",
        "your-secret-key",
        "xxx",
    }
)


def _csv(value: Any) -> Any:
    """Accept ``a,b,c`` for a tuple field, as the documentation promises.

    pydantic-settings parses a complex field from the environment as JSON. That
    makes ``ILUVTRADE_FETCH_ALLOWED_HOSTS=data.example.com`` a startup crash
    rather than a setting — and the variable it breaks is the SSRF allowlist,
    which is the one an operator most needs to be able to set. JSON is still
    accepted, so an existing ``["a","b"]`` keeps working.
    """

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            return json.loads(text)
        return tuple(part.strip() for part in text.split(",") if part.strip())
    return value


class DeploymentProblem(ValueError):
    """One production setting, or combination, that is not safe to run.

    Carries the variable an operator has to change, because "configuration is
    invalid" without naming the knob is an error message that wastes an hour.
    """

    def __init__(self, variable: str, message: str) -> None:
        super().__init__(f"{variable}: {message}")
        self.variable = variable
        self.message = message


class UnsafeProductionConfiguration(RuntimeError):
    """Production was asked to start with one or more unsafe settings."""

    def __init__(self, problems: list[DeploymentProblem]) -> None:
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"Refusing to start: {len(problems)} unsafe production setting(s).\n{detail}\n"
            "Each is deliberate — see docs/DEPLOYMENT.md. Nothing here can be "
            "overridden by lowering ILUVTRADE_ENVIRONMENT to development while "
            "serving real users."
        )
        self.problems = problems


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
    #: Acknowledge that this production deployment runs on SQLite.
    #:
    #: SQLite is refused in production by default, and the reason is specific to
    #: this application rather than general distaste: the backtest worker pool
    #: and every running trading session are threads writing concurrently, which
    #: is exactly the shape that produces ``database is locked``. A single-node
    #: deployment with light concurrency can still be a reasonable choice — so
    #: the escape hatch exists, and setting it is a statement that the operator
    #: knows which trade they made.
    allow_sqlite_in_production: bool = False

    # --- security --------------------------------------------------------
    #: Used to derive the session-token HMAC and to encrypt broker credentials.
    #: Generated per-process when unset, which logs out every session on restart
    #: — correct for development, and refused outright in production below.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    #: Previous secrets that may still *decrypt* stored credentials. Nothing is
    #: ever encrypted under one. A rotation is: append the old key here, set the
    #: new ``secret_key``, restart, rotate, then remove the old key — in that
    #: order, because removing it first makes every stored credential unreadable.
    retired_secret_keys: Annotated[tuple[str, ...], NoDecode] = ()
    session_ttl_seconds: int = 60 * 60 * 12
    #: Argon2id parameters. Deliberately explicit rather than library defaults,
    #: so a change is a reviewable diff.
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 64 * 1024
    argon2_parallelism: int = 4

    # --- HTTP ------------------------------------------------------------
    #: Browser origins allowed to call this API cross-origin *with credentials*.
    #:
    #: The default is the Vite dev server, which is what a developer needs and
    #: what production must not have: production is checked below for
    #: plaintext origins, so a deployment that leaves this untouched fails to
    #: start rather than allowing an http:// origin to carry session cookies.
    #: A deployment serving the built frontend from this same process wants
    #: this empty — same-origin needs no CORS at all.
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    # --- ingestion limits (PHASE 4 security requirements) ----------------
    max_upload_bytes: int = 256 * 1024 * 1024
    max_fetch_bytes: int = 64 * 1024 * 1024
    fetch_timeout_seconds: float = 20.0
    fetch_max_redirects: int = 3
    #: Hostnames a dataset may be fetched from. Empty means no host is allowed:
    #: an allowlist that defaults to "everything" is not an allowlist.
    fetch_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    #: Loopback is refused by default. Tests enable it explicitly.
    fetch_allow_loopback: bool = False
    #: Ports a data source may be fetched from. Narrow by default so an
    #: allowlisted host cannot be used to reach an unrelated service on it
    #: (an SSH port, a database, an internal admin panel). Configurable
    #: because a real deployment may front its data on another port, and a
    #: control nobody can adjust gets disabled wholesale instead.
    fetch_allowed_ports: Annotated[tuple[int, ...], NoDecode] = (80, 443, 8000, 8080, 8443)

    #: Whether the application creates tables at startup.
    #:
    #: ``create_all`` is right for development and tests and **wrong** for
    #: anything holding data: it creates what is missing and silently ignores
    #: what has drifted, so a column added to a model never reaches an existing
    #: database and the mismatch surfaces as a query error later. Production
    #: runs ``alembic upgrade head`` instead, and this is forced off there.
    auto_create_tables: bool = True

    # --- observability -----------------------------------------------------
    log_level: str = "INFO"
    #: JSON log lines. On by default because these are meant to be aggregated;
    #: a developer reading a terminal can turn it off.
    structured_logging: bool = True

    # --- billing -----------------------------------------------------------
    #: Which payment provider settles marketplace purchases.
    #:
    #: ``manual`` records a purchase without charging anything, which is what a
    #: deployment with no merchant account can honestly do. It is the default
    #: because the alternative — defaulting to a real provider that is not
    #: configured — would fail every purchase. The UI reads
    #: ``GET /reddesk/billing-status`` and says which is in play.
    payment_provider: str = "manual"

    # --- rate limiting -----------------------------------------------------
    #: On by default. A deployment turns it off only deliberately; the test
    #: suite turns it off per-test so unrelated tests are not throttled, and the
    #: rate-limit tests turn it back on explicitly.
    rate_limit_enabled: bool = True
    #: Per-policy overrides, as ``name=limit/window_seconds`` entries:
    #: ``login=5/60,backtest=20/60``. Only names already in ``POLICIES`` are
    #: accepted — an unknown name is a typo that would otherwise silently
    #: enforce nothing.
    rate_limit_overrides: Annotated[tuple[str, ...], NoDecode] = ()
    #: Set when a proxy or gateway in front of this application enforces the
    #: limits instead. Required to disable in-process limiting in production:
    #: turning the control off has to be a statement about where it moved, not
    #: a quiet omission.
    rate_limit_enforced_externally: bool = False

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

    _CSV_FIELDS = (
        "retired_secret_keys",
        "allowed_origins",
        "fetch_allowed_hosts",
        "fetch_allowed_ports",
        "rate_limit_overrides",
    )

    @field_validator(*_CSV_FIELDS, mode="before")
    @classmethod
    def _accept_csv(cls, value: Any) -> Any:
        return _csv(value)

    @field_validator("storage_root")
    @classmethod
    def _ensure_storage_root(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def secret_key_was_generated(self) -> bool:
        """Whether the key was invented for this process rather than configured.

        Inspecting the value cannot answer this: ``secret_key`` has a
        ``default_factory``, so an unset key is a perfectly valid random
        string. ``model_fields_set`` is what distinguishes them — it holds the
        fields that any source actually supplied, whether that was an
        environment variable, the ``.env`` file, or a constructor argument.
        """

        return "secret_key" not in self.model_fields_set

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def is_test(self) -> bool:
        return self.environment.lower() in {"test", "testing"}

    @property
    def should_create_tables(self) -> bool:
        """Never in production, whatever the setting says.

        A deployment that set this by accident would get a schema created
        around whatever the models happen to say, bypassing the migration
        history entirely — which is how a production database ends up in a
        state no revision describes.
        """

        return self.auto_create_tables and not self.is_production

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def deployment_problems(self) -> list[DeploymentProblem]:
        """Every production setting that is unsafe, or an empty list.

        Returns rather than raises so a diagnostic command can print all of
        them at once. :func:`get_settings` is what turns a non-empty list into
        a refusal to start.

        Outside production this is always empty — not because the settings are
        safe there, but because they are *meant* to be permissive, and a
        developer who has to satisfy production's checks to run a laptop will
        set ``ILUVTRADE_ENVIRONMENT=production`` to make the noise stop.
        """

        if not self.is_production:
            return []

        problems: list[DeploymentProblem] = []

        # --- the secret key ------------------------------------------------
        if self.secret_key_was_generated:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_SECRET_KEY",
                    "not set, so a random key was generated for this process. Every "
                    "session would be invalidated on restart and every stored broker "
                    "credential and TOTP secret would become permanently unreadable, "
                    "because both are sealed under this key.",
                )
            )
        elif len(self.secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_SECRET_KEY",
                    f"is {len(self.secret_key)} characters; production requires at "
                    f"least {MIN_PRODUCTION_SECRET_LENGTH}. Generate one with "
                    "'python -c \"import secrets; print(secrets.token_urlsafe(48))\"'.",
                )
            )
        elif self.secret_key.strip().lower() in _PLACEHOLDER_SECRETS:
            problems.append(
                DeploymentProblem("ILUVTRADE_SECRET_KEY", "is a placeholder value, not a secret.")
            )
        if self.secret_key in self.retired_secret_keys:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_RETIRED_SECRET_KEYS",
                    "contains the current key. A retired key is one nothing is "
                    "encrypted under any more; listing the active key there makes "
                    "the rotation a no-op.",
                )
            )

        # --- schema ownership ---------------------------------------------
        if self.auto_create_tables:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_AUTO_CREATE_TABLES",
                    "is on. Production schema is owned by Alembic; create_all "
                    "silently ignores drift. Run 'alembic upgrade head' instead "
                    "and set this to false.",
                )
            )
        if self.uses_sqlite and not self.allow_sqlite_in_production:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_DATABASE_URL",
                    "is SQLite. The backtest workers and every running trading "
                    "session write concurrently from threads, which is how 'database "
                    "is locked' happens under load. Point at PostgreSQL, or set "
                    "ILUVTRADE_ALLOW_SQLITE_IN_PRODUCTION=true to accept that trade "
                    "on a single node.",
                )
            )

        # --- browser-facing surface ----------------------------------------
        for origin in self.allowed_origins:
            if origin == "*":
                problems.append(
                    DeploymentProblem(
                        "ILUVTRADE_ALLOWED_ORIGINS",
                        "contains '*'. This API allows credentials, and a wildcard "
                        "origin with credentials lets any site act as a signed-in "
                        "user. List the exact origins, or leave it empty for "
                        "same-origin only.",
                    )
                )
            elif not origin.startswith("https://"):
                problems.append(
                    DeploymentProblem(
                        "ILUVTRADE_ALLOWED_ORIGINS",
                        f"contains the plaintext origin {origin!r}. The session "
                        "cookie is Secure in production and would never be sent "
                        "there; allowing it only widens the CORS surface.",
                    )
                )

        # --- network egress -------------------------------------------------
        if self.fetch_allow_loopback:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_FETCH_ALLOW_LOOPBACK",
                    "is on. Dataset fetching would be able to reach services bound "
                    "to localhost on the application host — the metadata endpoint, "
                    "an admin port, the database. This exists for tests.",
                )
            )

        # --- rate limiting ---------------------------------------------------
        if not self.rate_limit_enabled and not self.rate_limit_enforced_externally:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_RATE_LIMIT_ENABLED",
                    "is off and nothing claims to enforce limits elsewhere. Login "
                    "would accept unlimited password guesses. If a gateway enforces "
                    "them, set ILUVTRADE_RATE_LIMIT_ENFORCED_EXTERNALLY=true to say "
                    "so.",
                )
            )

        # --- capability configuration ----------------------------------------
        if self.live_trading_enabled and not (self.zerodha_api_key and self.zerodha_api_secret):
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_LIVE_TRADING_ENABLED",
                    "is on but no broker credentials are configured, so every live "
                    "session would fail at the venue. Configure "
                    "ILUVTRADE_ZERODHA_API_KEY and ILUVTRADE_ZERODHA_API_SECRET, or "
                    "turn live trading off.",
                )
            )
        if self.payment_provider not in KNOWN_PAYMENT_PROVIDERS:
            problems.append(
                DeploymentProblem(
                    "ILUVTRADE_PAYMENT_PROVIDER",
                    f"is {self.payment_provider!r}, which is not implemented. Known: "
                    f"{', '.join(sorted(KNOWN_PAYMENT_PROVIDERS))}.",
                )
            )

        return problems

    def rate_limit_overrides_parsed(self) -> dict[str, tuple[int, int]]:
        """``{"login": (5, 60)}`` from ``login=5/60``.

        Raises :class:`DeploymentProblem` on a malformed entry in every
        environment, not only production: a limit nobody can parse is silently
        not applied, which is the failure this whole setting exists to avoid.
        """

        parsed: dict[str, tuple[int, int]] = {}
        for entry in self.rate_limit_overrides:
            name, _, spec = entry.partition("=")
            limit_text, _, window_text = spec.partition("/")
            try:
                limit, window = int(limit_text), int(window_text)
            except ValueError as exc:
                raise DeploymentProblem(
                    "ILUVTRADE_RATE_LIMIT_OVERRIDES",
                    f"{entry!r} is not 'name=limit/window_seconds'.",
                ) from exc
            if limit < 1 or window < 1:
                raise DeploymentProblem(
                    "ILUVTRADE_RATE_LIMIT_OVERRIDES",
                    f"{entry!r} has a non-positive limit or window.",
                )
            parsed[name.strip()] = (limit, window)
        return parsed


#: Payment providers this application can actually construct. ``manual`` moves
#: no money; see :mod:`iluvtrade.billing.provider`.
KNOWN_PAYMENT_PROVIDERS = frozenset({"manual"})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object.

    Refuses to return unsafe production settings. Raising here rather than at
    the first use of a bad value means a misconfigured deployment fails at
    startup, in the foreground, where a deployment script notices — not two
    hours later on the first login.
    """

    settings = Settings()
    problems = settings.deployment_problems()
    if problems:
        raise UnsafeProductionConfiguration(problems)
    return settings
