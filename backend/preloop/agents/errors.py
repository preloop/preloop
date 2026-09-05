"""Typed failures raised while starting or supervising an agent runtime.

``FlowExecution.error_message`` is a string, so the orchestrator historically
had to re-read prose to decide what went wrong. These exceptions carry the
verdict alongside the message instead: the runner names the failure category
at the site that knows it (see
:mod:`preloop.services.flow_failure_category`), and the orchestrator stores
that name on the execution rather than guessing from text.
"""

from __future__ import annotations

from typing import Optional


class AgentStartError(RuntimeError):
    """An agent runtime could not be started.

    Attributes:
        category: ``FlowExecution.failure_category`` value describing the
            cause (e.g. ``runner_conflict`` for a Kubernetes Job name
            collision, ``runner_error`` for anything else that stopped the
            Job/container from being created). ``None`` leaves the
            orchestrator's text-based derivation in charge.

    There is deliberately no ``retryable`` flag. The only bounded retry of a
    Job/container start lives inside the runner, so an error that escapes has
    already exhausted it; the flow-level retry is scoped to the "agent process
    died" policy and never re-runs a start failure. A flag nothing reads would
    only suggest otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        category: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
