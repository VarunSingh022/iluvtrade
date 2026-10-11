"""AlphaLab may only be imported inside the bridge.

This is the architectural rule the whole build rests on, so it is checked by
walking the source tree rather than trusted to code review.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "iluvtrade"
BRIDGE = PACKAGE / "alphalab_bridge"


def _alphalab_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "alphalab"]
        elif isinstance(node, ast.ImportFrom) and (
            node.module and node.module.split(".")[0] == "alphalab"
        ):
            found.append(node.module)
    return found


def test_only_the_bridge_imports_alphalab() -> None:
    offenders: dict[str, list[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        if BRIDGE in path.parents or path == BRIDGE:
            continue
        imports = _alphalab_imports(path)
        if imports:
            offenders[str(path.relative_to(PACKAGE))] = imports

    assert not offenders, (
        "AlphaLab must only be imported from iluvtrade/alphalab_bridge/. These modules "
        f"import it directly: {offenders}. Route the call through the bridge instead — "
        "the boundary is one directory, not a convention."
    )


def test_the_bridge_actually_imports_alphalab() -> None:
    """A boundary nothing crosses would pass the test above vacuously."""

    total = sum(len(_alphalab_imports(p)) for p in BRIDGE.rglob("*.py"))
    assert total > 10, f"The bridge imports AlphaLab only {total} times; that looks wrong."


def test_no_module_recreates_an_alphalab_concept() -> None:
    """A crude guard against a second engine appearing by accident.

    Names a short list of class names that would indicate duplicated engine
    semantics. Not exhaustive, and not a substitute for review — but a
    ``class PortfolioEngine`` appearing in this repository is worth failing on.
    """

    forbidden = {
        # portfolio and execution
        "PortfolioEngine",
        "BacktestEngine",
        "ExecutionPipeline",
        "OrderManagementSystem",
        "OrderRouter",
        "MatchingEngine",
        # accounting
        "Ledger",
        "AccountingEngine",
        "PositionKeeper",
        "PnLCalculator",
        "Valuation",
        # risk
        "RiskEngine",
        "RiskManager",
        # strategy runtime
        "StrategyRegistry",
        "StrategyRuntime",
        "SignalEngine",
        # market data and FX
        "MarketDataset",
        "FxConverter",
        "CurrencyConverter",
        "ExchangeRateEngine",
    }
    offenders: dict[str, list[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in forbidden
        ]
        if defined:
            offenders[str(path.relative_to(PACKAGE))] = defined
    assert not offenders, (
        f"These modules define a class AlphaLab already owns: {offenders}. "
        "iluvtrade orchestrates the engine; it does not reimplement it."
    )


#: Substrings that name a *performance* quantity. AlphaLab computes every one
#: of them; this repository projects them onto an API response and nothing more.
_METRIC_WORDS = (
    "sharpe",
    "sortino",
    "drawdown",
    "cagr",
    "volatility",
    "annualis",
    "annualiz",
    "total_return",
    "compute_pnl",
    "calculate_pnl",
    "mark_to_market",
    "convert_currency",
)


def test_no_module_computes_a_performance_metric() -> None:
    """The stronger half of the boundary: not just "no second engine", but no
    second *answer*.

    A class named ``BacktestEngine`` is easy to notice in review. A helper
    called ``_sharpe`` inside a reporting module is not, and it is the more
    likely way a second set of numbers appears — one the API returns and one
    the engine believes, disagreeing by a rounding convention nobody wrote down.

    Metrics reach this application already computed, through
    ``alphalab_bridge/results.py``, which projects and does not calculate.
    """

    offenders: dict[str, list[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        named = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and any(word in node.name.lower() for word in _METRIC_WORDS)
        ]
        if named:
            offenders[str(path.relative_to(PACKAGE))] = named

    assert not offenders, (
        f"These modules define something that computes a performance quantity: "
        f"{offenders}. AlphaLab is the sole quantitative authority — a second "
        "implementation is a second answer, and the two will disagree."
    )


def test_the_bridge_projects_results_rather_than_computing_them() -> None:
    """``results.py`` must contain no arithmetic on engine output.

    The bridge is allowed to *import* AlphaLab; it is not allowed to become a
    quiet second calculator. Division is the tell — every ratio metric is a
    division, so a ``/`` in the projection layer is where a locally computed
    return would appear.
    """

    source = (BRIDGE / "results.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    divisions = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div | ast.FloorDiv)
    ]
    assert not divisions, (
        f"alphalab_bridge/results.py divides at line(s) {divisions}. This module "
        "projects what the engine returned; a ratio computed here is a second "
        "answer to a question AlphaLab has already answered."
    )


#: The modules AlphaLab's public API covers besides its packages: ``alphalab.api``
#: and the documented state modules (AlphaLab 4.1.0 ``docs/api/PUBLIC_API.md``
#: and ``public_api.json``). Every other non-package module is AlphaLab's own.
_DOCUMENTED_MODULES = frozenset(
    {
        "alphalab.api",
        "alphalab.allocation.snapshot",
        "alphalab.backtesting.replay_snapshot",
        "alphalab.broker.snapshot",
        "alphalab.instrument.snapshot",
        "alphalab.lifecycle.snapshot",
        "alphalab.oms.snapshot",
        "alphalab.portfolio.fx_feed",
        "alphalab.portfolio.snapshot",
        "alphalab.runtime.live_snapshot",
        "alphalab.runtime.run_snapshot",
        "alphalab.runtime.snapshot",
    }
)


def test_the_bridge_imports_only_alphalabs_public_api() -> None:
    """Every AlphaLab name the bridge uses is one AlphaLab promises to keep.

    AlphaLab's public API is each *package's* ``__all__`` plus the documented
    modules above; "any other module's names ... may change in any release",
    and moving a class between submodules is not a breaking change there. Up to
    AlphaLab 3.5 the bridge imported 71 names from submodules such as
    ``alphalab.risk.limits`` — each one public, but by a path a 4.x patch release
    is free to move. Checked against the *installed* package, so the rule is
    the engine's own ``__all__``, not a copy of it that could drift.
    """

    import importlib

    offenders: list[str] = []
    checked = 0
    for path in sorted(BRIDGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            where = f"{path.relative_to(PACKAGE)}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Import):
                offenders += [
                    f"{where} import {alias.name} (import names from the package instead)"
                    for alias in node.names
                    if alias.name.split(".")[0] == "alphalab" and alias.name != "alphalab"
                ]
                continue
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "alphalab"
            ):
                continue
            module = importlib.import_module(node.module)
            surface = hasattr(module, "__path__") or node.module in _DOCUMENTED_MODULES
            exported = set(getattr(module, "__all__", ()))
            for alias in node.names:
                checked += 1
                if not surface:
                    offenders.append(f"{where} {node.module} is not a public AlphaLab module")
                elif alias.name not in exported:
                    offenders.append(f"{where} {node.module}.{alias.name} is not in its __all__")

    assert checked > 10, f"Only {checked} AlphaLab names imported; the walk found nothing."
    assert not offenders, (
        "The bridge imports AlphaLab names outside its public API:\n  "
        + "\n  ".join(offenders)
        + "\nImport each from its package (e.g. `from alphalab.risk import RiskLimits`)."
    )
