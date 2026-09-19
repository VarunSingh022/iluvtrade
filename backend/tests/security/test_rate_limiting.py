"""Rate limiting, asserted deterministically.

The clock is injected everywhere, so nothing sleeps and nothing flakes on a
slow machine. The HTTP tests enable the limiter explicitly — the suite disables
it by default so unrelated tests are not throttled.
"""

from __future__ import annotations

import pytest

from iluvtrade.platform.ratelimit import (
    POLICIES,
    InMemoryBackend,
    Policy,
    RateLimiter,
    RateLimitError,
    reset_limiter,
)

pytestmark = pytest.mark.security


class Clock:
    """A clock a test advances explicitly."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- the limiter itself -----------------------------------------------------


def test_a_policy_allows_exactly_its_limit() -> None:
    clock = Clock()
    limiter = RateLimiter(InMemoryBackend(), clock=clock)
    policy = Policy("p", limit=3, window_seconds=60)

    for _ in range(3):
        limiter.check(policy, "user-1")
    with pytest.raises(RateLimitError):
        limiter.check(policy, "user-1")


def test_the_error_says_which_policy_and_when_to_retry() -> None:
    clock = Clock(start=1_000_000.0)
    limiter = RateLimiter(InMemoryBackend(), clock=clock)
    policy = Policy("login", limit=1, window_seconds=60)

    limiter.check(policy, "user-1")
    with pytest.raises(RateLimitError) as caught:
        limiter.check(policy, "user-1")
    assert caught.value.policy == "login"
    assert 1 <= caught.value.retry_after_seconds <= 60


def test_identities_have_separate_budgets() -> None:
    """One user must not be able to exhaust another's allowance."""

    clock = Clock()
    limiter = RateLimiter(InMemoryBackend(), clock=clock)
    policy = Policy("p", limit=2, window_seconds=60)

    limiter.check(policy, "user-1")
    limiter.check(policy, "user-1")
    with pytest.raises(RateLimitError):
        limiter.check(policy, "user-1")

    limiter.check(policy, "user-2")
    limiter.check(policy, "user-2")


def test_policies_have_separate_budgets() -> None:
    clock = Clock()
    limiter = RateLimiter(InMemoryBackend(), clock=clock)
    login = Policy("login", limit=1, window_seconds=60)
    backtest = Policy("backtest", limit=1, window_seconds=60)

    limiter.check(login, "user-1")
    limiter.check(backtest, "user-1")
    with pytest.raises(RateLimitError):
        limiter.check(login, "user-1")


def test_the_window_rolls() -> None:
    clock = Clock()
    limiter = RateLimiter(InMemoryBackend(), clock=clock)
    policy = Policy("p", limit=2, window_seconds=60)

    limiter.check(policy, "user-1")
    limiter.check(policy, "user-1")
    with pytest.raises(RateLimitError):
        limiter.check(policy, "user-1")

    clock.advance(60)
    limiter.check(policy, "user-1")
    limiter.check(policy, "user-1")


def test_a_disabled_limiter_never_refuses() -> None:
    limiter = RateLimiter(InMemoryBackend(), clock=Clock(), enabled=False)
    policy = Policy("p", limit=1, window_seconds=60)
    for _ in range(100):
        limiter.check(policy, "user-1")


def test_a_policy_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Policy("p", limit=0, window_seconds=60)
    with pytest.raises(ValueError):
        Policy("p", limit=1, window_seconds=0)


def test_the_backend_prunes_expired_windows() -> None:
    """A long-running process must not accumulate keys forever."""

    clock = Clock()
    backend = InMemoryBackend()
    for index in range(InMemoryBackend._SWEEP_AT + 100):
        backend.hit(f"k{index}", 60, clock.now)
    before = len(backend._counts)

    clock.advance(600)
    backend.hit("trigger", 60, clock.now)
    assert len(backend._counts) < before


# --- through the API --------------------------------------------------------


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch):
    """Turn the limiter on for one test."""

    from iluvtrade.config import get_settings

    monkeypatch.setenv("ILUVTRADE_RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    reset_limiter()
    yield
    get_settings.cache_clear()
    reset_limiter()


def test_login_is_rate_limited(limited, app) -> None:
    """Credential guessing is the reason this policy is the tightest."""

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "a@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "A",
            },
        )
        limit = POLICIES["login"].limit
        codes = [
            client.post(
                "/api/v1/auth/login",
                json={"email": "a@example.com", "password": "wrong-password-entirely"},
            ).status_code
            for _ in range(limit + 3)
        ]

    assert 401 in codes, "early attempts should be ordinary auth failures"
    assert 429 in codes, "the policy must eventually refuse"
    assert codes.index(429) >= limit, "it must not refuse before the limit"


def test_a_rate_limited_response_uses_the_error_envelope(limited, app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = None
        for _ in range(POLICIES["login"].limit + 3):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.com", "password": "whatever-long-enough"},
            )
            if response.status_code == 429:
                break

    assert response is not None and response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "RateLimited"
    assert body["error"]["policy"] == "login"
    assert body["error"]["retry_after_seconds"] >= 1
    assert response.headers["Retry-After"] == str(body["error"]["retry_after_seconds"])


def test_registration_is_rate_limited(limited, app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        codes = []
        for index in range(POLICIES["register"].limit + 2):
            codes.append(
                client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"user{index}@example.com",
                        "password": "correct-horse-battery-staple",
                        "display_name": "U",
                    },
                ).status_code
            )

    assert 201 in codes
    assert 429 in codes


def test_backtest_submission_is_rate_limited(limited, app, headers) -> None:
    """A worker-occupying operation needs a per-user budget."""

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "t@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "T",
            },
        )
        limit = POLICIES["backtest"].limit
        codes = [
            client.post(
                "/api/v1/backtests",
                json={
                    "dataset_version_id": "00000000-0000-0000-0000-000000000000",
                    "strategy_version_id": "00000000-0000-0000-0000-000000000000",
                },
                headers=headers,
            ).status_code
            for _ in range(limit + 2)
        ]

    # Every attempt references a nonexistent dataset, so the successful ones are
    # 404s. What matters is that the limit fires regardless of the outcome —
    # a policy that only counted *successful* calls would not stop abuse.
    assert 404 in codes
    assert 429 in codes


def test_an_authenticated_policy_is_keyed_by_user_not_by_address(limited, app, headers) -> None:
    """Two users behind one NAT must not share a budget."""

    from fastapi.testclient import TestClient

    with TestClient(app) as first, TestClient(app) as second:
        for client, email in ((first, "one@example.com"), (second, "two@example.com")):
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": "correct-horse-battery-staple",
                    "display_name": "U",
                },
            )

        body = {
            "dataset_version_id": "00000000-0000-0000-0000-000000000000",
            "strategy_version_id": "00000000-0000-0000-0000-000000000000",
        }
        for _ in range(POLICIES["backtest"].limit + 2):
            first.post("/api/v1/backtests", json=body, headers=headers)

        # The first user is now blocked; the second must not be.
        assert first.post("/api/v1/backtests", json=body, headers=headers).status_code == 429
        assert second.post("/api/v1/backtests", json=body, headers=headers).status_code != 429


def test_every_declared_policy_is_valid() -> None:
    for name, policy in POLICIES.items():
        assert policy.name == name
        assert policy.limit >= 1
        assert policy.window_seconds >= 1
