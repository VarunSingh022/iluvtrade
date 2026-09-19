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
