#!/usr/bin/env python3
"""Compare an ``npm audit --json`` report with the advisories already assessed.

Usage: check_advisories.py ACCEPTED_FILE AUDIT_JSON

An advisory is accepted by its **GitHub advisory id and the package it is
reported against** -- never by package name alone. Accepting a package by name
meant that any *new* advisory against that package passed unnoticed, however
severe.

Fails closed. Exit codes:

* 0 -- every reported advisory is accepted;
* 1 -- an advisory that has not been assessed (new id, or a known id against
  another package);
* 2 -- the report cannot be trusted: unreadable, an ``npm audit`` error
  (offline, registry failure), not the expected shape, or a finding whose
  advisory id cannot be determined. "Could not check" is never "nothing found".

``ACCEPTED_FILE`` holds one ``GHSA-xxxx-xxxx-xxxx package`` pair per line;
``#`` starts a comment. The assessment behind each entry is in
docs/SECURITY.md, "Dependency risk".
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GHSA = re.compile(r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}")


class Untrustworthy(Exception):
    """The report cannot be read as a complete list of findings."""


def read_accepted(path: Path) -> set[tuple[str, str]]:
    accepted: set[tuple[str, str]] = set()
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not GHSA.fullmatch(parts[0]):
            raise SystemExit(
                f"{path}:{number}: expected 'GHSA-xxxx-xxxx-xxxx package', got {raw.strip()!r}"
            )
        accepted.add((parts[0], parts[1]))
    return accepted


def findings(report: object) -> tuple[set[tuple[str, str]], dict[str, str]]:
    """Every ``(advisory id, package)`` the report names, and each id's severity.

    A package reported only *via* another package carries no advisory of its own;
    it is covered by the advisory it inherits, which must itself be reported.
    """

    if not isinstance(report, dict):
        raise Untrustworthy("the report is not a JSON object")
    if "error" in report:
        error = report["error"]
        summary = error.get("summary") if isinstance(error, dict) else error
        raise Untrustworthy(f"npm audit reported an error: {summary}")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise Untrustworthy("the report has no 'vulnerabilities' object")

    found: set[tuple[str, str]] = set()
    severity: dict[str, str] = {}
    inherited: dict[str, set[str]] = {}
    for package, entry in vulnerabilities.items():
        via = entry.get("via") if isinstance(entry, dict) else None
        if not isinstance(via, list) or not via:
            raise Untrustworthy(f"{package}: the finding names no advisory")
        for item in via:
            if isinstance(item, str):
                inherited.setdefault(package, set()).add(item)
                continue
            if not isinstance(item, dict):
                raise Untrustworthy(f"{package}: unreadable advisory entry {item!r}")
            match = GHSA.search(str(item.get("url") or ""))
            if match is None:
                raise Untrustworthy(
                    f"{package}: advisory {item.get('title') or item.get('source')!r} has no "
                    "GitHub advisory id, so it cannot be matched against an assessment"
                )
            name = str(item.get("name") or package)
            found.add((match.group(0), name))
            severity[match.group(0)] = str(item.get("severity") or "unknown")
    direct = {name for _, name in found}
    for package, sources in inherited.items():
        for source in sources:
            if source not in direct and source not in inherited:
                raise Untrustworthy(
                    f"{package} is reported via {source}, which the report names no advisory for"
                )
    return found, severity


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    accepted = read_accepted(Path(argv[1]))
    try:
        report = json.loads(Path(argv[2]).read_text())
        found, severity = findings(report)
    except (OSError, ValueError, Untrustworthy) as exc:
        print(f"FAIL (could not check): {exc}")
        return 2

    unexpected = sorted(found - accepted)
    resolved = sorted(accepted - found)
    for advisory, package in sorted(found):
        mark = "NEW " if (advisory, package) in unexpected else "    "
        print(f"{mark}{severity[advisory]:9} {advisory} {package}")
    if resolved:
        print("\nNo longer reported (remove from the accepted list):")
        for advisory, package in resolved:
            print(f"  {advisory} {package}")
    if unexpected:
        print(f"\nFAIL: {len(unexpected)} advisory/advisories not yet assessed:")
        for advisory, package in unexpected:
            print(f"  {advisory} {package}")
        print("Assess each in docs/SECURITY.md, then add it to scripts/accepted-advisories.txt.")
        return 1
    print(f"\nOK: {len(found)} advisory/advisories, all previously assessed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
