#!/usr/bin/env bash
#
# Verify the AlphaLab an environment holds, then run that release's consumer
# contract against it — AlphaLab docs/INTEGRATION.md, "Checklist", step 2.
#
#     scripts/verify-alphalab.sh /absolute/path/to/backend/.venv/bin/python
#
# The engine itself is installed by `pip install -e "backend[...]"`, from the
# hash-pinned direct reference in backend/pyproject.toml; pip refuses a wheel
# whose digest differs. This script checks what that left behind and runs the
# release's own contract, from a scratch directory outside every checkout so
# `alphalab` can only come from the environment:
#
#   1. `pip check` finds nothing broken;
#   2. `alphalab` and `alphalab.api` import, report VERSION, and load from the
#      environment's site-packages;
#   3. the release's sdist, checked against SDIST_SHA256, supplies
#      tests/consumer_contract.py, which must end with EXPECTED_LAST_LINE.
#
# Needs network (the sdist is downloaded), which is why it is a script CI and
# an operator run rather than a unit test. Exit 0 only if every step passed.
#
# Upgrading AlphaLab: change VERSION, SDIST_SHA256 and EXPECTED_LAST_LINE here
# together with the requirement in backend/pyproject.toml, each from the new
# release's SHA256SUMS file and INTEGRATION.md — never a value typed by hand.
# tests/unit/test_dependency_declaration.py fails if VERSION and the pin differ.

set -euo pipefail

VERSION="4.1.0"
# alphalab-4.1.0.tar.gz, from SHA256SUMS-4.1.0 of the v4.1.0 release.
SDIST_SHA256="355352eded23708e57a8649c9cb1c4c51eb8c9be717a6759d1cfdd3e5ee4dcaa"
EXPECTED_LAST_LINE="consumer contract: 9 checks passed"
RELEASE="https://github.com/VarunSingh022/AlphaLab/releases/download/v${VERSION}"

PYTHON="${1:?usage: verify-alphalab.sh /absolute/path/to/python}"
case "$PYTHON" in
  /*) ;;
  *) echo "verify-alphalab: give the interpreter as an absolute path" >&2; exit 2 ;;
esac

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"
# Nothing on PYTHONPATH may stand in for the installed package.
unset PYTHONPATH

echo "==> pip check"
"$PYTHON" -m pip check

echo "==> installed version and location"
"$PYTHON" - "$VERSION" <<'PY'
import pathlib
import sys
import sysconfig

import alphalab
import alphalab.api

expected = sys.argv[1]
location = pathlib.Path(alphalab.__file__).resolve()
site_packages = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
print(alphalab.__version__, alphalab.__file__)
if alphalab.__version__ != expected:
    sys.exit(f"alphalab {alphalab.__version__} is installed; {expected} is required")
if location != site_packages / "alphalab" / "__init__.py":
    sys.exit(f"alphalab loads from {location}, not this environment's {site_packages}")
PY

echo "==> consumer contract of the v${VERSION} release"
curl -fsSL --proto '=https' --retry 3 --connect-timeout 20 --max-time 600 \
  -o "alphalab-${VERSION}.tar.gz" "${RELEASE}/alphalab-${VERSION}.tar.gz"
"$PYTHON" - "alphalab-${VERSION}.tar.gz" "$SDIST_SHA256" <<'PY'
import hashlib
import sys

path, expected = sys.argv[1], sys.argv[2]
actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
if actual != expected:
    sys.exit(f"{path}: sha256 {actual}, expected {expected}; refusing to run it")
print(f"{path}: sha256 verified")
PY
tar -xzf "alphalab-${VERSION}.tar.gz"

output="$("$PYTHON" -W error "$scratch/alphalab-${VERSION}/tests/consumer_contract.py" "$VERSION")"
printf '%s\n' "$output"
last_line="$(printf '%s\n' "$output" | tail -n 1)"
if [ "$last_line" != "$EXPECTED_LAST_LINE" ]; then
  echo "verify-alphalab: the contract's last line was '$last_line', not '$EXPECTED_LAST_LINE'" >&2
  exit 1
fi
echo "==> AlphaLab ${VERSION} verified"
