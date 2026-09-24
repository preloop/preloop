"""Resolver for the workspace contract on a flow prompt."""

from __future__ import annotations

from typing import Optional

from .base import PromptResolver, ResolverContext


class WorkspaceResolver(PromptResolver):
    """Placeholders for where this run checks out code.

    ``{{workspace.mode}}`` is ``ephemeral`` for a container run,
    ``persistent_checkout`` when a persistent agent should reuse a host
    checkout, and ``clone_less`` when the review reads the tracker diff
    and does not clone.
    """

    @property
    def prefix(self) -> str:
        return "workspace"

    async def resolve(self, path: str, context: ResolverContext) -> Optional[str]:
        if path == "mode":
            return context.workspace_mode or "ephemeral"
        self.logger.warning("Unknown workspace field: %s", path)
        return None
