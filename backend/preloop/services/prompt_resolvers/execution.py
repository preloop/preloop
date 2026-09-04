"""Resolver for the current flow execution."""

from __future__ import annotations

import os
from typing import Optional

from preloop.services.flow_ci_feedback import CI_FAILURE_KEY

from .base import PromptResolver, ResolverContext

DEFAULT_PRELOOP_URL = "http://localhost:8000"

_PROVIDER_LABELS = {"github": "GitHub", "gitlab": "GitLab"}


def _render_ci_failure(failure: Optional[dict]) -> Optional[str]:
    """One human-readable line for the CI run that triggered this resume."""

    if not isinstance(failure, dict) or not failure:
        return None
    provider = str(failure.get("provider") or "").lower()
    label = _PROVIDER_LABELS.get(provider, provider or "CI")
    name = str(failure.get("name") or "job").strip() or "job"
    conclusion = str(failure.get("conclusion") or "failed").strip() or "failed"
    line = f"{label} {name} {conclusion}"
    url = str(failure.get("url") or "").strip()
    if url:
        line = f"{line}: {url}"
    return line


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
    - ``{{execution.ci_failure}}`` — one line describing the failing CI run
      that started this resume, empty when the run was not started by CI
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
        if path == "ci_failure":
            return _render_ci_failure(context.trigger_event_data.get(CI_FAILURE_KEY))
        self.logger.warning("Unknown execution field: %s", path)
        return None
