"""The dependency-advisory gate: matched by advisory id and package, failing closed.

Runs ``scripts/check_advisories.py`` -- the script CI and operators run -- on
synthetic ``npm audit --json`` reports, so no network is needed. The cases are
the ways the gate used to pass when it should not: a new advisory against a
package already accepted for a different advisory, and an audit that failed
outright (offline) read as "no findings".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check_advisories.py"
ACCEPTED = REPO / "scripts" / "accepted-advisories.txt"

OPEN_REDIRECT = "GHSA-wrjc-x8rr-h8h6"
SSR_INJECTION = "GHSA-337j-9hxr-rhxg"


def _advisory(ghsa: str | None, package: str = "react-router", severity: str = "moderate") -> dict:
    return {
        "source": 1,
        "name": package,
        "title": f"advisory {ghsa}",
        "url": f"https://github.com/advisories/{ghsa}" if ghsa else "",
        "severity": severity,
    }


def _report(*advisories: dict, inherited: dict[str, str] | None = None) -> dict[str, Any]:
    vulnerabilities: dict[str, Any] = {}
    for item in advisories:
        entry = vulnerabilities.setdefault(item["name"], {"severity": item["severity"], "via": []})
        entry["via"].append(item)
    for package, source in (inherited or {}).items():
        vulnerabilities[package] = {"severity": "moderate", "via": [source]}
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities}


def _run(tmp_path: Path, report: object, accepted: Path = ACCEPTED) -> subprocess.CompletedProcess:
    audit = tmp_path / "audit.json"
    audit.write_text(report if isinstance(report, str) else json.dumps(report))
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(accepted), str(audit)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_two_assessed_react_router_advisories_pass(tmp_path: Path) -> None:
    report = _report(
        _advisory(OPEN_REDIRECT),
        _advisory(SSR_INJECTION),
        inherited={"react-router-dom": "react-router"},
    )
    result = _run(tmp_path, report)
    assert result.returncode == 0, result.stdout
    assert "OK: 2 advisory/advisories" in result.stdout


def test_a_new_advisory_against_an_accepted_package_fails(tmp_path: Path) -> None:
    """Accepting react-router for two advisories is not accepting it for a third."""

    new = "GHSA-2222-3333-4444"
    report = _report(
        _advisory(OPEN_REDIRECT), _advisory(SSR_INJECTION), _advisory(new, severity="critical")
    )
    result = _run(tmp_path, report)
    assert result.returncode == 1, result.stdout
    assert f"NEW critical  {new} react-router" in result.stdout


def test_an_accepted_id_against_another_package_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, _report(_advisory(OPEN_REDIRECT, package="some-fork")))
    assert result.returncode == 1, result.stdout


def test_an_advisory_without_a_github_id_fails_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, _report(_advisory(OPEN_REDIRECT), _advisory(None)))
    assert result.returncode == 2, result.stdout
    assert "no GitHub advisory id" in result.stdout


@pytest.mark.parametrize(
    "report",
    [
        {"error": {"code": "ENOTFOUND", "summary": "request to registry.npmjs.org failed"}},
        {"auditReportVersion": 2},
        [],
        "",
        "not json",
        {"vulnerabilities": {"react-router": {"severity": "high", "via": []}}},
        # Reported via a package the report names no advisory for.
        _report(inherited={"react-router-dom": "react-router"}),
    ],
    ids=["npm-error", "no-vulnerabilities", "not-object", "empty", "garbage", "no-via", "orphan"],
)
def test_an_audit_that_cannot_be_read_fails_closed(tmp_path: Path, report: object) -> None:
    result = _run(tmp_path, report)
    assert result.returncode == 2, result.stdout
    assert "FAIL (could not check)" in result.stdout


def test_no_findings_passes_and_names_resolved_entries(tmp_path: Path) -> None:
    result = _run(tmp_path, {"auditReportVersion": 2, "vulnerabilities": {}})
    assert result.returncode == 0, result.stdout
    assert f"{OPEN_REDIRECT} react-router" in result.stdout  # listed for removal


def test_the_accepted_list_names_advisories_not_packages(tmp_path: Path) -> None:
    bare = tmp_path / "accepted.txt"
    bare.write_text("react-router   # a package name alone is no longer accepted\n")
    result = _run(tmp_path, _report(_advisory(OPEN_REDIRECT)), accepted=bare)
    assert result.returncode != 0
    assert "expected 'GHSA-xxxx-xxxx-xxxx package'" in (result.stdout + result.stderr)


def test_the_shell_gate_delegates_to_the_checker() -> None:
    script = (REPO / "scripts" / "audit-dependencies.sh").read_text()
    assert 'python3 "$ROOT/scripts/check_advisories.py" "$ACCEPTED" "$REPORT"' in script
    assert "set -euo pipefail" in script
