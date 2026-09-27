"""Guards on the core dependency manifest and the locks compiled from it."""

import tomllib
from pathlib import Path
from typing import Any, Dict, List

import pytest

# backend/tests/test_dependency_manifest.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

LOCKS_COMPILED_FROM_PYPROJECT = [
    "requirements/runtime.txt",
    ".github/requirements/app-dev.txt",
    ".github/requirements/runtime-plugin-test.txt",
]

# Packages that no core module imports. They must not be installed into
# every core deployment.
UNUSED_CORE_PACKAGES = ["aiosmtplib"]


def _pyproject() -> Dict[str, Any]:
    """Load the repository pyproject.toml.

    Returns:
        The parsed TOML document.
    """
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _names(requirements: List[str]) -> List[str]:
    """Reduce requirement specifiers to lower-case distribution names.

    Args:
        requirements: PEP 508 requirement strings.

    Returns:
        The distribution names, lower-cased.
    """
    names = []
    for requirement in requirements:
        name = requirement
        for separator in ("[", "<", ">", "=", "!", "~", ";", " "):
            name = name.split(separator, 1)[0]
        names.append(name.strip().lower())
    return names


@pytest.mark.parametrize("package", UNUSED_CORE_PACKAGES)
def test_unused_package_not_declared(package: str) -> None:
    """A package no core module imports is not a core dependency."""
    assert package not in _names(_pyproject()["project"]["dependencies"])


@pytest.mark.parametrize("lock", LOCKS_COMPILED_FROM_PYPROJECT)
@pytest.mark.parametrize("package", UNUSED_CORE_PACKAGES)
def test_unused_package_not_locked(lock: str, package: str) -> None:
    """The locks are recompiled after a dependency is dropped."""
    text = (REPO_ROOT / lock).read_text(encoding="utf-8")
    assert f"\n{package}==" not in text, (
        f"{lock} still pins {package}. Recompile it with the uv command in "
        "its header comment."
    )


# Packages only the Enterprise Edition growth plugin imports. They live in the
# ``ee`` extra so core installs and the core locks do not carry them.
EE_ONLY_PACKAGES = ["maxminddb", "user-agents"]


@pytest.mark.parametrize("package", EE_ONLY_PACKAGES)
def test_ee_only_package_is_in_ee_extra(package: str) -> None:
    """EE-only packages are declared in the ``ee`` extra, not the core list."""
    project = _pyproject()["project"]
    assert package not in _names(project["dependencies"])
    assert package in _names(project["optional-dependencies"]["ee"])


@pytest.mark.parametrize("lock", LOCKS_COMPILED_FROM_PYPROJECT)
@pytest.mark.parametrize("package", EE_ONLY_PACKAGES)
def test_ee_only_package_not_in_core_locks(lock: str, package: str) -> None:
    """The core locks are compiled without the ``ee`` extra."""
    text = (REPO_ROOT / lock).read_text(encoding="utf-8")
    assert f"\n{package}==" not in text, (
        f"{lock} still pins {package}. Recompile it with the uv command in "
        "its header comment (the ee extra is not part of the core locks)."
    )
