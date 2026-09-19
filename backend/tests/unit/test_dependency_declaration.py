"""Every third-party module the application imports must be declared.

Two dependencies have now shipped undeclared. ``pyotp`` was imported directly
and happened to be installed; ``email-validator`` was needed *transitively* by
``EmailStr`` and happened to be installed too. Both passed every test, because
the tests ran in the environment where the accident had already happened.

This catches the first kind — a direct import of something nobody declared.
It cannot catch the second: nothing in this repository imports
``email_validator``, pydantic does, so only installing into a clean
environment reveals it. That is what CI's `pip install -e ".[dev]"` on a fresh
runner is for, and it is why that step is not ceremony.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "iluvtrade"
PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"

#: Import name → the distribution that provides it, where they differ.
DISTRIBUTION_OF = {
    "argon2": "argon2-cffi",
    "sqlalchemy": "sqlalchemy",
    "pydantic_settings": "pydantic-settings",
    "multipart": "python-multipart",
    "dateutil": "python-dateutil",
    "jose": "python-jose",
    "yaml": "pyyaml",
}

#: Imported by the application and provided by something already declared.
TRANSITIVELY_PROVIDED = {
    # FastAPI re-exports it, and depends on it.
    "starlette": "fastapi",
    # uvicorn[standard] and httpx both pull it; nothing imports it directly.
    "anyio": "fastapi",
}


def _declared() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    requirements = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        requirements += list(extra)
    names = set()
    for requirement in requirements:
        # "pydantic[email]>=2.9,<3" -> "pydantic"
        name = requirement.split("[")[0]
        for separator in (">=", "<=", "==", "!=", "~=", ">", "<", ";", " "):
            name = name.split(separator)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def _top_level_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # Relative imports have no module of their own.
                modules = [node.module.split(".")[0]] if node.module and node.level == 0 else []
            else:
                continue
            for module in modules:
                found.setdefault(module, set()).add(str(path.relative_to(PACKAGE)))
    return found


def test_every_third_party_import_is_a_declared_dependency() -> None:
    declared = _declared()
    undeclared: dict[str, set[str]] = {}

    for module, files in sorted(_top_level_imports().items()):
        if module in sys.stdlib_module_names or module == "iluvtrade":
            continue
        if module in TRANSITIVELY_PROVIDED:
            assert TRANSITIVELY_PROVIDED[module] in declared
            continue
        distribution = DISTRIBUTION_OF.get(module, module).lower().replace("_", "-")
        if distribution not in declared:
            undeclared[f"{module} (-> {distribution})"] = files

    assert not undeclared, (
        f"These modules are imported but not declared in pyproject.toml: "
        f"{ {k: sorted(v) for k, v in undeclared.items()} }. They work here only "
        "because something else happened to install them; a clean environment "
        "would fail at import."
    )


def test_every_declared_dependency_is_importable() -> None:
    """The other direction: a declared name that does not resolve is a typo."""

    import importlib.metadata

    missing = []
    for name in sorted(_declared()):
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    # The postgres extra is deliberately not installed in the development
    # environment; see docs/DEPLOYMENT.md.
    missing = [name for name in missing if name != "psycopg"]
    assert not missing, f"Declared but not installed: {missing}"


def test_email_validation_actually_works() -> None:
    """The specific failure, pinned.

    ``EmailStr`` raises ``ImportError`` rather than a validation error when
    ``email-validator`` is absent, so every route with an email field fails at
    request time — registration, login, invitations, password reset. This
    exercises it directly so the dependency cannot quietly fall out again.
    """

    from iluvtrade.api.v1.schemas import RegisterRequest

    valid = RegisterRequest(
        email="someone@example.com",
        password="correct-horse-battery-staple",
        display_name="Someone",
    )
    assert valid.email == "someone@example.com"

    with pytest.raises(ValueError):
        RegisterRequest(
            email="not-an-email",
            password="correct-horse-battery-staple",
            display_name="Someone",
        )
