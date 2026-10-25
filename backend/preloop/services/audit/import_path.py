"""Resolve legacy audit imports to the single core implementation."""

from importlib import import_module
from types import ModuleType


def canonical_audit_package() -> str:
    """Return the package that owns the audit service and writer."""
    return "preloop.services.audit"


def import_audit_submodule(name: str) -> ModuleType:
    """Resolve an audit submodule without creating legacy module state."""
    if name == "endpoints":
        return import_module("preloop.api.endpoints.audit")
    if name == "session_search_audit":
        return import_module("preloop.services.session_search_audit")
    return import_module(f"{canonical_audit_package()}.{name}")
