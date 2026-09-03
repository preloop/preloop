"""Resolver for the current flow execution."""

from __future__ import annotations

import os
from typing import Optional

from .base import PromptResolver, ResolverContext

DEFAULT_PRELOOP_URL = "http://localhost:8000"


def execution_console_url(execution_id: str) -> str:
    """Return the console URL for a flow execution."""

    base = os.getenv("PRELOOP_URL", DEFAULT_PRELOOP_URL).rstrip("/")
    return f"{base}/console/flows/executions/{execution_id}"


class ExecutionResolver(PromptResolver):
    """Placeholders for the in-flight execution.

    - ``{{execution.id}}``
    - ``{{execution.url}}`` — console URL
    - ``{{execution.resume_from}}`` — prior execution id when this run
      was started from a PR comment on a PR this flow opened
    """

    @property
    def prefix(self) -> str:
        return "execution"

    async def resolve(self, path: str, context: ResolverContext) -> Optional[str]:
        if path == "id":
            return context.execution_id
        if path == "url":
            if not context.execution_id:
                return None
            return execution_console_url(context.execution_id)
        if path == "resume_from":
            resume = context.trigger_event_data.get("_resume") or {}
            prior = resume.get("execution_id")
            return str(prior) if prior else None
        self.logger.warning("Unknown execution field: %s", path)
        return None
