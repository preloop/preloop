"""Preloop - Automation Platform with Human Approval Layer"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path


def _resolve_version(default: str = "0.8.0") -> str:
    """Resolve the installed Preloop version.

    Mirrors ``preloop.config._load_release_version`` (importlib.metadata with
    a VERSION-file fallback for Docker/local dev) but deliberately avoids
    importing ``preloop.config`` here: that module pulls in pydantic settings
    and would create a circular import for anything doing
    ``from preloop import __version__``.
    """
    try:
        return _package_version("preloop")
    except PackageNotFoundError:
        pass

    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    try:
        v = version_file.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass

    return default


__version__ = _resolve_version()
