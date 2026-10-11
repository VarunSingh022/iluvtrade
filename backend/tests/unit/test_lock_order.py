"""The organization row is the audit chain's lock; no transaction may take two.

``platform.audit.record`` locks the organization row (FOR NO KEY UPDATE) before
appending to that organization's chain. Two transactions that each locked two
organizations in opposite orders would deadlock. None can: every audit call in
the application names one organization, and the flows that move a user between
organizations (invitation acceptance, switching) audit only the target. This
test reads the source and fails when a function starts auditing two
organizations, which is the change that would make lock order matter.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "iluvtrade"


def _audited_organizations(function: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "audit"
        ):
            for keyword in node.keywords:
                if keyword.arg == "organization_id":
                    found.add(ast.unparse(keyword.value))
    return found


def test_no_function_audits_more_than_one_organization() -> None:
    offenders = []
    calls = 0
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                organizations = _audited_organizations(node)
                calls += len(organizations)
                if len(organizations) > 1:
                    offenders.append(
                        f"{path.relative_to(SOURCE)}:{node.name} {sorted(organizations)}"
                    )
    assert calls > 10, "the scan found almost no audit calls; it is not reading the source"
    assert not offenders, (
        "These functions audit more than one organization, so they lock more than one "
        "organization row; give them a consistent lock order first: " + "; ".join(offenders)
    )


def test_the_audit_lock_is_no_key_update_on_the_organization_row() -> None:
    source = (SOURCE / "platform" / "audit.py").read_text()
    assert ".with_for_update(key_share=True)" in source
    assert "select(Organization.id)" in source
