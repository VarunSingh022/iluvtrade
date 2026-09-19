"""What this deployment is allowed to start as.

The subject here is not whether a setting parses. It is whether a *production*
process can come up in a state that quietly does the wrong thing — an invented
secret key, a schema created behind the migrations' back, a CORS origin that
carries credentials over plaintext. Each of those looks fine in a log and is
discovered much later, usually by a user.

So every test below drives :meth:`Settings.deployment_problems` for a specific
unsafe combination and asserts it is *named*, and the last group asserts that
development stays permissive — because a check a developer has to satisfy on a
laptop is a check that gets switched off.
"""

from __future__ import annotations

import pytest

from iluvtrade.config import (
    MIN_PRODUCTION_SECRET_LENGTH,
    DeploymentProblem,
    Settings,
    UnsafeProductionConfiguration,
    get_settings,
)

pytestmark = pytest.mark.security

#: A production base that is otherwise clean, so each test varies one thing.
SAFE_PRODUCTION = {
    "environment": "production",
    "secret_key": "s" * 64,
    "database_url": "postgresql+psycopg://user:pw@db:5432/iluvtrade",
    "auto_create_tables": False,
    "allowed_origins": ("https://app.example.com",),
    "rate_limit_enabled": True,
    "fetch_allow_loopback": False,
}


def _problems(**overrides: object) -> dict[str, str]:
    settings = Settings(_env_file=None, **{**SAFE_PRODUCTION, **overrides})
    return {p.variable: p.message for p in settings.deployment_problems()}


# --- the baseline -----------------------------------------------------------


def test_a_correctly_configured_production_deployment_has_no_problems() -> None:
    """Without this, every test below could pass for the wrong reason."""

    assert _problems() == {}


# --- the secret key ---------------------------------------------------------


def test_an_unset_secret_key_is_refused_in_production(monkeypatch) -> None:
    """The check this replaces could never fire.

    ``secret_key`` has a ``default_factory``, so an unset key is a *valid*
    random string — ``if not settings.secret_key`` was therefore always false,
    and production would start with a per-process key: every session dropped on
    restart, and every stored broker credential and TOTP secret sealed under a
    key that no longer exists.
    """

    monkeypatch.delenv("ILUVTRADE_SECRET_KEY", raising=False)
    settings = Settings(
        _env_file=None, **{k: v for k, v in SAFE_PRODUCTION.items() if k != "secret_key"}
    )
    assert settings.secret_key, "a key is still generated so development works"
    assert settings.secret_key_was_generated is True
    assert "ILUVTRADE_SECRET_KEY" in {p.variable for p in settings.deployment_problems()}


def test_a_short_secret_key_is_refused() -> None:
    problems = _problems(secret_key="s" * (MIN_PRODUCTION_SECRET_LENGTH - 1))
    assert "ILUVTRADE_SECRET_KEY" in problems
    assert str(MIN_PRODUCTION_SECRET_LENGTH) in problems["ILUVTRADE_SECRET_KEY"]


def test_a_placeholder_secret_key_is_refused() -> None:
    assert "ILUVTRADE_SECRET_KEY" in _problems(secret_key="change-me")


def test_the_active_key_may_not_also_be_listed_as_retired() -> None:
    """Otherwise the rotation is a no-op nobody notices."""

    problems = _problems(retired_secret_keys=("s" * 64, "older"))
    assert "ILUVTRADE_RETIRED_SECRET_KEYS" in problems


# --- schema ownership -------------------------------------------------------


def test_create_all_in_production_is_refused() -> None:
    assert "ILUVTRADE_AUTO_CREATE_TABLES" in _problems(auto_create_tables=True)


def test_sqlite_in_production_is_refused_unless_acknowledged() -> None:
    """Refused by default, allowed with a setting that names the trade."""

    assert "ILUVTRADE_DATABASE_URL" in _problems(database_url="sqlite:///var/x.db")
    assert _problems(database_url="sqlite:///var/x.db", allow_sqlite_in_production=True) == {}


# --- the browser-facing surface ---------------------------------------------


def test_a_wildcard_origin_is_refused() -> None:
    problems = _problems(allowed_origins=("*",))
    assert "ILUVTRADE_ALLOWED_ORIGINS" in problems
    assert "credentials" in problems["ILUVTRADE_ALLOWED_ORIGINS"]


def test_a_plaintext_origin_is_refused() -> None:
    assert "ILUVTRADE_ALLOWED_ORIGINS" in _problems(allowed_origins=("http://app.example.com",))


def test_the_development_default_does_not_survive_into_production() -> None:
    """The dev origins are http://localhost — production must not inherit them."""

    settings = Settings(
        _env_file=None,
        **{k: v for k, v in SAFE_PRODUCTION.items() if k != "allowed_origins"},
    )
    assert "ILUVTRADE_ALLOWED_ORIGINS" in {p.variable for p in settings.deployment_problems()}


def test_same_origin_deployments_may_have_no_cors_at_all() -> None:
    assert _problems(allowed_origins=()) == {}


