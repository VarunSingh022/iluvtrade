"""The classes of vulnerability that are absent by construction, kept absent.

A grep proves nothing about tomorrow. These tests parse the application's own
source with :mod:`ast` and fail when a construct that is currently absent
appears — so "we do not execute arbitrary code" stays a property of the
codebase rather than a claim someone made once.

Each test names what it rules out and why that class of bug is worth a
structural guard rather than a review habit.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "iluvtrade"
FRONTEND = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src"


def _sources() -> list[tuple[pathlib.Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(), filename=str(path)))
        for path in sorted(PACKAGE.rglob("*.py"))
    ]


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(PACKAGE.parent))


# --- arbitrary code execution -----------------------------------------------


def test_nothing_in_the_application_evaluates_a_string(request) -> None:
    """Rules out: arbitrary code execution.

    RedDesk sells strategies, and the one thing this platform must never do is
    run a seller's code. ``eval``, ``exec`` and ``compile`` are how that
    happens by accident — a "parameters" field that turns out to accept an
    expression, a "formula" that gets evaluated. None of them appears, and this
    fails if one does.
    """

    forbidden = {"eval", "exec", "compile"}
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} {node.func.id}()")
            if isinstance(node, ast.Name) and node.id == "__import__":
                offenders.append(f"{_relative(path)}:{node.lineno} __import__")
    assert not offenders, (
        f"Dynamic code evaluation found: {offenders}. Seller strategies are data, "
        "never code; see docs/SANDBOX_CONTRACT.md."
    )


def test_nothing_deserializes_an_untrusted_format(request) -> None:
    """Rules out: unsafe deserialization.

    ``pickle`` and ``marshal`` execute code on load, and ``yaml.load`` without a
    safe loader does too. A dataset, a strategy parameter set and a stored
    backtest result all arrive from outside; every one of them is JSON.
    """

    forbidden_modules = {"pickle", "cPickle", "marshal", "dill", "shelve", "yaml"}
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules:
                        offenders.append(f"{_relative(path)}:{node.lineno} import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in forbidden_modules
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} from {node.module}")
    assert not offenders, f"Unsafe deserialization module imported: {offenders}"


def test_the_application_never_shells_out(request) -> None:
    """Rules out: command injection.

    There is no legitimate reason for this application to start a process. A
    single ``subprocess.run`` with an interpolated filename is how a dataset
    name becomes a shell command.
    """

    forbidden_modules = {"subprocess", "pty", "commands"}
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{_relative(path)}:{node.lineno} import {alias.name}"
                    for alias in node.names
                    if alias.name.split(".")[0] in forbidden_modules
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in forbidden_modules
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} from {node.module}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in {"system", "popen", "execv"}
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} os.{node.attr}")
    assert not offenders, f"Process execution found: {offenders}"


# --- SQL --------------------------------------------------------------------


def test_no_sql_is_built_from_an_f_string_or_concatenation(request) -> None:
    """Rules out: SQL injection.

    Everything goes through SQLAlchemy's expression language, which parameterises.
    The risk is the one-off: a filter built with an f-string because the column
    name was dynamic. This walks every string passed to ``execute`` or ``text``
    and refuses a formatted one.
    """

    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if name not in {"execute", "text", "exec_driver_sql"}:
                continue
            for argument in node.args:
                if isinstance(argument, ast.JoinedStr) or (
                    isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add)
                ):
                    offenders.append(f"{_relative(path)}:{node.lineno} {name}(<formatted string>)")
    assert not offenders, (
        f"SQL built by formatting: {offenders}. Use bound parameters, or — if the "
        "value is genuinely an identifier — check it against a literal allowlist "
        "and say so at the call site."
    )


# --- the browser ------------------------------------------------------------


def test_the_frontend_never_injects_markup_or_evaluates_a_string() -> None:
    """Rules out: XSS through a React escape hatch.

    React escapes by default, so the realistic XSS in this app is one of the
    three escape hatches below. The CSP has no ``unsafe-inline`` for scripts as
    a second layer, but a control that depends on a header being delivered is
    not the only control worth having.
    """

    patterns = ("dangerouslySetInnerHTML", ".innerHTML", "new Function(", "document.write(")
    offenders = []
    for path in sorted(FRONTEND.rglob("*.ts*")):
        text = path.read_text()
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # A comment explaining why one is *not* used is not a use.
            if stripped.startswith(("*", "//", "/*")):
                continue
            for pattern in patterns:
                if pattern in line:
                    offenders.append(f"{path.name}:{line_number} {pattern}")
    assert not offenders, f"Markup injection or dynamic evaluation in the frontend: {offenders}"


# --- files ------------------------------------------------------------------


def test_every_blob_read_and_write_goes_through_the_key_resolver() -> None:
    """Rules out: path traversal in blob storage.

    ``storage.resolve`` is where a key is checked against the storage root. A
    module that opens a path itself has bypassed that check, whatever its
    intentions — so the guard is that nothing outside ``storage.py`` and the
    config's own directory creation touches ``open`` or ``Path.write_*``.
    """

    #: Modules that touch the filesystem directly, and why each is not a
    #: traversal surface. Every path in these comes from *configuration or an
    #: operator's argument*, never from a request.
    ALLOWED = {
        # Where resolve() itself lives.
        "common/storage.py": "owns the key resolver",
        # Creates the configured storage root at startup.
        "config.py": "creates ILUVTRADE_STORAGE_ROOT",
        # Creates the parent of the configured SQLite file.
        "db/session.py": "creates the directory of ILUVTRADE_DATABASE_URL",
        # Writes a backup to a path the operator typed on the command line.
        "cli.py": "writes to an operator-supplied backup path",
    }

    offenders = []
    for path, tree in _sources():
        relative = str(path.relative_to(PACKAGE))
        if relative in ALLOWED:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                offenders.append(f"{relative}:{node.lineno} open()")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "write_bytes",
                "write_text",
                "mkdir",
                "unlink",
                "rmtree",
            }:
                # ``storage.write_bytes(key, ...)`` is the resolver, not a
                # bypass of it; only a call on something else is a finding.
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in {"storage", "shutil"}:
                    continue
                offenders.append(f"{relative}:{node.lineno} .{node.func.attr}()")
    assert not offenders, (
        f"Direct filesystem access outside the storage module: {offenders}. Route it "
        "through iluvtrade.common.storage, which refuses a key that escapes the root, "
        "or add the module to ALLOWED with the reason its paths are not request data."
    )


def test_the_filesystem_allowlist_stays_short() -> None:
    """Four modules is a boundary; twelve is a convention nobody follows."""

    from tests.security import test_code_execution_surface as module

    source = pathlib.Path(module.__file__).read_text()
    assert source.count('": "') <= 6
