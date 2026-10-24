"""Base resolver interface for prompt placeholders."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class ResolverContext:
    """
    Context provided to resolvers for fetching data.

    Attributes:
        db: Database session for querying SpaceModels
        trigger_event_data: Data from the triggering event
        flow_id: UUID of the executing flow
        execution_id: UUID of the current execution
        account_id: Account owning the flow; scopes database lookups
    """

    db: Session
    trigger_event_data: Dict[str, Any]
    flow_id: str
    execution_id: str
    account_id: Optional[str] = None
    workspace_mode: str = "ephemeral"


class PromptResolver(ABC):
    """
    Abstract base class for placeholder resolvers.

    Resolvers are responsible for fetching and resolving specific types
    of placeholders in prompt templates (e.g., {{project.name}}, {{trigger_event.payload.title}}).
    """

    def __init__(self):
        """Initialize the resolver."""
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def prefix(self) -> str:
        """
        Return the placeholder prefix this resolver handles.

        Example: "project" for placeholders like {{project.name}}
        """
        pass

    @abstractmethod
    async def resolve(self, path: str, context: ResolverContext) -> Any:
        """
        Resolve a placeholder to its value.

        Args:
            path: The path after the prefix (e.g., "name" from "project.name")
            context: Resolver context with db, trigger data, etc.

        Returns:
            Resolved value, usually a string; None if not found. The
            orchestrator stringifies it, except that a trigger-event object or
            list is serialized into the prompt's data block.

        Example:
            For placeholder {{project.name}}, path would be "name"
        """
        pass

    def _safe_get_nested(
        self, data: Dict[str, Any], path: str, default: Optional[str] = None
    ) -> Optional[str]:
        """
        Safely get a nested value from a dictionary using dot notation.

        Args:
            data: Dictionary to query
            path: Dot-separated path (e.g., "payload.issue.title")
            default: Default value if path not found

        Returns:
            Value at path, or default if not found
        """
        value = self._get_nested_raw(data, path)
        return str(value) if value is not None else default

    @staticmethod
    def _get_nested_raw(data: Dict[str, Any], path: str) -> Any:
        """Like :meth:`_safe_get_nested`, but without stringifying the value.

        Args:
            data: Dictionary to query
            path: Dot-separated path (e.g., "payload.issue")

        Returns:
            The value at path as stored, or None if not found
        """
        value: Any = data
        for key in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(key)
            if value is None:
                return None
        return value
