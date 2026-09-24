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
    """

    db: Session
    trigger_event_data: Dict[str, Any]
    flow_id: str
    execution_id: str
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
    async def resolve(self, path: str, context: ResolverContext) -> Optional[str]:
        """
        Resolve a placeholder to its value.

        Args:
            path: The path after the prefix (e.g., "name" from "project.name")
            context: Resolver context with db, trigger data, etc.

        Returns:
            Resolved value as string, or None if not found

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
        keys = path.split(".")
        value = data

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default

        return str(value) if value is not None else default
