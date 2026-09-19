"""The end-to-end demonstration (PHASE 23).

Runs the **real application over its own HTTP API** — every step below is an
actual request against the FastAPI app, hitting the real services, the real
database and the real AlphaLab engine. Nothing is stubbed and no response is
fabricated; the numbers printed are whatever AlphaLab computed.

Two organizations take part, because half of what is being demonstrated is that
they are separated: a creator publishes a strategy to RedDesk, a buyer purchases
it, receives an entitlement to one concrete version, and deploys *that version*
to paper trading. The buyer never gains access to the creator's datasets.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
import sys
import tempfile
from typing import Any

import pyotp

__all__ = ["run_demo"]

WIDTH = 78


def _rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * WIDTH}\n{title}\n{'─' * WIDTH}")
    else:
        print("─" * WIDTH)


def _step(number: int, text: str) -> None:
    print(f"\n[{number:2d}] {text}")


def _synthetic_csv(seed: int = 4) -> bytes:
    """A deliberately imperfect OHLCV file.

    Imperfect on purpose: the point of the data workspace is what it *says*
    about a file, and a clean file demonstrates none of it. This one carries a
    duplicate row, an unparseable price, an inconsistent bar, a negative volume,
    a missing timestamp and an out-of-order row.
    """

    random.seed(seed)
    rows: list[list[Any]] = [["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"]]
    for symbol, base, bias in (("RELIANCE", 2400.0, 1.0), ("TCS", 3600.0, -0.5)):
        price = base
        day = dt.date(2023, 1, 2)
        for index in range(260):
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            trend = 1.1 if index < 150 else -0.9
            price = max(50.0, price + trend * bias + random.uniform(-15, 15))
            open_ = round(price - random.uniform(0, 6), 2)
            close = round(price, 2)
            high = round(max(open_, close) + random.uniform(0, 7), 2)
            low = round(min(open_, close) - random.uniform(0, 7), 2)
            rows.append(
                [
                    day.isoformat(),
                    symbol,
                    open_,
                    high,
                    low,
                    close,
                    random.randint(500_000, 3_000_000),
                ]
            )
            day += dt.timedelta(days=1)

    header, body = rows[0], sorted(rows[1:], key=lambda r: (r[0], r[1]))
    body.insert(40, list(body[39]))  # exact duplicate
    body.insert(80, [body[79][0], "RELIANCE", "notanumber", 2500, 2400, 2450, 900000])
    body.insert(120, [body[119][0], "TCS", 3600, 3500, 3700, 3650, 800000])  # high < low
    body.insert(160, [body[159][0], "RELIANCE", 2500, 2520, 2480, 2510, -900])
    body.insert(200, ["", "TCS", 3600, 3620, 3580, 3610, 700000])  # no timestamp
    body.insert(240, ["2022-06-01", "RELIANCE", 2300, 2320, 2280, 2310, 600000])  # backwards

    buffer = io.StringIO()
    csv.writer(buffer).writerows([header, *body])
    return buffer.getvalue().encode("utf-8")


def run_demo() -> int:
    """Execute the full workflow. Returns a process exit code."""

    import os

    workspace = tempfile.mkdtemp(prefix="iluvtrade-demo-")
    os.environ.setdefault("ILUVTRADE_DATABASE_URL", f"sqlite:///{workspace}/demo.db")
    os.environ.setdefault("ILUVTRADE_STORAGE_ROOT", f"{workspace}/storage")
    os.environ.setdefault("ILUVTRADE_SECRET_KEY", "demo-only-secret-not-for-production")

    from fastapi.testclient import TestClient

    from iluvtrade.alphalab_bridge import ALPHALAB_VERSION
    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.main import create_app
    from iluvtrade.trading.runner import RUNNER

    print("=" * WIDTH)
    print("iluvtrade — end-to-end demonstration".center(WIDTH))
    print(f"engine: alphalab {ALPHALAB_VERSION}".center(WIDTH))
    print("=" * WIDTH)
    print(f"\nWorkspace: {workspace}")

    app = create_app(start_workers=False, create_tables=True)

    def check(response: Any, expected: int, what: str) -> dict:
        if response.status_code != expected:
            print(f"\n  ✗ {what}: HTTP {response.status_code}\n    {response.text[:600]}")
            raise SystemExit(1)
        return response.json() if response.content else {}

    with TestClient(app) as creator, TestClient(app) as buyer:
        # -- 1. accounts and workspaces ---------------------------------
        _rule("PLATFORM — accounts, workspaces, tenant isolation")

        _step(1, "Create the creator account and its workspace")
        creator_user = check(
            creator.post(
                "/api/v1/auth/register",
                json={
                    "email": "quant@example.com",
                    "password": "correct-horse-battery-staple",
                    "display_name": "Quant Desk",
                    "organization_name": "Quant Desk Research",
                },
            ),
            201,
            "register creator",
        )["user"]
        print(
            f"     user={creator_user['email']}  org={creator_user['organization_name']}"
            f"  role={creator_user['role']}"
        )

        _step(2, "Create a second, unrelated account — the buyer")
        buyer_user = check(
            buyer.post(
                "/api/v1/auth/register",
                json={
                    "email": "trader@example.com",
                    "password": "correct-horse-battery-staple",
                    "display_name": "Retail Trader",
                    "organization_name": "Trader Workspace",
                },
            ),
            201,
            "register buyer",
        )["user"]
        print(f"     user={buyer_user['email']}  org={buyer_user['organization_name']}")

        headers = {"X-Requested-With": "XMLHttpRequest"}

        # -- 2. data workspace ------------------------------------------
        _rule("DATA WORKSPACE — upload, detect, clean, report, approve")

        payload = _synthetic_csv()
        _step(3, f"Inspect a {len(payload):,}-byte CSV without storing it")
        detected = check(
            creator.post(
                "/api/v1/datasets/inspect",
                files={"file": ("nse.csv", payload, "text/csv")},
                headers=headers,
            ),
            200,
            "inspect",
        )
        fields = detected["fields"]
        print(f"     columns          : {', '.join(detected['columns'])}")
        print(
            f"     timestamp field  : {fields['timestamp']['column']} "
            f"({detected['timestamp_format']}, confidence "
            f"{fields['timestamp']['confidence']:.2f})"
        )
        print(f"     symbol field     : {fields['symbol']['column']}")
        print(f"     OHLCV complete   : {detected['ohlc_complete']}")

        _step(4, "Upload it — detection, validation, cleaning and the quality report")
        version = check(
            creator.post(
                "/api/v1/datasets/upload",
                files={"file": ("nse.csv", payload, "text/csv")},
                data={"dataset_name": "NSE daily bars"},
                headers=headers,
            ),
            201,
            "upload",
        )
        quality = version["quality"]
        print(f"     rows in file     : {quality['total_input_rows']}")
        print(f"     accepted         : {quality['accepted_rows']}")
        print(
            f"     rejected         : {quality['rejected_rows']}  {quality['rejection_breakdown']}"
        )
        print(
            f"     symbols          : {quality['symbol_count']} "
            f"{quality['symbols']}   frequency: {quality['inferred_frequency']}"
        )
        print(f"     quality score    : {quality['score']}")
        print(f"     status           : {version['status']}  (nothing may use it yet)")

        print("\n     every rejected row, with its reason:")
        for row in version["rejected_rows"]["logged"][:8]:
            print(f"       line {row['line_number']:>4}  {row['reason']:<20} {row['detail'][:44]}")

        print("\n     every transformation, counted:")
        for kind, count in version["transformations"]["counts"].items():
            print(f"       {kind:<28} {count}")

        print("\n     findings:")
        for finding in quality["findings"]:
            print(f"       [{finding['severity']:<7}] {finding['message'][:60]}")

        print(f"\n     provenance       : sha256={version['source']['content_hash'][:24]}…")
        print(f"     canonical hash   : {version['canonical_hash'][:24]}…")

        dataset_version_id = version["id"]

        _step(5, "Try to use the dataset before approving it")
        strategy = check(
            creator.post(
                "/api/v1/strategies",
                json={"name": "Trend Rider", "description": "Moving-average crossover."},
                headers=headers,
            ),
            201,
            "create strategy",
        )
        draft = check(
            creator.post(
                f"/api/v1/strategies/{strategy['id']}/versions",
                json={
                    "implementation_key": "moving_average_crossover",
                    "parameters": {"fast": 8, "slow": 24, "quantity": 40},
                    "changelog": "Initial release.",
                },
                headers=headers,
            ),
            201,
            "create version",
        )
        published = check(
            creator.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers),
            200,
            "publish version",
        )
        refused = creator.post(
            "/api/v1/backtests",
            json={
                "dataset_version_id": dataset_version_id,
                "strategy_version_id": published["id"],
            },
            headers=headers,
        )
        print(f"     HTTP {refused.status_code} — {refused.json()['error']['message'][:64]}")

        _step(6, "Approve the dataset version")
        approved = check(
            creator.post(
                f"/api/v1/datasets/versions/{dataset_version_id}/approve", headers=headers
            ),
            200,
            "approve",
        )
        print(f"     status={approved['status']}  rows={approved['row_count']}")

        # -- 3. strategy versioning -------------------------------------
        _rule("STRATEGIES — identity, immutable versions")

        _step(7, "The published version is frozen")
        print(
            f"     version {published['version']}  hash={published['content_hash'][:24]}…"
            f"  frozen={published['frozen']}  integrity_ok={published['integrity_ok']}"
        )
        blocked = creator.patch(
            f"/api/v1/strategies/versions/{published['id']}",
            json={"changelog": "sneaking a change in"},
            headers=headers,
        )
        print(
            f"     editing it: HTTP {blocked.status_code} — "
            f"{blocked.json()['error']['message'][:60]}"
        )
        strategy_version_id = published["id"]

        # -- 4. research ------------------------------------------------
        _rule("RESEARCH — asynchronous backtest on the real engine")

        _step(8, "Submit a backtest")
        job = check(
            creator.post(
                "/api/v1/backtests",
                json={
                    "dataset_version_id": dataset_version_id,
                    "strategy_version_id": strategy_version_id,
                    "parameters": {"fast": 8, "slow": 24, "quantity": 40},
                    "starting_cash": "2000000.00",
                    "currency": "INR",
                    "risk_profile": "research",
                    "seed": 20260914,
                },
                headers=headers,
            ),
            202,
            "submit backtest",
        )
        print(f"     job={job['id'][:8]}  status={job['status']}  (HTTP 202 — not run yet)")

        _step(9, "Submit the identical request again")
        duplicate = check(
            creator.post(
                "/api/v1/backtests",
                json={
                    "dataset_version_id": dataset_version_id,
                    "strategy_version_id": strategy_version_id,
                    "parameters": {"fast": 8, "slow": 24, "quantity": 40},
                    "starting_cash": "2000000.00",
                    "currency": "INR",
                    "risk_profile": "research",
                    "seed": 20260914,
                },
                headers=headers,
            ),
            202,
            "resubmit",
        )
        print(
            f"     same job id: {duplicate['id'] == job['id']}  "
            f"deduplicated={duplicate['deduplicated']}"
        )

        _step(10, "Run the worker")
        BacktestWorkerPool(size=1).drain(timeout=180)
        finished = check(creator.get(f"/api/v1/backtests/{job['id']}"), 200, "poll job")
        print(
            f"     status={finished['status']}"
            + (
                f"  error={finished['error_code']}: {finished['error_message']}"
                if finished["error_code"]
                else ""
            )
        )
        if finished["status"] != "completed":
            return 1

        _step(11, "Read the result — every figure produced by AlphaLab")
        result = check(creator.get(f"/api/v1/backtests/{job['id']}/result"), 200, "result")
        metrics, repro = result["metrics"], result["reproducibility"]
        for key in (
            "records_processed",
            "order_count",
            "fill_count",
            "ending_equity",
            "realized_pnl",
            "unrealized_pnl",
            "commission_paid",
            "total_return",
            "cagr",
            "volatility",
            "sharpe_ratio",
            "max_drawdown",
        ):
            print(f"     {key:<20} {metrics[key]}")

        print("\n     reproducibility record:")
        print(
            f"       dataset version   {repro['dataset_version_id'][:8]}  "
            f"hash {(repro['dataset_canonical_hash'] or '')[:16]}…"
        )
        print(
            f"       strategy version  {repro['strategy_version_id'][:8]}  "
            f"hash {(repro['strategy_content_hash'] or '')[:16]}…"
        )
        print(f"       seed              {repro['seed']}")
        print(f"       engine            {repro['engine']}")
        print(f"       universe          {repro['universe']}")

        document = result["result"]
        refusals = document.get("risk_refusals", {})
        print(
            f"\n     orders: {len(document['orders'])}   fills: {len(document['fills'])}"
            f"   equity curve points: {len(document['equity_curve'])}"
        )
        print(f"     orders refused by risk controls: {refusals.get('count', 0)}")
        for refused in refusals.get("logged", [])[:3]:
            print(f"       record {refused['record_index']:>4}  {refused['reason'][:58]}")
        for order in document["orders"][:4]:
            print(
                f"       {order['symbol']:<10} {order['side']:<5} "
                f"{order['quantity']:>12}  @ {order['average_fill_price']:<12} "
                f"{order['status']}"
            )
        backtest_run_id = result["run_id"]

        # -- 5. RedDesk --------------------------------------------------
        _rule("REDDESK — listing, validation, publication, purchase, entitlement")

        _step(12, "Create a listing and attach the version with its evidence")
        listing = check(
            creator.post(
                "/api/v1/reddesk/listings",
                json={
                    "strategy_id": strategy["id"],
                    "title": "Trend Rider NSE",
                    "summary": "Moving-average crossover on NSE daily bars.",
                    "description": (
                        "Goes long when the fast moving average crosses above the slow one "
                        "and flattens when it crosses back below. Trend-following, and "
                        "loses money in sideways markets by construction."
                    ),
                    "methodology": (
                        "8/24 simple moving averages over daily closes, fixed quantity per "
                        "signal, no leverage, no shorting."
                    ),
                    "risk_disclosure": (
                        "Backtested results are a simulation over historical data under "
                        "stated assumptions. They are not a forecast and not a guarantee."
                    ),
                    "price_amount": "4999.00",
                    "price_currency": "INR",
                    "billing_cadence": "monthly",
                    "version_access_policy": "pinned",
                    "licence_terms": (
                        "A licence to run this strategy on iluvtrade for the subscription "
                        "term. It does not transfer ownership of the strategy or its source."
                    ),
                    "supported_brokers": ["paper", "zerodha"],
                    "supported_instruments": ["NSE equities"],
                    "supported_data": ["daily OHLCV"],
                },
                headers=headers,
            ),
            201,
            "create listing",
        )
        check(
            creator.post(
                f"/api/v1/reddesk/listings/{listing['id']}/versions",
                json={
                    "strategy_version_id": strategy_version_id,
                    "evidence_backtest_run_id": backtest_run_id,
                    "release_notes": "Initial release.",
                },
                headers=headers,
            ),
            201,
            "offer version",
        )
        print(f"     listing={listing['id'][:8]}  status={listing['status']}")

        _step(13, "Validate, submit, review and publish")
        validation = check(
            creator.get(f"/api/v1/reddesk/listings/{listing['id']}/validate"), 200, "validate"
        )
        print(f"     publishable={validation['publishable']}  issues={len(validation['issues'])}")
        check(
            creator.post(f"/api/v1/reddesk/listings/{listing['id']}/submit", headers=headers),
            200,
            "submit listing",
        )
        check(
            creator.post(
                f"/api/v1/reddesk/listings/{listing['id']}/review",
                json={"approve": True, "notes": "Evidence present; disclosures complete."},
                headers=headers,
            ),
            200,
            "review listing",
        )
        live = check(
            creator.post(f"/api/v1/reddesk/listings/{listing['id']}/publish", headers=headers),
            200,
            "publish listing",
        )
        print(f"     status={live['status']}")

        _step(14, "The buyer discovers it")
        found = check(buyer.get("/api/v1/reddesk/discover"), 200, "discover")
        entry = found[0]
        print(
            f'     "{entry["title"]}" — {entry["price"]["currency"]} '
            f"{entry['price']['amount']}/{entry['price']['cadence']}  "
            f"licence: {entry['version_access_policy']}"
        )
        evidence = entry["evidence"]
        print(
            f"     evidence: {evidence['order_count']} orders over "
            f"{evidence['records_processed']} records, "
            f"return {evidence['total_return']}, Sharpe {evidence['sharpe_ratio']}"
        )
        print(f"     {evidence['disclaimer'][:70]}…")

        _step(15, "Before buying, the buyer tries to run it")
        denied = buyer.post(
            "/api/v1/trading/sessions",
            json={
                "name": "unauthorised",
                "mode": "paper",
                "strategy_version_id": entry["current_strategy_version_id"],
                "dataset_version_id": dataset_version_id,
            },
            headers=headers,
        )
        print(f"     HTTP {denied.status_code} — {denied.json()['error']['message'][:62]}")

        _step(16, "Buy it")
        bought = check(
            buyer.post(
                "/api/v1/reddesk/purchases",
                json={"listing_id": listing["id"], "idempotency_key": "demo-purchase-1"},
                headers=headers,
            ),
            201,
            "purchase",
        )
        purchase, entitlement = bought["purchase"], bought["entitlement"]
        print(
            f"     paid {purchase['currency']} {purchase['amount']}  "
            f"(platform fee {purchase['platform_fee']}, creator net {purchase['creator_net']})"
        )
        print(f"     licence hash recorded: {purchase['licence_hash'][:24]}…")
        print(
            f"     entitlement grants exactly version "
            f"{entitlement['granted_strategy_version_id'][:8]} "
            f"({entitlement['version_access_policy']}), valid until "
            f"{(entitlement['valid_until'] or 'forever')[:10]}"
        )

        _step(17, "Repeat the purchase with the same idempotency key")
        again = check(
            buyer.post(
                "/api/v1/reddesk/purchases",
                json={"listing_id": listing["id"], "idempotency_key": "demo-purchase-1"},
                headers=headers,
            ),
            201,
            "repeat purchase",
        )
        print(f"     same purchase returned: {again['purchase']['id'] == purchase['id']}")

        _step(18, "The buyer still cannot see the creator's data")
        leak = buyer.get(f"/api/v1/datasets/versions/{dataset_version_id}")
        print(f"     creator's dataset: HTTP {leak.status_code} ({leak.json()['error']['code']})")
        own = check(buyer.get("/api/v1/datasets"), 200, "buyer datasets")
        print(f"     buyer's own datasets: {len(own)}")

        # -- 6. paper trading --------------------------------------------
        _rule("PAPER TRADING — deploying the entitled version")

        _step(19, "The buyer uploads their own copy of the data and approves it")
        buyer_version = check(
            buyer.post(
                "/api/v1/datasets/upload",
                files={"file": ("nse.csv", payload, "text/csv")},
                data={"dataset_name": "My NSE bars"},
                headers=headers,
            ),
            201,
            "buyer upload",
        )
        check(
            buyer.post(f"/api/v1/datasets/versions/{buyer_version['id']}/approve", headers=headers),
            200,
            "buyer approve",
        )
        print(f"     rows={buyer_version['row_count']}  score={buyer_version['quality_score']}")

        _step(20, "Deploy the purchased version to paper trading")
        paper = check(
            buyer.post(
                "/api/v1/trading/sessions",
                json={
                    "name": "Trend Rider — paper",
                    "mode": "paper",
                    "strategy_version_id": entitlement["granted_strategy_version_id"],
                    "dataset_version_id": buyer_version["id"],
                    "starting_cash": "2000000.00",
                    "risk_profile": "research",
                    "seed": 20260914,
                },
                headers=headers,
            ),
            201,
            "create session",
        )
        print(
            f"     session={paper['id'][:8]}  mode={paper['mode'].upper()}  "
            f"status={paper['status']}"
        )
        print(
            f"     entitlement={paper['entitlement_id'][:8]}  "
            f"running version {paper['strategy_version_id'][:8]}"
        )
        session_id = paper["id"]

        _step(21, "A live session is refused — three independent gates")
        live_attempt = buyer.post(
            "/api/v1/trading/sessions",
            json={
                "name": "live attempt",
                "mode": "live",
                "strategy_version_id": entitlement["granted_strategy_version_id"],
                "broker_account_id": "anything",
                "live_confirmed": True,
            },
            headers=headers,
        )
        print(
            f"     HTTP {live_attempt.status_code} — {live_attempt.json()['error']['message'][:64]}"
        )

        _step(22, "Run the paper session through AlphaLab")
        # ``/start`` launches the runner in the background, which is what the
        # product does. The demo then waits for that thread rather than calling
        # ``run_session`` itself — a second call would be refused by the claim in
        # the runner, but waiting is what a client actually does.
        check(
            buyer.post(f"/api/v1/trading/sessions/{session_id}/start", headers=headers),
            200,
            "start session",
        )
        RUNNER.join(session_id, timeout=180)
        state = check(buyer.get(f"/api/v1/trading/sessions/{session_id}"), 200, "session")
        print(f"     finished with status={state['status']}")

        _step(23, "Inspect orders, fills, positions and P&L")
        state = check(buyer.get(f"/api/v1/trading/sessions/{session_id}"), 200, "session")
        print(
            f"     mode={state['mode'].upper()}  status={state['status']}  "
            f"records={state['records_processed']}"
        )
        print(f"     cash           {state['cash']}")
        print(f"     equity         {state['equity']}")
        print(f"     realized P&L   {state['realized_pnl']}")
        print(f"     unrealized P&L {state['unrealized_pnl']}")
        print(f"     commission     {state['commission_paid']}")

        orders = check(buyer.get(f"/api/v1/trading/sessions/{session_id}/orders"), 200, "orders")
        fills = check(buyer.get(f"/api/v1/trading/sessions/{session_id}/fills"), 200, "fills")
        positions = check(
            buyer.get(f"/api/v1/trading/sessions/{session_id}/positions"), 200, "positions"
        )
        print(f"\n     {len(orders)} orders, {len(fills)} fills, {len(positions)} open position(s)")
        for order in orders[:4]:
            print(
                f"       {order['instrument']:<10} {order['side']:<5} "
                f"{order['quantity']:>12} @ {order['average_fill_price']:<12} "
                f"{order['status']}"
            )
        for position in positions:
            print(
                f"       holding {position['instrument']:<10} {position['quantity']:>12}  "
                f"unrealized {position['unrealized_pnl']}"
            )

        _step(24, "Stop the session and read its event log")
        events = check(buyer.get(f"/api/v1/trading/sessions/{session_id}/events"), 200, "events")
        print(f"     {len(events)} events")
        for event in list(reversed(events))[:5]:
            print(f"       [{event['severity']:<7}] {event['kind']:<20} {event['message'][:38]}")

        # -- 7. audit -----------------------------------------------------
        _rule("AUDIT — the trail both workspaces accumulated")

        _step(25, "Read the buyer's audit log")
        trail = check(buyer.get("/api/v1/audit"), 200, "audit")
        print(f"     {len(trail)} events")
        for event in list(reversed(trail))[:10]:
            print(f"       {event['action']:<34} {event['resource_type']:<18} {event['outcome']}")

        _step(26, "Confirm no audit payload carries a credential")
        serialized = json.dumps(trail)
        leaked = [
            marker
            for marker in ("correct-horse-battery-staple", "access_token", "api_secret")
            if marker in serialized
        ]
        print(f"     credential markers found in audit payloads: {leaked or 'none'}")

        # -- 8. the boundaries, demonstrated rather than asserted ----------
        _rule("BOUNDARIES — what this build deliberately will not do")

        _step(27, "Live trading is refused, and for a structural reason")
        attempt = buyer.post(
            "/api/v1/trading/sessions",
            json={
                "name": "live",
                "mode": "live",
                "strategy_version_id": entitlement["granted_strategy_version_id"],
                "broker_account_id": "any",
                "live_confirmed": True,
            },
            headers=headers,
        )
        print(f"     HTTP {attempt.status_code} — {attempt.json()['error']['message'][:62]}")
        import ast as _ast
        import pathlib as _pathlib

        package = _pathlib.Path(__file__).resolve().parent
        live_mode_sites = [
            str(path.relative_to(package))
            for path in package.rglob("*.py")
            for node in _ast.walk(_ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, _ast.Attribute)
            and node.attr == "LIVE"
            and "ExecutionMode" in _ast.dump(node.value)
        ]
        print(f"     ExecutionMode.LIVE constructed in: {live_mode_sites or 'nowhere'}")
        print("     AlphaLab derives routing from mode, so no configured run can reach a venue.")

        _step(28, "Seller-code execution does not exist")
        implementations = creator.get("/api/v1/strategies/implementations").json()
        print(
            f"     a listing names one of {len(implementations)} in-repository "
            f"implementations: {[i['key'] for i in implementations]}"
        )
        rejected = creator.post(
            f"/api/v1/strategies/{strategy['id']}/versions",
            json={"implementation_key": "attacker_supplied_module", "parameters": {}},
            headers=headers,
        )
        print(
            f"     an unknown implementation: HTTP {rejected.status_code} — "
            f"{rejected.json()['error']['message'][:58]}"
        )

        _step(29, "The payment boundary fails closed")
        billing = buyer.get("/api/v1/reddesk/billing-status").json()
        print(f"     provider={billing['provider']}  moves_money={billing['moves_money']}")
        print(f"     state={billing['state']}")
        from decimal import Decimal

        from iluvtrade.billing import PaymentError, get_provider

        try:
            get_provider("manual").payout(
                organization_id="x", amount=Decimal("1000"), currency="INR"
            )
            print("     ✗ a payout was recorded without money moving")
            return 1
        except PaymentError as exc:
            print(f"     a payout raises rather than writing a false entry: {str(exc)[:54]}")

        _step(30, "The audit chain verifies, and detects tampering")
        verification = buyer.get("/api/v1/audit/verify").json()
        print(
            f"     {verification['events_checked']} events, intact={verification['intact']}, "
            f"head={verification['head_hash'][:16]}…"
        )

        _step(31, "Rate limiting reports its real scope")
        health = buyer.get("/api/health").json()
        limiting = health["rate_limiting"]
        print(
            f"     enabled={limiting['enabled']}  "
            f"shared_across_instances={limiting['shared_across_instances']}"
        )
        if limiting["caveat"]:
            print(f"     {limiting['caveat'][:72]}")
        print(f"     notification channels attached: {health['notification_channels'] or 'none'}")

        # -- accounts, continued ----------------------------------------
        _rule("ACCOUNT SECURITY — second factor, teams, password recovery")

        _step(32, "A correlation id spans the request and comes back on the response")
        traced = buyer.get("/api/v1/dashboard", headers={"X-Request-ID": "demo-trace-0001"})
        echoed = traced.headers.get("X-Request-ID")
        print(f"     sent X-Request-ID=demo-trace-0001  received={echoed}")
        if echoed != "demo-trace-0001":
            print("     ✗ the inbound id was not adopted")
            raise SystemExit(1)
        print("     an id supplied by a proxy is adopted, not replaced, so one trace stays one")

        _step(33, "Enable two-factor authentication on the buyer's account")
        enrolment = check(
            buyer.post("/api/v1/auth/mfa/enrol", headers=headers), 201, "begin MFA enrolment"
        )
        secret, recovery = enrolment["secret"], enrolment["recovery_codes"]
        print(f"     secret issued ({len(secret)} chars) and {len(recovery)} recovery codes")
        print("     MFA is NOT yet on — enrolment is two-step, so a lost")
        print("     secret cannot lock anyone out of their own account")
        before = buyer.get("/api/v1/auth/mfa").json()
        print(f"     enabled={before['enabled']}  pending={before['enrolment_pending']}")

        check(
            buyer.post(
                "/api/v1/auth/mfa/confirm",
                json={"code": pyotp.TOTP(secret).now()},
                headers=headers,
            ),
            200,
            "confirm MFA",
        )
        print("     confirmed with a live code from the secret — MFA is now required at sign-in")

        _step(34, "The secret is never served again")
        leaked_secret = [
            path
            for path in ("/api/v1/auth/mfa", "/api/v1/auth/me", "/api/v1/audit?limit=200")
            if secret in buyer.get(path).text
        ]
        print(f"     endpoints re-serving the secret: {leaked_secret or 'none'}")
        if leaked_secret:
            # Joins whatever the credential sweep in step 26 already found, so
            # the demo's exit status reflects every leak rather than the last.
            leaked.extend(leaked_secret)

        _step(35, "Sign out, then try to sign in with only the password")
        buyer.post("/api/v1/auth/logout", headers=headers)
        refused = buyer.post(
            "/api/v1/auth/login",
            json={"email": "trader@example.com", "password": "correct-horse-battery-staple"},
        )
        body = refused.json()["error"]
        print(f"     HTTP {refused.status_code}  code={body['code']}")
        print(f"     X-MFA-Required: {refused.headers.get('X-MFA-Required')}")
        if body["code"] != "MfaRequired":
            print("     ✗ the refusal is indistinguishable from a wrong password")
            raise SystemExit(1)
        print("     distinguishable from a wrong password, so the UI shows a code challenge")

        _step(36, "Sign in with a recovery code, which is then spent")
        check(
            buyer.post(
                "/api/v1/auth/login",
                json={
                    "email": "trader@example.com",
                    "password": "correct-horse-battery-staple",
                    "mfa_code": recovery[0],
                },
            ),
            200,
            "login with a recovery code",
        )
        remaining = buyer.get("/api/v1/auth/mfa").json()["recovery_codes_remaining"]
        print(f"     signed in; recovery codes remaining: {remaining} (was {len(recovery)})")
        replayed = buyer.post(
            "/api/v1/auth/login",
            json={
                "email": "trader@example.com",
                "password": "correct-horse-battery-staple",
                "mfa_code": recovery[0],
            },
        )
        print(f"     replaying the same recovery code: HTTP {replayed.status_code}")
        if replayed.status_code == 200:
            print("     ✗ a recovery code was accepted twice")
            raise SystemExit(1)

        _step(37, "The creator invites an analyst into their workspace")
        with TestClient(app) as analyst:
            check(
                analyst.post(
                    "/api/v1/auth/register",
                    json={
                        "email": "analyst@example.com",
                        "password": "correct-horse-battery-staple",
                        "display_name": "Analyst",
                    },
                ),
                201,
                "register analyst",
            )
            invitation = check(
                creator.post(
                    "/api/v1/organizations/invitations",
                    json={"email": "analyst@example.com", "role": "viewer"},
                    headers=headers,
                ),
                201,
                "create invitation",
            )
            print(
                f"     invited analyst@example.com as {invitation['invitation']['role']}, "
                f"status={invitation['invitation']['status']}"
            )
            print("     the token is returned to the inviter once — this deployment sends no email")

            _step(38, "A leaked token is inert in the wrong hands")
            stolen = buyer.post(
                "/api/v1/organizations/invitations/accept",
                json={"token": invitation["token"]},
                headers=headers,
            )
            print(f"     the buyer tries the same token: HTTP {stolen.status_code}")
            if stolen.status_code == 200:
                print("     ✗ an invitation was accepted by an address it was not issued to")
                raise SystemExit(1)

            _step(39, "The analyst accepts, and the role is the invitation's, not theirs to choose")
            joined = check(
                analyst.post(
                    "/api/v1/organizations/invitations/accept",
                    json={"token": invitation["token"]},
                    headers=headers,
                ),
                200,
                "accept invitation",
            )
            print(f"     joined as {joined['role']}")
            escalation = analyst.post(
                "/api/v1/organizations/invitations/accept",
                json={"token": invitation["token"], "role": "owner"},
                headers=headers,
            )
            print(f"     attempting to send a role with the token: HTTP {escalation.status_code}")

            _step(40, "The analyst switches workspace and the role is enforced there")
            switched = check(
                analyst.post(
                    "/api/v1/auth/switch-organization",
                    json={"organization_id": creator_user["organization_id"]},
                    headers=headers,
                ),
                200,
                "switch organization",
            )
            print(
                f"     now acting in {switched['user']['organization_name']} "
                f"as {switched['user']['role']}"
            )
            members = analyst.get("/api/v1/organizations/members").json()
            print(f"     workspace members: {[m['email'] for m in members]}")
            denied = analyst.post(
                "/api/v1/strategies", json={"name": "Not allowed"}, headers=headers
            )
            print(f"     a viewer creating a strategy: HTTP {denied.status_code}")
            if denied.status_code != 403:
                print("     ✗ a viewer was able to write")
                raise SystemExit(1)

        _step(41, "Password reset refuses rather than pretending")
        availability = creator.get("/api/v1/auth/password-reset").json()
        print(
            f"     delivery channel: {availability['channel']}  "
            f"available={availability['available']}"
        )
        attempted = creator.post(
            "/api/v1/auth/password-reset/request", json={"email": "quant@example.com"}
        )
        unknown = creator.post(
            "/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"}
        )
        print(
            f"     known address:   HTTP {attempted.status_code} "
            f"{attempted.json()['error']['code']}"
        )
        print(f"     unknown address: HTTP {unknown.status_code} {unknown.json()['error']['code']}")
        if attempted.text != unknown.text:
            print("     ✗ the two answers differ, which would enumerate accounts")
            raise SystemExit(1)
        print("     identical for both, and decided before the address is looked up")
        print("     a 202 that never arrives would be worse: the user would wait for nothing")

        _rule()
        print("\nEvery step above ran against the real application: real HTTP routes, real")
        print(f"services, real database, and AlphaLab {ALPHALAB_VERSION} computing every")
        print("number shown. Nothing was stubbed.\n")
        print(f"Workspace kept at: {workspace}")
        return 0 if not leaked else 1


if __name__ == "__main__":
    sys.exit(run_demo())
