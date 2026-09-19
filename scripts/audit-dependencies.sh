#!/usr/bin/env bash
#
# Compare the current dependency advisories against the ones already assessed.
#
# Needs network: `npm audit` queries the registry. That is why it is a script
# an operator or CI runs rather than a unit test — a test that fails when the
# network is down teaches people to ignore it.
#
# Exit codes: 0 = nothing new, 1 = an advisory not in the accepted set.
#
# The accepted set and the reasoning behind each entry are in
# docs/SECURITY.md, under "Dependency risk". Adding a line here without
# adding the assessment there defeats the point.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCEPTED="$ROOT/scripts/accepted-advisories.txt"

cd "$ROOT/frontend"

echo "==> npm audit"
npm audit --json > /tmp/iluvtrade-audit.json || true

python3 - "$ACCEPTED" <<'PY'
import json, sys, pathlib

accepted = {
    line.split("#", 1)[0].strip()
    for line in pathlib.Path(sys.argv[1]).read_text().splitlines()
    if line.split("#", 1)[0].strip()
}
report = json.loads(pathlib.Path("/tmp/iluvtrade-audit.json").read_text())
found = set(report.get("vulnerabilities", {}))

unexpected = sorted(found - accepted)
resolved = sorted(accepted - found)

for name in sorted(found):
    entry = report["vulnerabilities"][name]
    mark = "NEW " if name in unexpected else "    "
    print(f"{mark}{entry['severity']:9} {name}")

if resolved:
    print()
    print("No longer reported (remove from accepted-advisories.txt):")
    for name in resolved:
        print(f"  {name}")

if unexpected:
    print()
    print(f"FAIL: {len(unexpected)} advisory/advisories not yet assessed: {unexpected}")
    print("Assess each in docs/SECURITY.md, then add it to scripts/accepted-advisories.txt.")
    sys.exit(1)

print()
print(f"OK: {len(found)} advisory/advisories, all previously assessed.")
PY
