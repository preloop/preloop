"""Resolver for the current flow execution."""

from __future__ import annotations

import os
from typing import Optional

from .base import PromptResolver, ResolverContext

DEFAULT_PRELOOP_URL = "http://localhost:8000"

RESUME_REBASE_CONFLICT_HINT = (
    "If /workspace/evidence/rebase-conflict.txt exists, git rebase onto "
    "the flow base branch conflicted and was aborted. Inspect that file "
    "and resolve the listed paths before continuing."
)


def resume_rebase_conflict_hint(trigger_event_data: Optional[dict]) -> str:
    """Instruction appended to resume prompts so the agent inspects conflicts.

    The rebase runs inside the container after this prompt is resolved, so
    the hint is always present on resume runs and the agent checks the
    evidence file if it appears.
    """

    if not isinstance(trigger_event_data, dict):
        return ""
    resume = trigger_event_data.get("_resume")
    if not resume:
        return ""
    return "\n\n" + RESUME_REBASE_CONFLICT_HINT


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

    Resume rebase conflicts are not a prompt placeholder. Resume runs
    always get an instruction to inspect
    ``/workspace/evidence/rebase-conflict.txt`` if that file exists after
    init. The container also exports ``PRELOOP_RESUME_REBASE_CONFLICT=1``
    for in-container processes; the control plane never sees that env var.
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
