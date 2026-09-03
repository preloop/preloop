"""Prompt placeholder resolution system for dynamic context injection."""

from .base import PromptResolver, ResolverContext
from .registry import resolver_registry
from .trigger_event import TriggerEventResolver
from .project import ProjectResolver
from .account import AccountResolver
from .execution import ExecutionResolver

__all__ = [
    "PromptResolver",
    "ResolverContext",
    "resolver_registry",
    "TriggerEventResolver",
    "ProjectResolver",
    "AccountResolver",
    "ExecutionResolver",
]
