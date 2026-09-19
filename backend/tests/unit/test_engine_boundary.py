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
        "PortfolioEngine",
        "BacktestEngine",
        "ExecutionPipeline",
        "OrderManagementSystem",
        "RiskEngine",
        "StrategyRegistry",
        "MarketDataset",
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