# --- egress -----------------------------------------------------------------


def test_loopback_fetching_is_refused_in_production() -> None:
    """The SSRF escape hatch that exists for tests must not ship."""

    assert "ILUVTRADE_FETCH_ALLOW_LOOPBACK" in _problems(fetch_allow_loopback=True)


# --- rate limiting -----------------------------------------------------------


def test_disabling_rate_limits_in_production_is_refused() -> None:
    assert "ILUVTRADE_RATE_LIMIT_ENABLED" in _problems(rate_limit_enabled=False)


def test_rate_limits_may_be_disabled_when_something_else_enforces_them() -> None:
    """An escape hatch that requires naming where the control went."""

    assert _problems(rate_limit_enabled=False, rate_limit_enforced_externally=True) == {}


# --- capability configuration -------------------------------------------------


def test_live_trading_without_broker_credentials_is_refused() -> None:
    problems = _problems(live_trading_enabled=True)
    assert "ILUVTRADE_LIVE_TRADING_ENABLED" in problems

    assert (
        _problems(
            live_trading_enabled=True,
            zerodha_api_key="k",
            zerodha_api_secret="s",
        )
        == {}
    )


def test_an_unimplemented_payment_provider_is_refused() -> None:
    """Better than discovering it at the first purchase."""

    assert "ILUVTRADE_PAYMENT_PROVIDER" in _problems(payment_provider="stripe")


# --- the refusal itself --------------------------------------------------------


def test_get_settings_refuses_to_return_unsafe_production_settings(monkeypatch) -> None:
    """Startup, in the foreground, where a deploy script notices."""

    monkeypatch.setenv("ILUVTRADE_ENVIRONMENT", "production")
    # An explicitly short key rather than an absent one: get_settings() reads
    # the repository's .env, which supplies a key in development, so "absent"
    # is not reproducible here. The refusal path is the same.
    monkeypatch.setenv("ILUVTRADE_SECRET_KEY", "too-short")
    get_settings.cache_clear()
    try:
        with pytest.raises(UnsafeProductionConfiguration) as caught:
            get_settings()
        assert caught.value.problems
        # The message has to name the variables, or it costs an hour.
        assert "ILUVTRADE_SECRET_KEY" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_development_is_left_permissive() -> None:
    """A check a laptop has to satisfy is a check that gets turned off."""

    assert Settings(_env_file=None, environment="development").deployment_problems() == []
    assert Settings(_env_file=None, environment="test").deployment_problems() == []


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("data.example.com,files.example.org", ("data.example.com", "files.example.org")),
        (" a.example.com , b.example.com ", ("a.example.com", "b.example.com")),
        ('["a.example.com","b.example.com"]', ("a.example.com", "b.example.com")),
        ("", ()),
    ],
)
def test_list_settings_accept_the_syntax_the_documentation_promises(
    monkeypatch, raw: str, expected: tuple[str, ...]
) -> None:
    """``.env.example`` says "comma-separated"; it has to actually work.

    pydantic-settings decodes a complex field from the environment as JSON, so
    following the documentation was a startup crash — on the SSRF allowlist,
    the setting an operator most needs to be able to change.
    """

    monkeypatch.setenv("ILUVTRADE_FETCH_ALLOWED_HOSTS", raw)
    assert Settings(_env_file=None).fetch_allowed_hosts == expected


def test_ports_parse_as_integers(monkeypatch) -> None:
    monkeypatch.setenv("ILUVTRADE_FETCH_ALLOWED_PORTS", "443,8443")
    assert Settings(_env_file=None).fetch_allowed_ports == (443, 8443)


def test_rate_limit_overrides_parse(monkeypatch) -> None:
    monkeypatch.setenv("ILUVTRADE_RATE_LIMIT_OVERRIDES", "login=5/60,backtest=20/120")
    parsed = Settings(_env_file=None).rate_limit_overrides_parsed()
    assert parsed == {"login": (5, 60), "backtest": (20, 120)}


@pytest.mark.parametrize("bad", ["login", "login=five/60", "login=0/60", "login=5/0"])
def test_a_malformed_rate_limit_override_is_an_error_not_a_silent_default(bad: str) -> None:
    """Silently ignoring it would enforce a limit the operator does not think they have."""

    settings = Settings(_env_file=None, rate_limit_overrides=(bad,))
    with pytest.raises(DeploymentProblem):
        settings.rate_limit_overrides_parsed()


def test_an_override_for_an_unknown_policy_is_refused() -> None:
    from iluvtrade.platform.ratelimit import effective_policies

    with pytest.raises(KeyError, match="lgoin"):
        effective_policies({"lgoin": (5, 60)})


def test_an_override_actually_changes_the_enforced_policy() -> None:
    from iluvtrade.platform.ratelimit import POLICIES, effective_policies

    resolved = effective_policies({"login": (3, 30)})
    assert resolved["login"].limit == 3
    assert resolved["login"].window_seconds == 30
    assert POLICIES["login"].limit == 10, "the module default must not be mutated"
