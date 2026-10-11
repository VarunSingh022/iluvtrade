#!/usr/bin/env bash
#
# Compare the current dependency advisories against the ones already assessed.
#
# Needs network: `npm audit` queries the registry. That is why it is a script
# an operator or CI runs rather than a unit test — a test that fails when the
# network is down teaches people to ignore it.
#
# Exit codes: 0 = nothing new, 1 = an advisory not in the accepted set,
# 2 = the audit could not be checked (no report, an npm error, an advisory
# without a GitHub id). Accepted advisories are matched by GHSA id *and*
# package: a new advisory against an accepted package fails.
#
# The accepted set and the reasoning behind each entry are in
# docs/SECURITY.md, under "Dependency risk". Adding a line here without
# adding the assessment there defeats the point.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCEPTED="$ROOT/scripts/accepted-advisories.txt"

cd "$ROOT/frontend"

echo "==> npm audit"
REPORT="$(mktemp -t iluvtrade-audit.XXXXXX)"
trap 'rm -f "$REPORT"' EXIT
# npm audit exits non-zero whenever it finds anything, so its status is not the
# verdict; check_advisories.py reads the report and fails closed if npm could
# not produce one (offline, registry error).
npm audit --json > "$REPORT" || true

python3 "$ROOT/scripts/check_advisories.py" "$ACCEPTED" "$REPORT"
