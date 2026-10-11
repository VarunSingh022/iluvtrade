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
import re
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


#: The one acceptable shape for the engine requirement: AlphaLab's own GitHub
#: release asset, by direct reference, with its SHA-256. AlphaLab is not on
#: PyPI, so a bare ``alphalab==X`` resolves against a public index and installs
#: whoever registers the name (AlphaLab ``docs/INTEGRATION.md``).
_ALPHALAB_REQUIREMENT = re.compile(
    r"^alphalab @ (?P<url>https://github\.com/VarunSingh022/AlphaLab/releases/download/"
    r"v(?P<version>\d+\.\d+\.\d+)/alphalab-(?P=version)-py3-none-any\.whl)"
    r"#sha256=(?P<sha256>[0-9a-f]{64})$"
)


def _alphalab_requirements() -> list[str]:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    requirements = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        requirements += list(extra)
    return [r for r in requirements if r.split("[")[0].split(" ")[0].split("=")[0] == "alphalab"]


def test_alphalab_is_pinned_to_its_release_artifact_by_hash() -> None:
    requirements = _alphalab_requirements()
    assert len(requirements) == 1, f"Exactly one AlphaLab requirement expected: {requirements}"
    assert _ALPHALAB_REQUIREMENT.match(requirements[0]), (
        f"{requirements[0]!r} is not a hash-pinned direct reference to an AlphaLab "
        "release wheel. Write it from the release's SHA256SUMS file."
    )


def test_the_installed_alphalab_is_the_declared_artifact() -> None:
    """Not merely "a version 4.1.0 imports": the declared wheel, at its hash.

    pip records where an installed distribution came from in
    ``direct_url.json``. Comparing it with the declaration catches an engine
    installed from a local wheel, an editable checkout or an index — any of
    which can report the right version while being different code.
    """

    import importlib.metadata
    import json

    import alphalab

    declared = _ALPHALAB_REQUIREMENT.match(_alphalab_requirements()[0])
    assert declared is not None
    distribution = importlib.metadata.distribution("alphalab")
    origin = json.loads(distribution.read_text("direct_url.json") or "{}")

    assert distribution.version == declared["version"]
    assert alphalab.__version__ == declared["version"]
    assert origin.get("url") == declared["url"], origin
    assert origin.get("archive_info", {}).get("hashes", {}).get("sha256") == declared["sha256"]
    assert "editable" not in origin.get("dir_info", {}), origin

    # Inside *this interpreter's* site-packages — not merely a path containing
    # "site-packages", and not a checkout that happens to be on sys.path.
    import sysconfig

    location = pathlib.Path(alphalab.__file__).resolve()
    site_packages = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
    assert location == site_packages / "alphalab" / "__init__.py", (
        f"alphalab imports from {location}, not from this environment's {site_packages}."
    )


def test_the_release_verification_script_names_the_pinned_release() -> None:
    """``scripts/verify-alphalab.sh`` checks the version the pin installs.

    It carries the release's version and its sdist digest (for the consumer
    contract), so an upgrade that moves the pin and forgets the script would
    verify an engine the application no longer installs. Read from both files.
    """

    script = (PYPROJECT.parent.parent / "scripts" / "verify-alphalab.sh").read_text()
    declared = _ALPHALAB_REQUIREMENT.match(_alphalab_requirements()[0])
    assert declared is not None

    version = re.search(r'^VERSION="([^"]+)"$', script, re.MULTILINE)
    sdist = re.search(r'^SDIST_SHA256="([0-9a-f]{64})"$', script, re.MULTILINE)
    assert version is not None and version[1] == declared["version"]
    assert sdist is not None, "the sdist the contract comes from must be hash-pinned"
