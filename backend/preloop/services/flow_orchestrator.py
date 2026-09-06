import logging
import uuid
import json
import asyncio
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import re

from sqlalchemy.orm import Session
from nats.aio.client import Client

from preloop.models import schemas
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
    crud_runtime_session,
    crud_user,
)
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
    resolve_execution_agent_selection,
)
from preloop.models.models.ai_model import AIModel
from preloop.models.models.runtime_session import RuntimeSession
from preloop.agents import (
    create_executor_for_execution,
    AgentStatus,
)
from preloop.agents.container import AGENT_SESSION_SUFFIX_KEY
from preloop.agents.kubernetes import detect_kubernetes_environment
from preloop.agents.cli_session import (
    AGENT_SESSION_MARKER,
    extract_session_pack,
    parse_agent_session_marker,
)
from preloop.agents.completion_nudge import (
    COMPLETION_NUDGE_MARKER,
    COMPLETION_NUDGE_RESULT_MARKER,
    COMPLETION_NUDGE_UNSUPPORTED_MARKER,
)
from preloop.agents.failure_analysis import analyze_agent_failure
from preloop.agents.verification import (
    VERIFICATION_DENIED_MARKER,
    VERIFICATION_MARKER,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_UNKNOWN,
    derive_failure_category,
)
from preloop.services.flow_execution_notifications import (
    needs_tracker_comment,
    notify_terminal_execution,
)
from preloop.services.prompt_resolvers.execution import execution_console_url
from preloop.config import settings
from preloop.services.flow_pr_binding import (
    PR_OPENED_MARKER,
    find_bound_execution,
    max_resumes_per_pr,
    merge_result_preserving_pr_binding,
    note_resume_started,
    parse_pr_opened_marker,
    record_cli_session,
    record_opened_pr,
    resume_cli_session_of,
    resume_count,
    take_pending_followup,
)
from preloop.services.prompt_resolvers import (
    resolver_registry,
    ResolverContext,
    TriggerEventResolver,
    ProjectResolver,
    AccountResolver,
    ExecutionResolver,
)
from preloop.services.prompt_resolvers.execution import resume_rebase_conflict_hint
from preloop.services.flow_execution_logger import FlowExecutionLogger
from preloop.services.flow_runtime_token import (
    create_flow_runtime_token,
    revoke_flow_runtime_tokens,
)
from preloop.services.tracker_git_token import resolve_tracker_git_token
from preloop.sync.event_normalizer import attach_trigger_subject
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.utils.git_credentials import (
    GitCredential,
    credential_username,
    strip_url_credentials,
    temporary_credential_file,
)
from preloop.utils.repo_urls import repo_url_log_location, tracker_host_kind
from preloop.utils.workspace_seed import (
    attach_workspace_file_paths,
    parse_workspace_files,
)
from preloop.utils.secret_scrubbing import scrub_secrets
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

# Statuses after which no agent of this execution can still be running, so its
# runtime credentials can be retired. Anything else means the agent may still
# be live (an interrupted run is resumed by a peer worker), and its gateway
# token must keep working.
TERMINAL_EXECUTION_STATUSES = frozenset({"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"})

# Sentinel string that agents print when completing successfully.
FLOW_SUCCESS_SENTINEL = "FLOW_EXECUTION_SUCCESS"

# Marker printed by the agent script immediately before the agent command runs.
# Sentinel detection is suppressed until this marker is seen in the logs,
# preventing false positives from the prompt echo that contains the sentinel
# instruction text.
AGENT_EXEC_START_MARKER = "PRELOOP_AGENT_EXEC_START"
MCP_TOOL_LOOP_PATTERN_MAX_LENGTH = 3
MCP_TOOL_LOOP_MIN_REPETITIONS = 3
MCP_TOOL_LOOP_SINGLE_CALL_REPETITIONS = 4
MCP_TOOL_LOOP_DUPLICATE_WINDOW_SECONDS = 0.5

# Instruction appended to prompts to have agents signal success.
# IMPORTANT: The sentinel is kept INLINE (not on its own line) so that when
# the prompt text is echoed in logs, it cannot trigger the exact-line detector.
FLOW_SUCCESS_INSTRUCTION = f"""

---
IMPORTANT: When you have successfully completed your task, you MUST confirm success in one of two ways: print the following marker on a line by itself (no other text on that line): {FLOW_SUCCESS_SENTINEL}
or write /workspace/result.json with a recognized completion status. Statuses such as "success", "pass", and "fail" confirm completion. Preserve any richer structured report instead of replacing it with a bare status object. Without one of these confirmations the run is marked FAILED.
---"""

FLOW_EVAL_SUCCESS_INSTRUCTION = """

---
IMPORTANT: Your existing structured /workspace/result.json report is the flow confirmation channel. Preserve its schema and all rich report fields; do not overwrite it with a bare status object. The `pass` and `fail` verdicts confirm that the flow completed. An `error` verdict means the evaluation could not complete. Do not print sentinel markers.
---"""

# Instruction for audit flows (CRA/RSA preset family) whose result contract
# uses a top-level "verdict" instead of "status". Mirrors the eval precedent:
# the structured result.json IS the confirmation channel, and a failing audit
# is a completed flow. Unlike the eval instruction, the sentinel is not
# forbidden — printing it additionally is harmless, and it stays REQUIRED for
# contracts without a top-level "verdict" AND without a top-level "status"
# field, which otherwise have no result.json confirmation at all.
# (preloop.cra.vulnscan/v1 carries a top-level "status" completion field
# since preset revision #283, so it uses the generic instruction, whose
# result.json-status branch matches its contract.)
# NOTE: the sentinel is kept INLINE (see FLOW_SUCCESS_INSTRUCTION) so the
# prompt echo cannot trigger the exact-line detector.
FLOW_AUDIT_SUCCESS_INSTRUCTION = f"""

---
IMPORTANT: Your structured /workspace/result.json report is the flow confirmation channel: a top-level "verdict" of "pass", "pass_with_findings", or "fail" confirms that the flow ran to completion (a failing audit is still a completed audit); a verdict of "error" means the audit itself could not complete. Preserve the exact result schema your instructions require and all rich report fields; never overwrite the file with a bare status object. Additionally printing the success marker on a line by itself (no other text on that line) after writing the file is allowed and harmless: {FLOW_SUCCESS_SENTINEL}
If your required result shape has NO top-level "verdict" field, you MUST print that marker after writing the file — without it the run is marked FAILED.
---"""

# Schema ids of the audit result contracts (presets 004-006) whose top-level
# "verdict" vocabulary is pass | pass_with_findings | fail (+ error). The
# due-diligence contract (preloop.cra.duediligence/v1, verdict
# "recorded" | "error") is deliberately NOT listed: its verdict vocabulary is
# not a recognized completion confirmation, so those flows keep the generic
# sentinel instruction.
AUDIT_RESULT_SCHEMA_MARKERS = (
    "preloop.cra.sbomaudit/v1",
    "preloop.cra.releaseaudit/v1",
)


def _success_instruction_for_prompt(prompt: str) -> str:
    """Select the completion instruction matching the prompt's result contract."""
    normalized_prompt = prompt.lower()
    if (
        "preloop.eval.result/v1" in normalized_prompt
        or "do not print sentinel markers" in normalized_prompt
    ):
        return FLOW_EVAL_SUCCESS_INSTRUCTION
    if any(marker in normalized_prompt for marker in AUDIT_RESULT_SCHEMA_MARKERS):
        return FLOW_AUDIT_SUCCESS_INSTRUCTION
    return FLOW_SUCCESS_INSTRUCTION


# result.json "status" values that count as an explicit success confirmation
# (channel 2 — equal in standing to the printed sentinel) or an explicit
# flow-failure report. Eval vocabulary (preloop.eval.result/v1): "pass" and
# "fail" are completed-run verdicts (the subject's checks passed or failed);
# only "error" means the eval itself could not complete. "fail" is therefore
# a success confirmation for the flow, not a flow-failure signal.
RESULT_ARTIFACT_SUCCESS_STATUSES = frozenset(
    {"success", "succeeded", "pass", "passed", "fail"}
)
RESULT_ARTIFACT_FAILURE_STATUSES = frozenset({"failure", "failed", "error"})

# Statuses that mean the work did not finish. On the widened-signal path
# (runtimes that cannot be resumed) these must not count as success: a
# report that says "timeout" is the agent saying it ran out of time.
RESULT_ARTIFACT_INCOMPLETE_STATUSES = frozenset(
    {
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
        "partial",
        "in_progress",
        "running",
        "pending",
    }
)

# Audit vocabulary (preloop.cra.sbomaudit/v1, preloop.cra.releaseaudit/v1):
# the top-level field is "verdict", never "status". As with eval, "fail" and
# "pass_with_findings" are completed-run verdicts — the audit ran to
# completion and reported its outcome — so they confirm the FLOW succeeded;
# only "error" means the audit itself could not complete.
RESULT_ARTIFACT_VERDICT_SUCCESSES = frozenset(
    {"pass", "passed", "pass_with_findings", "fail"}
)
RESULT_ARTIFACT_VERDICT_FAILURES = frozenset({"error"})


def extract_verification_evidence(lines: List[str]) -> Optional[Dict[str, Any]]:
    """Return the LAST publication-gate evidence reported in log lines.

    The gate (``preloop.agents.verification``) prints one
    ``PRELOOP_VERIFICATION {json}`` line per run. The marker is parsed by the
    runner, not written by it — anything the agent prints during its own
    execution is superseded because the gate always runs afterwards
    (post-execution), so the last marker is the authoritative one. Returns
    ``None`` when no well-formed marker exists (the normal case for flows
    without a verification policy).
    """
    for line in reversed(lines):
        stripped = str(line).strip()
        if not stripped.startswith(VERIFICATION_MARKER + " "):
            continue
        raw = stripped[len(VERIFICATION_MARKER) + 1 :]
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Ignoring malformed %s marker", VERIFICATION_MARKER)
            continue
        if not isinstance(payload, dict) or "allowed" not in payload:
            logger.warning(
                "Ignoring %s marker without an allowed flag",
                VERIFICATION_MARKER,
            )
            continue
        return payload
    return None


def separate_agent_verification_claim(
    artifact: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Rename an agent-authored ``verification`` key to ``verification_reported``.

    Report fields distinguish implemented status from verification status
    (issue #428): whatever the agent writes under ``verification`` in its
    result.json is a claim. The runner-captured gate evidence takes the
    ``verification`` key; the claim survives renamed, clearly not evidence.
    """
    if not isinstance(artifact, dict):
        return artifact
    if "verification" not in artifact:
        return artifact
    claim = artifact.pop("verification")
    artifact.setdefault("verification_reported", claim)
    return artifact


def _result_artifact_confirmation(artifact: Optional[Dict[str, Any]]) -> Optional[str]:
    """Classify a result.json artifact as an explicit completion verdict.

    Returns ``"success"`` or ``"failure"`` when the artifact carries an
    explicit ``status`` verdict, else ``None`` (no artifact, no status field,
    or an unrecognised value — none of which is a confirmation).

    ``status`` is authoritative when it carries a recognized value. When it
    is absent or unrecognized, the audit contracts' top-level ``verdict`` is
    consulted as a fallback so audit runs that honour their result schema are
    recognized as completed (channel 2) instead of being overridden to FAILED
    for a missing success confirmation.
    """
    if not isinstance(artifact, dict):
        return None
    status = artifact.get("status")
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in RESULT_ARTIFACT_SUCCESS_STATUSES:
            return "success"
        if normalized in RESULT_ARTIFACT_FAILURE_STATUSES:
            return "failure"
    verdict = artifact.get("verdict")
    if isinstance(verdict, str):
        normalized = verdict.strip().lower()
        if normalized in RESULT_ARTIFACT_VERDICT_SUCCESSES:
            return "success"
        if normalized in RESULT_ARTIFACT_VERDICT_FAILURES:
            return "failure"
    return None


# Line-start prefix an agent can print during the one-shot confirmation round
# to state plainly that the ORIGINAL task did not complete. Everything after
# the prefix is surfaced verbatim as the failure reason.
FLOW_FAILURE_REPORT_PREFIX = "FLOW_EXECUTION_FAILED:"

# Rough chars-per-token ratio used to convert the configured nudge token
# ceiling into a character budget for the prior-context excerpt.
CONFIRMATION_NUDGE_TOKEN_CHAR_RATIO = 4

# How many trailing lines of the previous run's output are quoted into the
# nudge prompt (before the character budget is applied).
CONFIRMATION_NUDGE_LOG_TAIL_LINES = 200


def _armed_log_lines(lines: List[str]) -> List[str]:
    """Return stripped log lines armed at the agent-exec-start marker.

    Mirrors the live-stream detector: lines before AGENT_EXEC_START_MARKER
    are prompt echo and are discarded. When the marker itself is missing from
    the refetched logs (e.g. runtime-side truncation), all lines are scanned:
    that is safe because every completion instruction embeds the sentinel
    INLINE, so a prompt echo can never occupy a full line by itself.
    """
    stripped = [str(line).strip() for line in lines]
    try:
        start = stripped.index(AGENT_EXEC_START_MARKER) + 1
    except ValueError:
        start = 0
    return stripped[start:]


def _sentinel_in_log_lines(lines: List[str]) -> bool:
    """Exact-line success-sentinel scan over complete (refetched) logs."""
    return FLOW_SUCCESS_SENTINEL in _armed_log_lines(lines)


def _failure_report_in_log_lines(lines: List[str]) -> Optional[str]:
    """Return the reason from an explicit FLOW_EXECUTION_FAILED: line, if any."""
    for line in _armed_log_lines(lines):
        if line.startswith(FLOW_FAILURE_REPORT_PREFIX):
            return line[len(FLOW_FAILURE_REPORT_PREFIX) :].strip() or "no reason given"
    return None


def _build_confirmation_nudge_prompt(
    original_prompt: str, prior_log_lines: List[str], max_tokens: int
) -> str:
    """Build the minimal follow-up prompt for the confirmation round.

    The prompt carries prior context (head of the original prompt + tail of
    the previous run's output) bounded by the configured token ceiling
    (~4 chars/token, split evenly between the two excerpts). Every embedded
    log line is quoted with "> " so that when the runtime echoes the prompt
    into its own logs, no embedded line can satisfy the exact-line sentinel
    detector or the line-start failure prefix.
    """
    budget_chars = max(2000, max_tokens * CONFIRMATION_NUDGE_TOKEN_CHAR_RATIO)
    prompt_excerpt = original_prompt[: budget_chars // 2]
    quoted_tail = "\n".join(
        "> " + str(line)
        for line in prior_log_lines[-CONFIRMATION_NUDGE_LOG_TAIL_LINES:]
    )[-(budget_chars // 2) :]
    return f"""This is a one-shot completion-confirmation round, NOT a new task.

Your previous invocation for the task below exited without confirming whether it completed. Do NOT redo, continue, or extend the task, and do not perform any new side effects (no pushes, comments, or API writes).

Decide from the context below whether the ORIGINAL task ran to completion.

If and only if it completed, confirm it through the originally-instructed channel: prefer writing /workspace/result.json with the completion status or verdict the original instructions require (for example {{"status": "success"}}); printing this marker on a line by itself (no other text on that line) is also accepted: {FLOW_SUCCESS_SENTINEL}

If it did not complete, state that plainly: write /workspace/result.json as {{"status": "failure", "reason": "<one short sentence>"}} or print a single line that starts with {FLOW_FAILURE_REPORT_PREFIX} followed by the reason.

--- ORIGINAL TASK PROMPT (truncated, context only) ---
{prompt_excerpt}
--- END ORIGINAL TASK PROMPT ---

--- OUTPUT TAIL OF PREVIOUS RUN (each line quoted with "> ") ---
{quoted_tail}
--- END OUTPUT TAIL ---"""


# Bounds for a per-flow timeout budget. The floor keeps a typo from making a
# flow unrunnable (a container needs longer than a few seconds just to pull an
# image and clone), and the ceiling keeps a runaway run from holding a worker
# slot and an agent Job for more than a day.
FLOW_TIMEOUT_SECONDS_MIN = 60
FLOW_TIMEOUT_SECONDS_MAX = 86400


@dataclass(frozen=True)
class TimeoutBudget:
    """The wall-clock budget one flow execution is allowed to spend.

    ``source`` is ``"flow"`` when the flow carries its own
    ``timeout_seconds`` and ``"default"`` when the global setting applies. It
    exists so the failure message can name the budget an operator has to
    change, which the flat "Execution timed out after 3600 seconds" could
    not: on staging every one of the 7 timeouts sat exactly on the global
    ceiling, which told nobody whether the run was stuck or simply long.
    """

    seconds: int
    source: str

    def timeout_message(self) -> str:
        """Operator-facing failure message naming the budget that expired."""
        if self.source == "flow":
            return (
                f"Execution timed out after {self.seconds} seconds "
                f"(this flow's timeout budget). Raise timeout_seconds on the "
                "flow if the work genuinely needs longer."
            )
        return (
            f"Execution timed out after {self.seconds} seconds (the default "
            "timeout budget). Set timeout_seconds on the flow to give it a "
            "budget of its own."
        )


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types to serializable ones."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    return obj


def _exception_message(exc: BaseException) -> str:
    """Return a useful message for exceptions whose str() is empty."""
    return str(exc) or exc.__class__.__name__


RUNNER_LOG_PAGE_SIZE = 500
RUNNER_LOG_SUMMARY_LINES = 1000


class FlowExecutionOrchestrator:
    """Manages the end-to-end lifecycle of a single Flow invocation."""

    def __init__(
        self,
        db: Session,
        flow_id: uuid.UUID,
        trigger_event_data: Dict[str, Any],
        nats_client: Client,
    ):
        self.db = db
        self.flow_id = flow_id
        self.trigger_event_data = trigger_event_data
        self.flow: Optional[Flow] = None
        self.ai_model: Optional[AIModel] = None
        # Effective agent type: flow.agent_type unless overridden by a matrix
        # cell (see MATRIX_OVERRIDES_KEY); resolved in _get_flow_details.
        self.agent_type: Optional[str] = None
        self.execution_log = None
        self.runtime_session: Optional[RuntimeSession] = None
        self.nats_client: Client = nats_client
        self.execution_logger = FlowExecutionLogger()
        self.temporary_api_key_id: Optional[uuid.UUID] = None
        self._log_streaming_task: Optional[asyncio.Task] = None
        self._runner_log_cursor: Optional[tuple[datetime, uuid.UUID]] = None
        self._runner_log_previous_line = ""
        self._runner_log_count = 0
        self._command_subscription: Optional[Any] = None
        self._stop_requested = asyncio.Event()
        self._success_sentinel_seen = asyncio.Event()
        # PR/MR the container wrapper opened in its post-execution step, read
        # from the PRELOOP_PR_OPENED log line and bound at terminal status.
        self._opened_pr: Optional[Dict[str, str]] = None
        # Native CLI agent session (opencode/codex) reported by the container
        # via the PRELOOP_AGENT_SESSION marker, persisted on the execution so
        # a correlated PR-comment resume can invoke the CLI resume flag.
        self._agent_session: Optional[Dict[str, str]] = None
        self._agent_exec_started = (
            False  # Set when AGENT_EXEC_START_MARKER seen in logs
        )
        # One-shot completion-confirmation round (layer 2): at most one nudge
        # per execution, across all attempts. Set the moment a nudge is
        # considered, so even an errored nudge is never repeated.
        self._confirmation_nudge_attempted = False
        # In-place completion nudge (the cheap layer): the agent script itself
        # re-invoked the harness in this container when it exited without
        # confirming. Observed from the marker lines it prints, so the
        # orchestrator never nudges a second time for the same session.
        self._inplace_nudge_seen = False
        self._inplace_nudge_logged = False
        self._inplace_nudge_unsupported = False
        # Execution context of the current attempt, kept so the confirmation
        # nudge can re-invoke the agent with prior context.
        self._execution_context: Optional[Dict[str, Any]] = None
        self._user_messages: asyncio.Queue = asyncio.Queue()
        # Evidence pack (tar.gz bytes) captured alongside the result artifact
        # before executor cleanup; persisted at finalize time. Kept out of
        # the agent_result dict so it never travels through NATS updates.
        self._evidence_archive: Optional[bytes] = None
        # tar.gz of /workspace captured before the runtime is torn down, so a
        # run that failed before pushing can be downloaded or resumed.
        self._workspace_snapshot: Optional[bytes] = None
        # Publication-gate evidence (issue #428) captured from the
        # PRELOOP_VERIFICATION marker the runner-controlled verifier prints
        # in its post-execution block. Kept on the orchestrator (not the
        # agent_result) so every terminal path reports it the same way, and
        # merged into the stored result with ``source: runner`` — the
        # agent's own verification claim in result.json is preserved under
        # verification_reported instead.
        self._verification_evidence: Optional[Dict[str, Any]] = None

        # Execution metrics tracked during execution
        self.total_tokens: int = 0
        self.tool_calls_count: int = 0
        # None means "not priced yet / could not be priced". Defaulting this to
        # 0.0 made unpriced executions display a confident $0.00 in the UI.
        self.estimated_cost: Optional[float] = None

        # Commit status tracking
        self._tracker_client = None
        self._commit_sha: Optional[str] = None
        self._status_context: str = "preloop"
        self._is_recovered: bool = False  # Set to True during execution recovery
        # Warning messages already surfaced on the execution timeline, so a
        # repeated condition (e.g. status posted at pending and again at
        # success) does not spam the same line.
        self._emitted_warnings: set[str] = set()
        # Set when a sync worker owns this orchestrator (claim lease heartbeat).
        self._orchestrator_worker_id: Optional[str] = None

    def _extract_commit_sha(self) -> Optional[str]:
        """Extract the commit SHA from the trigger event data.

        Looks for commit SHA in common locations for different event types.
        """
        payload = self.trigger_event_data.get("payload", {})

        # Ensure payload is a dict (could be a string in edge cases)
        if not isinstance(payload, dict):
            logger.debug(f"Payload is not a dict: {type(payload)}")
            return None

        # Try common locations for commit SHA
        # GitHub push event
        if "head_commit" in payload:
            sha = payload["head_commit"].get("id")
            if sha:
                logger.debug(f"Found commit SHA in head_commit.id: {sha[:8]}")
                return sha

        # GitHub/GitLab pull request / merge request events
        object_attrs = payload.get("object_attributes", {})
        if object_attrs:
            # GitLab MR
            if "last_commit" in object_attrs:
                sha = object_attrs["last_commit"].get("id")
                if sha:
                    logger.debug(
                        f"Found commit SHA in object_attributes.last_commit.id: {sha[:8]}"
                    )
                    return sha
            # GitLab may also have sha directly
            if "sha" in object_attrs:
                sha = object_attrs["sha"]
                if sha:
                    logger.debug(
                        f"Found commit SHA in object_attributes.sha: {sha[:8]}"
                    )
                    return sha

        # GitHub PR event - check for head sha
        if "pull_request" in payload:
            pr = payload["pull_request"]
            if "head" in pr:
                sha = pr["head"].get("sha")
                if sha:
                    logger.debug(
                        f"Found commit SHA in pull_request.head.sha: {sha[:8]}"
                    )
                    return sha

        # Direct commit reference
        if "commit" in payload:
            commit = payload["commit"]
            if isinstance(commit, dict):
                sha = commit.get("sha") or commit.get("id")
                if sha:
                    logger.debug(f"Found commit SHA in commit: {sha[:8]}")
                    return sha

        # Check for sha at top level
        if "sha" in payload:
            logger.debug(f"Found commit SHA at top level: {payload['sha'][:8]}")
            return payload["sha"]

        # Check in after (for push events)
        if "after" in payload:
            logger.debug(f"Found commit SHA in after: {payload['after'][:8]}")
            return payload["after"]

        logger.debug(f"No commit SHA found in payload. Keys: {list(payload.keys())}")
        return None

    def _extract_trigger_repository_identifier(self) -> Optional[str]:
        """Return the repository identifier carried by the trigger payload.

        GitHub sends ``repository.full_name`` and GitLab sends
        ``project.path_with_namespace``. Used to tell "the webhook named a repo
        we could not map to a project" (a real misconfiguration) apart from
        "this execution has no repository context at all" (manual runs).
        """
        payload = self.trigger_event_data.get("payload", {})
        if not isinstance(payload, dict):
            return None

        repo = payload.get("repository")
        if isinstance(repo, dict):
            identifier = repo.get("full_name") or repo.get("name")
            if identifier:
                return str(identifier)

        project = payload.get("project")
        if isinstance(project, dict):
            identifier = project.get("path_with_namespace") or project.get("name")
            if identifier:
                return str(identifier)

        return None

    async def _resolve_commit_status_project_id(self) -> Optional[str]:
        """Resolve the project the commit status must be posted to.

        The status has to land on the repository that actually triggered this
        execution. Posting to ``flow.trigger_project_ids[0]`` means every repo
        other than the first one a multi-repo flow watches gets a
        ``422 No commit found for SHA`` instead of a check (issue #175).
        """
        resolved = self._resolve_trigger_project_id(
            allow_first_project_fallback=False,
        )
        if resolved:
            return resolved

        repo_identifier = self._extract_trigger_repository_identifier()
        if repo_identifier:
            # The webhook told us which repo it came from and we could not map
            # it to a project. Falling back to the first trigger project would
            # post the status to the wrong repository, so refuse and say so.
            await self._emit_execution_warning(
                "Commit status skipped: could not match the triggering repository "
                f"'{repo_identifier}' to a Preloop project. Add it as a project on "
                "the tracker so checks can be posted to the right repository.",
                details={
                    "repository": repo_identifier,
                    "flow_trigger_project_ids": [
                        str(pid) for pid in (self.flow.trigger_project_ids or [])
                    ]
                    if self.flow
                    else [],
                },
            )
            return None

        trigger_project_ids = (
            [str(pid) for pid in self.flow.trigger_project_ids]
            if self.flow and self.flow.trigger_project_ids
            else []
        )
        if not trigger_project_ids:
            return None

        if len(trigger_project_ids) > 1:
            # No repository context and several candidates: any choice is a
            # guess, so make the guess visible instead of silently picking one.
            await self._emit_execution_warning(
                "Commit status target is ambiguous: the trigger event carries no "
                f"repository, and this flow watches {len(trigger_project_ids)} "
                "projects. Using the first one, which may be the wrong repository.",
                details={"flow_trigger_project_ids": trigger_project_ids},
            )

        return trigger_project_ids[0]

    async def _emit_execution_warning(
        self,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Surface a non-fatal problem on the execution timeline, not just in logs.

        Server-side ``logger.warning`` calls are invisible to the user whose
        flow silently did half its job. This publishes an ``execution_warning``
        entry, which is persisted with the execution logs and rendered in the
        console.

        Identical messages are emitted once per execution: commit status runs
        two or three times per execution (pending, then success or failure),
        and a misconfiguration would otherwise repeat verbatim each time.
        """
        if message in self._emitted_warnings:
            logger.debug(f"Suppressing repeated execution warning: {message}")
            return
        self._emitted_warnings.add(message)

        logger.warning(message)
        payload: Dict[str, Any] = {"message": message, "level": "warning"}
        if details:
            payload["details"] = _make_json_serializable(details)
        try:
            await self._publish_update("execution_warning", payload)
        except Exception as publish_error:
            logger.warning(
                f"Failed to publish execution warning: {publish_error}",
            )

    async def _get_tracker_client_for_status(self):
        """Get a tracker client for updating commit status.

        Returns None if we can't get a valid client (e.g., no project configured).
        """
        if self._tracker_client is not None:
            logger.debug("[CommitStatus] Using cached tracker client")
            return self._tracker_client

        try:
            if not self.flow:
                logger.warning("[CommitStatus] No flow object available")
                return None

            # Resolve the project from the repository that ACTUALLY triggered
            # this execution, not from flow.trigger_project_ids[0].
            trigger_project_id = await self._resolve_commit_status_project_id()

            if not trigger_project_id:
                # Expected for flows not tied to a specific project; the
                # misconfigured cases already emitted a warning above.
                logger.debug(
                    "[CommitStatus] No trigger project resolved - skipping status update"
                )
                return None

            from preloop.models.crud import crud_project
            from preloop.api.common import get_tracker_client

            project = crud_project.get(self.db, id=trigger_project_id)
            if not project:
                await self._emit_execution_warning(
                    "Commit status skipped: the triggering project "
                    f"{trigger_project_id} no longer exists.",
                )
                return None

            if not project.organization_id:
                await self._emit_execution_warning(
                    f"Commit status skipped: project {project.name} is not linked "
                    "to an organization, so the target repository is unknown.",
                )
                return None

            # Create a minimal user context for auth
            account = crud_account.get(self.db, id=self.flow.account_id)
            if not account:
                logger.warning(
                    f"[CommitStatus] Account not found: {self.flow.account_id}"
                )
                return None

            # Get the account owner or first admin
            users = crud_user.get_by_account(self.db, account_id=account.id, limit=1)
            if not users:
                logger.warning(
                    f"[CommitStatus] No users found for account: {account.id}"
                )
                return None

            logger.info(
                f"[CommitStatus] Getting tracker client for project {project.id}, "
                f"org {project.organization_id}, user {users[0].username}"
            )

            self._tracker_client = await get_tracker_client(
                organization_id=project.organization_id,
                project_id=project.id,
                db=self.db,
                current_user=users[0],
            )
            return self._tracker_client

        except Exception as e:
            logger.error(
                f"[CommitStatus] Exception getting tracker client: {e}",
                exc_info=True,
            )
            return None

    @staticmethod
    def _describe_status_target(tracker_client: Any) -> str:
        """Human-readable description of where a commit status is being posted."""
        details = getattr(tracker_client, "connection_details", None)
        if not isinstance(details, dict):
            return "the triggering repository"

        owner = details.get("owner")
        repo = details.get("repo")
        if owner and repo:
            return f"{owner}/{repo}"

        project_path = details.get("project_path") or details.get("project_id")
        if project_path:
            return str(project_path)

        return "the triggering repository"

    async def _update_commit_status(
        self,
        state: str,
        description: Optional[str] = None,
    ):
        """Update the commit status on the PR/MR.

        Args:
            state: Status state (pending, success, failure, error)
            description: Optional description text
        """
        # Only log at debug level initially - upgrade to info if we actually update
        logger.debug(
            f"[CommitStatus] Checking if status update needed (state='{state}')"
        )

        # Skip commit status updates during execution recovery
        # to avoid making external API calls for old/stale executions
        if self._is_recovered:
            logger.info("[CommitStatus] Skipping - execution is recovered")
            return

        if not self._commit_sha:
            self._commit_sha = self._extract_commit_sha()
            if self._commit_sha:
                logger.info(
                    f"[CommitStatus] Extracted commit SHA: {self._commit_sha[:8]}"
                )
            else:
                # Log more details about what's in trigger_event_data
                payload = self.trigger_event_data.get("payload", {})
                logger.info(
                    f"[CommitStatus] No commit SHA found. "
                    f"trigger_event_data keys: {list(self.trigger_event_data.keys())}, "
                    f"payload type: {type(payload).__name__}, "
                    f"payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}"
                )

        if not self._commit_sha:
            # This is expected for flows not triggered by commit/PR events
            logger.debug(
                "[CommitStatus] No commit SHA available - skipping status update"
            )
            return

        status_target = "the triggering repository"
        try:
            logger.info(
                f"[CommitStatus] Getting tracker client. "
                f"Flow ID: {self.flow_id}, "
                f"trigger_project_ids: {self.flow.trigger_project_ids if self.flow else None}"
            )

            tracker_client = await self._get_tracker_client_for_status()
            if not tracker_client:
                logger.warning(
                    f"[CommitStatus] Could not get tracker client. "
                    f"Flow trigger_project_ids: {self.flow.trigger_project_ids if self.flow else None}, "
                    f"account_id: {self.flow.account_id if self.flow else None}"
                )
                return

            status_target = self._describe_status_target(tracker_client)

            logger.info(
                f"[CommitStatus] Got tracker client: {type(tracker_client).__name__}, "
                f"connection_details: {list(tracker_client.connection_details.keys()) if hasattr(tracker_client, 'connection_details') else 'N/A'}"
            )

            # Check if the tracker supports commit status
            if not hasattr(tracker_client, "create_commit_status"):
                logger.info(
                    f"[CommitStatus] Tracker {type(tracker_client).__name__} doesn't support commit status"
                )
                return

            # Build the target URL for the execution
            target_url = None
            if self.execution_log:
                # Construct absolute URL to the execution details page
                # GitHub/GitLab require absolute URLs for commit status links
                base_url = getattr(settings, "preloop_url", None) or getattr(
                    settings, "PRELOOP_URL", None
                )
                if base_url:
                    # Remove trailing slash if present
                    base_url = base_url.rstrip("/")
                    target_url = (
                        f"{base_url}/console/flows/executions/{self.execution_log.id}"
                    )
                else:
                    # Fallback to relative path if no base URL configured
                    logger.warning(
                        "[CommitStatus] PRELOOP_URL not configured, using relative URL"
                    )
                    target_url = f"/console/flows/executions/{self.execution_log.id}"

            # Log the API call we're about to make
            logger.info(
                f"[CommitStatus] Calling create_commit_status: "
                f"sha={self._commit_sha[:8]}, state={state}, context={self._status_context}, "
                f"target_url={target_url[:50] if target_url else None}..."
            )

            await tracker_client.create_commit_status(
                sha=self._commit_sha,
                state=state,
                context=self._status_context,
                description=description,
                target_url=target_url,
            )

            logger.info(
                f"[CommitStatus] SUCCESS - Updated to '{state}' on {self._commit_sha[:8]}"
            )

        except Exception as e:
            # Don't fail the execution if status update fails, but do not hide
            # it either: a swallowed 422 used to leave the flow looking green
            # while GitHub showed no check at all (issue #175).
            logger.error(
                f"[CommitStatus] FAILED to update: {e}",
                exc_info=True,
            )
            sha_label = self._commit_sha[:8] if self._commit_sha else "unknown"
            await self._emit_execution_warning(
                f"Commit status '{state}' could not be posted to {status_target} "
                f"for commit {sha_label}: {_exception_message(e)}",
                details={
                    "state": state,
                    "commit_sha": self._commit_sha,
                    "target": status_target,
                },
            )

    @staticmethod
    async def send_command(
        execution_id: str,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        nats_client: Optional[Client] = None,
    ):
        """
        Send a command to a running flow execution via NATS.

        Args:
            execution_id: ID of the flow execution
            command: Command to send (e.g., 'stop', 'send_message')
            payload: Optional command payload
            nats_client: Optional NATS client (if not provided, will try to get from app state)

        Raises:
            RuntimeError: If NATS client is not available
        """
        # If nats_client not provided, try to get it from app state
        if nats_client is None:
            try:
                import inspect

                # Try to find the app instance in the call stack
                for frame_info in inspect.stack():
                    frame_locals = frame_info.frame.f_locals
                    if "request" in frame_locals:
                        request = frame_locals["request"]
                        if hasattr(request, "app") and hasattr(request.app, "state"):
                            nats_client = getattr(request.app.state, "nats", None)
                            break
            except Exception:
                # NATS may be unavailable when send_command is invoked outside a request.
                pass

        if nats_client is None:
            raise RuntimeError("NATS client not available or not connected")

        try:
            command_subject = f"flow-commands.{execution_id}"
            command_data = {"command": command, "payload": payload or {}}

            await nats_client.publish(
                command_subject, json.dumps(command_data).encode()
            )
            logger.info(
                f"Sent command '{command}' to execution {execution_id} via NATS"
            )
        except Exception as e:
            logger.error(f"Failed to send command via NATS: {e}", exc_info=True)
            raise

    # NATS max payload is typically 1MB; use 900KB to leave headroom
    NATS_MAX_PAYLOAD_BYTES = 900 * 1024

    async def _publish_update(self, message_type: str, payload: Dict[str, Any]):
        """
        Publishes a structured message to the NATS stream for real-time updates.
        Includes account_id for proper filtering to prevent cross-account data leaks.
        Automatically truncates large payloads to avoid NATS MaxPayloadError.
        """
        if not self.nats_client or not self.nats_client.is_connected:
            logger.warning("NATS client not available, skipping update publish.")
            return

        if not self.execution_log:
            logger.warning("Execution log not created yet, skipping update publish.")
            return

        try:
            message = {
                "execution_id": str(self.execution_log.id),
                "flow_id": str(self.flow_id),
                "account_id": str(self.flow.account_id)
                if self.flow and self.flow.account_id
                else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": message_type,
                "payload": payload,
            }
            subject = f"flow-updates.{self.execution_log.id}"
            encoded_message = json.dumps(message).encode()

            # Check if message exceeds NATS max payload
            if len(encoded_message) > self.NATS_MAX_PAYLOAD_BYTES:
                # Truncate the payload - specifically handle "line" field for log lines
                truncated_payload = dict(payload)
                if "line" in truncated_payload and isinstance(
                    truncated_payload["line"], str
                ):
                    # Calculate how much we need to truncate
                    excess = len(encoded_message) - self.NATS_MAX_PAYLOAD_BYTES
                    line = truncated_payload["line"]
                    # Truncate line with some extra margin
                    max_line_len = max(1000, len(line) - excess - 1000)
                    truncated_payload["line"] = (
                        line[:max_line_len]
                        + f"\n... [truncated {len(line) - max_line_len} chars]"
                    )
                    truncated_payload["truncated"] = True
                    message["payload"] = truncated_payload
                    encoded_message = json.dumps(message).encode()
                    logger.warning(
                        f"Truncated large log line ({len(line)} -> {max_line_len} chars) "
                        f"to fit NATS max payload"
                    )
                else:
                    # For other payload types, skip publishing this message
                    logger.warning(
                        f"Skipping {message_type} update: payload too large "
                        f"({len(encoded_message)} bytes > {self.NATS_MAX_PAYLOAD_BYTES})"
                    )
                    return

            await self.nats_client.publish(subject, encoded_message)
            logger.debug(f"Published {message_type} to NATS subject '{subject}'")
        except Exception as e:
            logger.error(f"Failed to publish update to NATS: {e}", exc_info=True)

    def _get_flow_details(self, *, refresh: bool = False):
        """Retrieve the Flow definition and associated AIModel."""
        logger.info(f"Retrieving flow details for flow_id: {self.flow_id}")

        # Get flow - convert UUID to string for comparison
        flow_id_str = (
            str(self.flow_id) if isinstance(self.flow_id, uuid.UUID) else self.flow_id
        )
        # Use CRUD layer without account filtering since this is an internal service
        # and we don't have the account_id yet (it's a property of the flow itself)
        self.flow = crud_flow.get(
            self.db, id=flow_id_str, **({"refresh": True} if refresh else {})
        )
        if not self.flow:
            raise ValueError(f"Flow with id {self.flow_id} not found")

        # Apply per-cell matrix overrides or a controller-written routing
        # record without mutating the shared flow row.
        matrix_overrides = (self.trigger_event_data or {}).get(
            MATRIX_OVERRIDES_KEY
        ) or {}
        self.agent_type, effective_ai_model_id = resolve_execution_agent_selection(
            self.trigger_event_data,
            flow_agent_type=self.flow.agent_type,
            flow_ai_model_id=self.flow.ai_model_id,
        )
        if matrix_overrides:
            logger.info(
                "Matrix overrides for execution (batch %s, cell %s): "
                "agent_type=%s, ai_model_id=%s",
                matrix_overrides.get("batch_id"),
                matrix_overrides.get("index"),
                matrix_overrides.get("agent_type"),
                matrix_overrides.get("ai_model_id"),
            )
        routing_record = (self.trigger_event_data or {}).get(ROUTING_RECORD_KEY) or {}
        if routing_record:
            logger.info(
                "Model routing for execution: source=%s rule_id=%s "
                "agent_type=%s ai_model_id=%s",
                routing_record.get("source"),
                routing_record.get("rule_id"),
                routing_record.get("agent_type"),
                routing_record.get("ai_model_id"),
            )

        logger.info(f"Found flow: {self.flow.name} (agent_type: {self.agent_type})")

        # Get AI model if specified
        if effective_ai_model_id:
            ai_model_id_str = (
                str(effective_ai_model_id)
                if isinstance(effective_ai_model_id, uuid.UUID)
                else effective_ai_model_id
            )
            self.ai_model = crud_ai_model.get(self.db, id=ai_model_id_str)
            if not self.ai_model:
                routing_record = (self.trigger_event_data or {}).get(
                    ROUTING_RECORD_KEY
                ) or {}
                if routing_record:
                    raise ValueError(
                        f"Recorded routing model {effective_ai_model_id} is "
                        "not available; refusing to start without a fallback."
                    )
                logger.warning(
                    f"AI model {effective_ai_model_id} not found for flow {self.flow_id}"
                )
            else:
                logger.info(
                    f"Loaded AI model: {self.ai_model.name} ({self.ai_model.model_identifier})"
                )
        else:
            logger.info("No AI model specified for this flow")

    def _resolve_execution_model_runtime(self):
        """Resolve model runtime for the selected flow model."""
        if not self.ai_model:
            return None

        return resolve_ai_model_runtime(self.ai_model, allow_gateway=True)

    async def _resolve_prompt(self) -> str:
        """
        Resolve dynamic placeholders in the prompt template using registered resolvers.

        Supports placeholders like:
        - {{trigger_event.payload.issue.title}}
        - {{project.name}}
        - {{account.email}}
        """
        logger.info("Resolving prompt template")

        # Ensure resolvers are registered
        self._ensure_resolvers_registered()

        prompt_template = self.flow.prompt_template
        resolved_prompt = prompt_template

        # Create resolver context
        resolver_context = ResolverContext(
            db=self.db,
            trigger_event_data=self.trigger_event_data,
            flow_id=str(self.flow_id),
            execution_id=str(self.execution_log.id) if self.execution_log else "",
        )

        # Extract all {{placeholder}} patterns
        placeholders = re.findall(r"\{\{(\w+(?:\.\w+)*)\}\}", prompt_template)

        for placeholder in placeholders:
            # Split prefix and path (e.g., "trigger_event.payload.title" -> "trigger_event" + "payload.title")
            parts = placeholder.split(".", 1)
            prefix = parts[0]
            path = parts[1] if len(parts) > 1 else ""

            # Get resolver for this prefix
            resolver = resolver_registry.get(prefix)

            if resolver:
                try:
                    # Resolve the placeholder
                    value = await resolver.resolve(path, resolver_context)

                    if value is not None:
                        # Replace the placeholder with the value
                        resolved_prompt = resolved_prompt.replace(
                            f"{{{{{placeholder}}}}}", str(value)
                        )
                        logger.debug(f"Resolved {{{{{placeholder}}}}}: {value}")
                    else:
                        logger.warning(
                            f"Placeholder {{{{{placeholder}}}}} resolved to None, leaving as-is"
                        )
                except Exception as e:
                    logger.error(
                        f"Error resolving placeholder {{{{{placeholder}}}}}: {e}",
                        exc_info=True,
                    )
            else:
                # Try simple replacement from trigger_event_data for backwards compatibility
                value = self._simple_resolve(placeholder, self.trigger_event_data)
                if value is not None:
                    resolved_prompt = resolved_prompt.replace(
                        f"{{{{{placeholder}}}}}", str(value)
                    )
                    logger.debug(f"Simple resolved {{{{{placeholder}}}}}: {value}")
                else:
                    logger.warning(
                        f"No resolver found for prefix '{prefix}' and simple resolution failed for {{{{{placeholder}}}}}"
                    )

        feedback_prompt = (self.trigger_event_data or {}).get("_feedback_prompt")
        if isinstance(feedback_prompt, str):
            resolved_prompt += "\n\n" + feedback_prompt

        # Resume runs always learn to inspect rebase-conflict.txt, because
        # the rebase happens after this prompt is resolved. Keep this before
        # the success-confirmation instruction, which must stay last.
        resolved_prompt = resolved_prompt + resume_rebase_conflict_hint(
            self.trigger_event_data
        )

        # Append the success-confirmation instruction so the agent can signal
        # completion. MUST stay at the very END of the prompt (recency): after
        # multi-million-token runs the model is far more likely to honor the
        # last instruction it saw (see execution ff1294e1 — work done, marker
        # forgotten). Nothing may be appended after this.
        resolved_prompt = resolved_prompt + _success_instruction_for_prompt(
            resolved_prompt
        )

        logger.info("Prompt resolution complete")
        return resolved_prompt

    def _ensure_resolvers_registered(self):
        """Ensure all built-in resolvers are registered."""
        # Register built-in resolvers if not already registered
        if not resolver_registry.get("trigger_event"):
            resolver_registry.register(TriggerEventResolver())
        if not resolver_registry.get("project"):
            resolver_registry.register(ProjectResolver())
        if not resolver_registry.get("account"):
            resolver_registry.register(AccountResolver())
        if not resolver_registry.get("execution"):
            resolver_registry.register(ExecutionResolver())

    def _sync_runtime_session(
        self,
        *,
        session_reference: Optional[str] = None,
        ended_at: Optional[datetime] = None,
    ) -> Optional[RuntimeSession]:
        """Create or update the shared runtime session for this flow execution."""
        if not self.flow or not self.execution_log or not self.flow.account_id:
            return None

        now = datetime.now(timezone.utc)
        execution_started_at = getattr(self.execution_log, "start_time", None) or now
        previous_runtime_session = crud_runtime_session.get_by_source(
            self.db,
            account_id=self.flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(self.execution_log.id),
        )
        self.runtime_session = crud_runtime_session.upsert_by_source(
            self.db,
            account_id=self.flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(self.execution_log.id),
            session_reference=session_reference,
            runtime_principal_type="flow_execution",
            runtime_principal_id=str(self.execution_log.id),
            runtime_principal_name=self.flow.name,
            started_at=execution_started_at,
            last_activity_at=ended_at or now,
            ended_at=ended_at,
        )
        self.db.commit()
        self.db.refresh(self.runtime_session)

        event_type = None
        if previous_runtime_session is None:
            event_type = "created"
        elif ended_at is not None and previous_runtime_session.ended_at != ended_at:
            event_type = "ended"
        elif (
            session_reference is not None
            and previous_runtime_session.session_reference != session_reference
        ):
            event_type = "updated"

        if event_type:
            try:
                from preloop.plugins.base import get_plugin_manager

                plugin_manager = get_plugin_manager()
                audit_service = plugin_manager.get_service("audit_service")
                if audit_service:
                    audit_service.log_runtime_session_event(
                        db=self.db,
                        account_id=self.flow.account_id,
                        runtime_session_id=self.runtime_session.id,
                        event=event_type,
                        session_source_type=self.runtime_session.session_source_type,
                        session_source_id=self.runtime_session.session_source_id,
                        session_reference=self.runtime_session.session_reference,
                        runtime_principal_type=self.runtime_session.runtime_principal_type,
                        runtime_principal_id=self.runtime_session.runtime_principal_id,
                        runtime_principal_name=self.runtime_session.runtime_principal_name,
                        flow_execution_id=self.execution_log.id,
                    )
            except Exception:
                logger.debug("Failed to audit runtime session lifecycle", exc_info=True)
            emit_account_event(
                build_account_event(
                    account_id=str(self.flow.account_id),
                    topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                    event_type=f"runtime_session_{event_type}",
                    payload={
                        "runtime_session_id": str(self.runtime_session.id),
                        "session_source_type": self.runtime_session.session_source_type,
                        "session_source_id": self.runtime_session.session_source_id,
                        "session_reference": self.runtime_session.session_reference,
                        "runtime_principal_type": self.runtime_session.runtime_principal_type,
                        "runtime_principal_id": self.runtime_session.runtime_principal_id,
                        "runtime_principal_name": self.runtime_session.runtime_principal_name,
                        "started_at": self.runtime_session.started_at.isoformat()
                        if self.runtime_session.started_at
                        else None,
                        "last_activity_at": self.runtime_session.last_activity_at.isoformat()
                        if self.runtime_session.last_activity_at
                        else None,
                        "ended_at": self.runtime_session.ended_at.isoformat()
                        if self.runtime_session.ended_at
                        else None,
                    },
                    runtime_session_id=str(self.runtime_session.id),
                    flow_id=str(self.flow.id),
                    execution_id=str(self.execution_log.id),
                )
            )
            emit_account_event(
                build_account_event(
                    account_id=str(self.flow.account_id),
                    topic=ACCOUNT_TOPIC_AUDIT,
                    event_type="audit_event",
                    payload={
                        "action": f"runtime_session_{event_type}",
                        "runtime_session_id": str(self.runtime_session.id),
                        "session_source_type": self.runtime_session.session_source_type,
                        "session_source_id": self.runtime_session.session_source_id,
                        "session_reference": self.runtime_session.session_reference,
                        "runtime_principal_type": self.runtime_session.runtime_principal_type,
                        "runtime_principal_id": self.runtime_session.runtime_principal_id,
                        "runtime_principal_name": self.runtime_session.runtime_principal_name,
                        "flow_execution_id": str(self.execution_log.id),
                        "flow_id": str(self.flow.id),
                    },
                    runtime_session_id=str(self.runtime_session.id),
                    flow_id=str(self.flow.id),
                    execution_id=str(self.execution_log.id),
                )
            )
        return self.runtime_session

    def _create_temporary_api_token(self) -> tuple[Optional[str], Optional[uuid.UUID]]:
        """
        Create a temporary API token for this flow execution.

        Returns:
            Tuple of (token_key, token_id) or (None, None) if creation failed
        """
        try:
            runtime_session = self._sync_runtime_session()
            return create_flow_runtime_token(
                self.db,
                flow=self.flow,
                execution_id=self.execution_log.id if self.execution_log else None,
                runtime_session_id=(
                    runtime_session.id if runtime_session is not None else None
                ),
            )
        except Exception as exc:
            logger.error(
                "Failed to create temporary API key record: %s",
                type(exc).__name__,
                exc_info=True,
            )
            self.db.rollback()
            return None, None

    def _execution_reached_terminal_state(self) -> bool:
        """Read this execution's committed status back from the database.

        The in-memory row can be stale (another worker may have finished the
        execution), so the status is re-read rather than trusted.
        """
        if self.execution_log is None:
            return False
        try:
            row = crud_flow_execution.get(self.db, id=self.execution_log.id)
            if row is not None:
                # Another worker may have finished this execution; without an
                # explicit refresh the identity map hands back this session's
                # own, possibly stale, copy of the row.
                self.db.refresh(row)
        except Exception as exc:
            logger.warning(
                "Could not read execution status for %s: %s",
                self.execution_log.id,
                type(exc).__name__,
            )
            return False
        if row is None:
            return False
        return str(row.status).upper() in TERMINAL_EXECUTION_STATUSES

    def _revoke_execution_runtime_tokens(self) -> int:
        """Revoke every runtime token minted for this execution."""
        account_id = getattr(self.flow, "account_id", None)
        execution_id = self.execution_log.id if self.execution_log else None
        revoked = revoke_flow_runtime_tokens(
            self.db,
            account_id=account_id,
            execution_id=execution_id,
        )
        if revoked == 0 and self.temporary_api_key_id:
            # Fall back to the key this orchestrator minted (account or
            # execution unknown, e.g. a flow row that never loaded).
            try:
                if crud_api_key.deactivate(self.db, key_id=self.temporary_api_key_id):
                    revoked = 1
            except Exception as exc:
                logger.error(
                    "Failed to cleanup temporary API key record: %s",
                    type(exc).__name__,
                    exc_info=True,
                )
                self.db.rollback()
        return revoked

    def _cleanup_temporary_api_token(self):
        """Retire this execution's runtime token once the execution is terminal.

        The agent outlives its orchestrator. A deploy drain cancels the
        in-flight handler, releases the claim and re-dispatches the execution
        so a peer worker resumes monitoring the *same* agent Job (see
        ``flow_execution_runner.claim_and_run_execution``). Revoking the token
        on the way out of an interrupted run leaves that still-running agent
        holding a credential the gateway rejects, and the run dies mid-stream
        with "Invalid authentication credentials".

        So the token is retired only when the execution itself has reached a
        terminal state; whichever worker finishes the run revokes it.
        """
        if not self.temporary_api_key_id and self.execution_log is None:
            return

        if not self._execution_reached_terminal_state():
            logger.info(
                "Leaving runtime token active for execution %s: not terminal "
                "(handed off to another worker)",
                self.execution_log.id if self.execution_log else "unknown",
            )
            return

        self._revoke_execution_runtime_tokens()

    def _simple_resolve(self, placeholder: str, data: Dict[str, Any]) -> Optional[str]:
        """
        Simple fallback resolver for backwards compatibility.

        Args:
            placeholder: Placeholder string (e.g., "payload.issue.title")
            data: Dictionary to resolve from

        Returns:
            Resolved value or None
        """
        keys = placeholder.split(".")
        value = data

        try:
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return None

            return str(value) if value is not None else None
        except Exception:
            return None

    async def _perform_git_clone(self, work_dir: str) -> Optional[str]:
        """
        Perform git clone operation if configured.

        Args:
            work_dir: Working directory where the clone should happen

        Returns:
            Path to cloned repository or None if not configured/failed
        """
        if not self.flow.git_clone_config:
            logger.debug("Git clone not configured for this flow")
            return None

        git_config = self.flow.git_clone_config
        if not git_config.get("enabled", False):
            logger.debug("Git clone is disabled")
            return None

        logger.info("Performing git clone operation")

        try:
            # Get repository URL
            repo_url = git_config.get("repository_url")
            if not repo_url:
                # Try to get from trigger event (GitHub/GitLab)
                repo_url = self._resolve_repository_url_from_trigger()

            if not repo_url:
                logger.error("No repository URL configured or found in trigger event")
                return None

            # Get clone path
            clone_path = git_config.get("clone_path", "./workspace")
            full_clone_path = f"{work_dir}/{clone_path}"

            # Get branch from config or trigger event. When a commit SHA is available,
            # clone the MR/PR target branch — the source ref may not exist yet.
            branch = git_config.get("branch")
            commit_sha = self._extract_commit_sha()
            if not branch:
                if commit_sha:
                    branch = self._extract_pr_target_branch_from_trigger() or "main"
                    logger.info(
                        "Commit SHA %s available; cloning branch '%s' "
                        "instead of source branch",
                        commit_sha[:8],
                        branch,
                    )
                else:
                    branch = self._extract_pr_branch_from_trigger()

            branch_arg = f" -b {shlex.quote(branch)}" if branch else ""

            # Resolve tracker credentials. The token is written to a
            # short-lived credential file rather than into the clone URL, so
            # the resulting remote carries no secret (issue #173).
            credential: Optional[GitCredential] = None
            use_tracker_creds = git_config.get("use_tracker_credentials", True)
            if use_tracker_creds:
                credentials = await self._get_tracker_credentials()
                if credentials:
                    credential = self._build_clone_credential(repo_url, credentials)

            repo_url = strip_url_credentials(repo_url)
            clone_cmd = (
                f"git clone --recursive{branch_arg} "
                f"{shlex.quote(repo_url)} {shlex.quote(full_clone_path)}"
            )

            logger.info(f"Executing git clone to {full_clone_path}")

            with temporary_credential_file(credential) as clone_env:
                # Execute git clone
                process = await asyncio.create_subprocess_shell(
                    clone_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                    env=clone_env,
                )

                stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(
                    "Git clone failed with code %s: %s",
                    process.returncode,
                    scrub_secrets(stderr.decode()),
                )
                return None

            logger.info(f"Git clone successful: {scrub_secrets(stdout.decode())}")

            # Checkout the specific commit SHA from trigger event if available
            # This ensures we're reviewing the exact code from the PR/push event
            if commit_sha:
                logger.info(
                    f"Checking out specific commit SHA from trigger event: {commit_sha[:8]}"
                )
                checkout_process = await asyncio.create_subprocess_shell(
                    f"git checkout {commit_sha}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=full_clone_path,
                )
                checkout_stdout, checkout_stderr = await checkout_process.communicate()

                if checkout_process.returncode != 0:
                    # If checkout fails, try fetching commit, source branch, then MR ref
                    logger.warning(
                        f"Direct checkout failed, trying fetch first: {checkout_stderr.decode()}"
                    )
                    source_branch = self._extract_pr_branch_from_trigger()
                    mr_ref = self._extract_merge_request_ref_from_trigger()
                    fetch_cmds = [f"git fetch origin {commit_sha}"]
                    if source_branch:
                        fetch_cmds.append(
                            f"git fetch origin {source_branch}:preloop-source-head"
                        )
                    if mr_ref:
                        fetch_cmds.append(f"git fetch origin {mr_ref}:preloop-mr-head")
                    for fetch_cmd in fetch_cmds:
                        fetch_process = await asyncio.create_subprocess_shell(
                            fetch_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=full_clone_path,
                        )
                        await fetch_process.communicate()

                    checkout_process = await asyncio.create_subprocess_shell(
                        f"git checkout {commit_sha}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=full_clone_path,
                    )
                    (
                        checkout_stdout,
                        checkout_stderr,
                    ) = await checkout_process.communicate()

                    if checkout_process.returncode != 0:
                        logger.error(
                            f"Failed to checkout commit {commit_sha[:8]}: {checkout_stderr.decode()}"
                        )
                    else:
                        logger.info(
                            f"Successfully checked out commit {commit_sha[:8]} after fetch"
                        )
                else:
                    logger.info(f"Successfully checked out commit {commit_sha[:8]}")
            else:
                logger.debug(
                    "No commit SHA in trigger event - using default branch HEAD"
                )

            return full_clone_path

        except Exception as e:
            logger.error(f"Error during git clone: {e}", exc_info=True)
            return None

    def _extract_merge_request_ref_from_trigger(self) -> Optional[str]:
        """Extract a git fetch ref for the MR/PR head commit."""
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            object_attrs = payload.get("object_attributes", {})
            if object_attrs and object_attrs.get("iid") is not None:
                return f"refs/merge-requests/{object_attrs['iid']}/head"

            if "pull_request" in payload:
                pr = payload["pull_request"]
                if pr.get("number") is not None:
                    return f"pull/{pr['number']}/head"

            return None
        except Exception as e:
            logger.debug(f"Error extracting merge request ref: {e}")
            return None

    def _extract_pr_target_branch_from_trigger(self) -> Optional[str]:
        """Extract the PR/MR target/base branch name from the trigger event."""
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            if "pull_request" in payload:
                pr = payload["pull_request"]
                if "base" in pr and "ref" in pr["base"]:
                    branch = pr["base"]["ref"]
                    logger.debug(f"Extracted PR base branch: {branch}")
                    return branch

            object_attrs = payload.get("object_attributes", {})
            if object_attrs and "target_branch" in object_attrs:
                branch = object_attrs["target_branch"]
                logger.debug(f"Extracted MR target branch: {branch}")
                return branch

            project = payload.get("project")
            if isinstance(project, dict) and project.get("default_branch"):
                return project["default_branch"]

            return None
        except Exception as e:
            logger.debug(f"Error extracting PR target branch: {e}")
            return None

    def _extract_pr_branch_from_trigger(self) -> Optional[str]:
        """Extract the PR/MR source branch name from the trigger event.

        For pull requests, we want to clone the head/source branch so we have
        all the commits from the PR available for checkout.
        """
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            # GitHub PR - get the head branch (source branch of PR)
            if "pull_request" in payload:
                pr = payload["pull_request"]
                if "head" in pr and "ref" in pr["head"]:
                    branch = pr["head"]["ref"]
                    logger.debug(f"Extracted PR head branch: {branch}")
                    return branch

            # GitLab MR - get the source branch
            object_attrs = payload.get("object_attributes", {})
            if object_attrs and "source_branch" in object_attrs:
                branch = object_attrs["source_branch"]
                logger.debug(f"Extracted MR source branch: {branch}")
                return branch

            return None
        except Exception as e:
            logger.debug(f"Error extracting PR branch: {e}")
            return None

    def _resolve_trigger_project_id(
        self, *, allow_first_project_fallback: bool = True
    ) -> Optional[str]:
        """Resolve the project that triggered this execution.

        Prefer the repository from the webhook payload (e.g. the MR's project)
        over the first entry in flow.trigger_project_ids, which may be a
        different repo when the flow watches multiple projects.

        Args:
            allow_first_project_fallback: When False, return None instead of
                falling back to ``flow.trigger_project_ids[0]``. Callers that
                address a specific repository (commit statuses) need this,
                because a wrong repo is worse than no repo.
        """
        project_id = self.trigger_event_data.get("project_id")
        if project_id:
            return str(project_id)

        from preloop.services.flow_trigger_service import FlowTriggerService

        resolved = FlowTriggerService(self.db)._extract_project_id(
            self.trigger_event_data
        )
        if resolved:
            logger.info(f"Resolved trigger project from event payload: {resolved}")
            return resolved

        if not allow_first_project_fallback:
            return None

        if self.flow.trigger_project_ids:
            fallback = str(self.flow.trigger_project_ids[0])
            logger.info(
                "No project in trigger event; using first flow trigger_project_id: "
                f"{fallback}"
            )
            return fallback

        return None

    def _resolve_repository_url_from_trigger(self) -> Optional[str]:
        """Extract repository URL from trigger event data."""
        try:
            # GitHub structure
            if "repository" in self.trigger_event_data:
                repo = self.trigger_event_data["repository"]
                if isinstance(repo, dict):
                    return repo.get("clone_url") or repo.get("html_url")

            # GitLab structure
            if "project" in self.trigger_event_data:
                project = self.trigger_event_data["project"]
                if isinstance(project, dict):
                    return project.get("http_url_to_repo") or project.get("web_url")

            return None
        except Exception as e:
            logger.error(f"Error extracting repository URL from trigger: {e}")
            return None

    async def _get_tracker_credentials(self) -> Optional[Dict[str, str]]:
        """Get tracker credentials from the database (deprecated - use _get_tracker_credentials_by_id)."""
        try:
            # Get tracker_id from trigger event or flow config
            tracker_id = self.trigger_event_data.get("tracker_id")
            if not tracker_id:
                logger.warning("No tracker_id in trigger event data")
                return None

            return await self._get_tracker_credentials_by_id(tracker_id)

        except Exception as e:
            logger.error(f"Error getting tracker credentials: {e}", exc_info=True)
            return None

    async def _get_tracker_credentials_by_id(
        self, tracker_id: str
    ) -> Optional[Dict[str, str]]:
        """Get tracker credentials by tracker ID."""
        try:
            from preloop.models.crud import crud_tracker

            tracker = crud_tracker.get(self.db, id=tracker_id)
            if not tracker:
                logger.warning(f"Tracker {tracker_id} not found")
                return None

            # Not `resolved_api_key`: a GitHub App tracker stores no key, its
            # credential is a short-lived installation token minted here. Using
            # the raw column left App-installed repositories with no git
            # credential at all, so the post-execution push failed with
            # "could not read Username for 'https://github.com'".
            token = await resolve_tracker_git_token(tracker)
            if not token:
                logger.warning(
                    "Tracker %s has no usable git token (auth_type=%s)",
                    tracker_id,
                    tracker.auth_type,
                )

            return {
                "tracker_id": str(tracker_id),
                "token": token or "",
                "tracker_type": tracker.tracker_type,
            }

        except Exception as e:
            logger.error(
                f"Error getting tracker credentials for {tracker_id}: {e}",
                exc_info=True,
            )
            return None

    def _resolve_project_tracker_id(self, project_id: Optional[str]) -> Optional[str]:
        """Return the tracker owning ``project_id``, or None."""

        if not project_id:
            return None
        try:
            from preloop.models.crud import crud_project

            project = crud_project.get(self.db, id=str(project_id))
            organization = project.organization if project else None
            tracker_id = getattr(organization, "tracker_id", None)
            return str(tracker_id) if tracker_id else None
        except Exception as e:
            logger.warning(
                "Could not resolve the tracker for project %s: %s", project_id, e
            )
            return None

    async def _attach_trigger_tracker_credentials(
        self, execution_context: Dict[str, Any]
    ) -> None:
        """Add the triggering project's tracker credentials to the context.

        The agent container resolves a repository's token from
        ``git_credentials_map[tracker_id]`` and otherwise falls back to a
        database lookup that reads the stored key only. That fallback returns
        nothing for a GitHub App tracker, which has no stored key, so a flow
        whose repository entry carries no ``tracker_id`` ran with no git
        credential: the clone of a public repository still worked and the
        post-execution push then failed to authenticate.

        Minting an App installation token is async and must happen here, in
        the orchestrator, not in the synchronous container code path.

        GitHub App installation tokens expire within an hour. They are minted
        at execution start and delivered in the container environment; the
        post-execution ``git push`` is the same container script, so a run
        longer than that window can still fail the push with an expired
        token. The recovery bundle is written before the push.
        """

        tracker_id = self._resolve_project_tracker_id(
            execution_context.get("trigger_project_id")
        ) or self.trigger_event_data.get("tracker_id")
        if not tracker_id:
            return

        tracker_id = str(tracker_id)
        credentials_map = execution_context.get("git_credentials_map") or {}
        if not (credentials_map.get(tracker_id) or {}).get("token"):
            creds = await self._get_tracker_credentials_by_id(tracker_id)
            if not creds or not creds.get("token"):
                logger.warning(
                    "No git token available for the triggering tracker %s; "
                    "the post-execution push will have no credentials",
                    tracker_id,
                )
                return
            credentials_map[tracker_id] = creds

        execution_context["git_credentials_map"] = credentials_map
        # Read by container.py when a repository entry declares no tracker.
        execution_context["trigger_tracker_id"] = tracker_id
        logger.info("Attached trigger tracker %s git credentials", tracker_id)

    def _build_clone_credential(
        self, repo_url: str, credentials: Dict[str, str]
    ) -> Optional[GitCredential]:
        """Build a credential for a repository without altering its URL.

        Replaces the previous ``_inject_credentials_into_url``. Embedding the
        token in the URL made it the repository's ``origin`` remote, so any
        later ``git remote -v`` leaked it into flow execution logs (issue
        #173). The token is now supplied through a short-lived credential file
        instead, leaving the remote clean.
        """
        try:
            token = credentials.get("token")
            tracker_type = credentials.get("tracker_type")

            if not token:
                return None

            host_kind = tracker_host_kind(repo_url)
            if host_kind is None and tracker_type not in {"github", "gitlab"}:
                logger.warning(
                    "Could not determine tracker type for %s; "
                    "using the generic credential username",
                    repo_url_log_location(repo_url),
                )

            return GitCredential(
                repo_url=strip_url_credentials(repo_url),
                username=credential_username(host_kind, tracker_type),
                token=token,
            )

        except Exception as e:
            logger.error(
                "Error building git credential: %s",
                type(e).__name__,
                exc_info=True,
            )
            return None

    async def _execute_custom_commands(self, work_dir: str) -> bool:
        """
        Execute custom commands if configured (admin-only feature).

        Args:
            work_dir: Working directory where commands should run

        Returns:
            True if successful or not configured, False if failed
        """
        if not self.flow.custom_commands:
            logger.debug("Custom commands not configured for this flow")
            return True

        custom_cmds = self.flow.custom_commands
        if not custom_cmds.get("enabled", False):
            logger.debug("Custom commands are disabled")
            return True

        # Security check: Verify the flow was created by a superuser
        # This prevents non-admin users from executing arbitrary commands
        try:
            from preloop.models.crud import crud_user

            # Get all users from the account
            users = crud_user.get_by_account(self.db, account_id=self.flow.account_id)

            # Check if ANY user with owner role exists in this account
            # (Flow creation/update should have been blocked if user wasn't admin)
            has_admin = any(user.is_superuser for user in users)
            if not has_admin:
                logger.error(
                    "Custom commands configured but no admin users found for account. "
                    "This is a security violation - skipping custom commands."
                )
                return False

        except Exception as e:
            logger.error(f"Error verifying admin status: {e}", exc_info=True)
            return False

        commands = custom_cmds.get("commands", [])
        if not commands:
            logger.debug("No custom commands to execute")
            return True

        logger.info(f"Executing {len(commands)} custom command(s)")

        try:
            for idx, cmd in enumerate(commands):
                logger.info(
                    f"Executing custom command {idx + 1}/{len(commands)}: {cmd}"
                )

                process = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                )

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    logger.error(
                        f"Custom command failed with code {process.returncode}: {stderr.decode()}"
                    )
                    return False

                logger.info(f"Custom command output: {stdout.decode()}")

            logger.info("All custom commands executed successfully")
            return True

        except Exception as e:
            logger.error(f"Error executing custom commands: {e}", exc_info=True)
            return False

    async def _prepare_execution_context(
        self, *, resolved_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """Prepare the full execution context for the agent."""
        effective_agent_type = self.agent_type or self.flow.agent_type
        logger.info(
            f"Preparing execution context for agent type: {effective_agent_type}"
        )

        if resolved_prompt is None:
            resolved_prompt = await self._resolve_prompt()

        # Validate trigger-payload workspace seeds before any agent starts: a
        # bad `workspace_files` declaration (path traversal, oversized inline
        # content) must fail the execution here with a clear message rather
        # than inside the container. Raises WorkspaceSeedError -> run() marks
        # the execution FAILED with that message.
        workspace_files = parse_workspace_files(
            self.trigger_event_data.get("payload")
            if isinstance(self.trigger_event_data, dict)
            else None
        )
        if workspace_files:
            logger.info(
                "Trigger payload seeds %d workspace file(s): %s",
                len(workspace_files),
                [seed.path for seed in workspace_files],
            )

        # Native profiles use only the operator's local Cursor login/config.
        # Do not mint model/MCP tokens or resolve cloud provider secrets here.
        from preloop.services.host_exec import (
            host_exec_profile_name,
            host_exec_flow_error,
            host_exec_unavailable_reason,
        )

        profile = host_exec_profile_name(self.flow.agent_config)
        if effective_agent_type == "cursor" or profile:
            error = host_exec_flow_error(
                agent_type=effective_agent_type,
                agent_config=self.flow.agent_config,
                runner_pool=self.flow.runner_pool,
            ) or host_exec_unavailable_reason(
                git_clone_config=self.flow.git_clone_config,
                custom_commands=self.flow.custom_commands,
            )
            if error:
                raise ValueError(error)
            if workspace_files or (self.trigger_event_data or {}).get("_resume"):
                raise ValueError(
                    "Host profiles do not support remote workspace seeds or native resume"
                )
            return {
                "flow_id": str(self.flow_id),
                "flow_name": self.flow.name,
                "execution_id": str(self.execution_log.id),
                "prompt": resolved_prompt,
                "agent_type": "cursor",
                "agent_config": {"host_exec_profile": profile},
                "account_id": self.flow.account_id,
                # A request for the local profile's explicit model map. This is
                # never a claim about the model Cursor actually reported.
                "model_identifier": self.ai_model.model_identifier
                if self.ai_model
                else None,
            }

        # Create short-lived API token for this flow execution
        account_api_token = None
        if self.flow.account_id:
            account_api_token, self.temporary_api_key_id = (
                self._create_temporary_api_token()
            )
            if not account_api_token:
                logger.warning(
                    "Could not create temporary API key record for account %s",
                    self.flow.account_id,
                )

        execution_context = {
            "flow_id": str(self.flow_id),
            "flow_name": self.flow.name,  # Used for generating git branch names
            "execution_id": str(self.execution_log.id),
            "prompt": resolved_prompt,
            "agent_type": effective_agent_type,
            "agent_config": self.flow.agent_config,
            "allowed_mcp_servers": self.flow.allowed_mcp_servers,
            "allowed_mcp_tools": self.flow.allowed_mcp_tools,
            "account_id": self.flow.account_id,
            "account_api_token": account_api_token,
            "git_clone_config": self.flow.git_clone_config,
            "custom_commands": self.flow.custom_commands,
            "trigger_event_data": self.trigger_event_data,
            "trigger_project_ids": [str(pid) for pid in self.flow.trigger_project_ids]
            if self.flow.trigger_project_ids
            else None,  # For git clone fallback
            # Singular form used by container.py for git clone and credential lookup
            "trigger_project_id": self._resolve_trigger_project_id(),
        }

        from preloop.services.flow_feedback import resolve_native_checkpoint
        from preloop.services.model_routing import validate_native_resume_identity

        resume = (self.trigger_event_data or {}).get("_resume") or {}
        native = (
            resolve_native_checkpoint(
                self.db,
                account_id=self.flow.account_id,
                flow_id=self.flow.id,
                execution_id=self.execution_log.id,
                resume=resume,
            )
            if resume.get("thread_id")
            else None
        )
        if resume.get("thread_id"):
            execution_context["checkpoint_resume_authorized"] = True
            execution_context["thread_id"] = resume["thread_id"]
        if native or resume.get("cli_session"):
            validate_native_resume_identity(
                self.db, self.flow, self.trigger_event_data, resume
            )
        if native:
            resume["cli_session"] = native
            if isinstance(native.get("artifact_reference"), dict):
                execution_context["native_session_reference"] = native[
                    "artifact_reference"
                ]

        # Correlated resume: hand the runner the prior execution's workspace
        # so unpushed commits (and scratch state) survive into this run.
        restore_archive = (
            None
            if settings.flow_artifact_direct_upload
            else self._resolve_workspace_restore_archive()
        )
        if restore_archive is not None:
            execution_context["workspace_restore_archive"] = restore_archive

        # Correlated resume: extract the prior execution's packed CLI session
        # from its workspace snapshot so the agent script can restore it into
        # runners that cannot seed the filesystem pre-start (Kubernetes).
        cli_session_archive = self._resolve_cli_session_restore_archive()
        if cli_session_archive is not None:
            execution_context["cli_session_restore_archive"] = cli_session_archive

        from preloop.services.isolated_publication import (
            isolated_publication_enabled,
            prepare_isolated_publication,
        )

        self._isolated_publication_policy = None
        self._publication_verification = None
        self._publication_runtime_stopped = False
        if isolated_publication_enabled(self.flow.git_clone_config):
            self._isolated_publication_policy = await prepare_isolated_publication(
                self.db, self.flow, execution_context
            )

        # Isolated mode never resolves the existing broad tracker token.
        if self.flow.git_clone_config and self._isolated_publication_policy is None:
            repositories = self.flow.git_clone_config.get("repositories", [])
            if repositories:
                logger.info(
                    f"Preparing git credentials for {len(repositories)} configured repositories"
                )
                # Get unique tracker IDs from repositories
                tracker_ids = set(
                    repo.get("tracker_id")
                    for repo in repositories
                    if repo.get("tracker_id")
                )

                # Fetch credentials for each tracker
                credentials_map = {}
                for tracker_id in tracker_ids:
                    creds = await self._get_tracker_credentials_by_id(tracker_id)
                    if creds:
                        credentials_map[tracker_id] = creds

                if credentials_map:
                    execution_context["git_credentials_map"] = credentials_map
                    logger.info(
                        f"Prepared git credentials for {len(credentials_map)} tracker(s)"
                    )
                else:
                    logger.warning(
                        "Git clone enabled but could not get tracker credentials"
                    )

            # Repositories declared without a tracker_id (and the
            # trigger-project fallback used when none are declared at all)
            # resolved their token inside the agent container, which reads the
            # stored key only and therefore finds nothing for a GitHub App
            # tracker. Resolve it here, where minting an installation token is
            # possible, and hand it over with the rest of the credentials.
            await self._attach_trigger_tracker_credentials(execution_context)

        # Add AI model details if available
        if self.ai_model:
            logger.info(
                f"AI model loaded: id={self.ai_model.id}, "
                f"identifier={self.ai_model.model_identifier}, "
                f"provider={self.ai_model.provider_name}"
            )
            resolved_model_runtime = self._resolve_execution_model_runtime()
            execution_context.update(
                resolved_model_runtime.to_execution_context(
                    gateway_token=account_api_token
                    if resolved_model_runtime.model_gateway_enabled
                    else None
                )
            )

            # Populate the authorized gateway model list so agent config
            # generators (e.g. OpenCode) can include every model the
            # principal is allowed to use, not just the primary one.  The
            # list must be scoped to the execution credential: this flow
            # principal is not authorized for subscription-OAuth models
            # bound to a managed agent, and offering them would only
            # produce a 400 at the gateway.
            if resolved_model_runtime.model_gateway_enabled:
                try:
                    from preloop.services.agent_model_list import (
                        list_authorized_gateway_models,
                    )
                    from preloop.services.model_gateway_auth import (
                        build_runtime_key_auth_context,
                    )

                    auth_context = (
                        build_runtime_key_auth_context(
                            self.db,
                            token=account_api_token,
                            api_key_id=str(self.temporary_api_key_id),
                        )
                        if account_api_token and self.temporary_api_key_id
                        else None
                    )
                    authorized = list_authorized_gateway_models(
                        self.db,
                        str(self.flow.account_id),
                        auth_context=auth_context,
                    )
                    execution_context["authorized_gateway_models"] = [
                        {"alias": m.alias, "display_name": m.display_name}
                        for m in authorized
                    ]
                except Exception:
                    logger.warning(
                        "Could not resolve authorized gateway models for flow "
                        "%s; the agent config falls back to the primary model "
                        "only",
                        self.flow.id,
                        exc_info=True,
                    )
        else:
            logger.warning(
                f"No AI model configured for flow {self.flow.id}, "
                f"ai_model_id={self.flow.ai_model_id if hasattr(self.flow, 'ai_model_id') else 'N/A'}, "
                "agent will need to use defaults"
            )

        logger.info("Execution context prepared successfully")
        return execution_context

    async def _stream_logs_to_nats(self, agent_executor, session_reference: str):
        """
        Background task to stream agent logs to NATS in real-time.

        Args:
            agent_executor: Agent executor instance
            session_reference: Container/Job reference
        """
        # Private runner WebSockets already persist and publish each line.
        # The main monitor consumes their stored markers with a keyset cursor.
        if getattr(agent_executor, "streams_logs_externally", False) is True:
            return
        logger.info(f"Starting log streaming for {session_reference}")
        log_count = 0

        # Track previous line for token parsing (tokens used pattern spans 2 lines)
        previous_line = ""

        try:
            async for log_line in agent_executor.stream_logs(session_reference):
                log_count += 1
                logger.debug(f"Streamed log line #{log_count}: {log_line[:100]}")

                await self._process_agent_log_line(
                    log_line,
                    session_reference=session_reference,
                    previous_line=previous_line,
                    log_count=log_count,
                )

                # Update previous line for next iteration
                previous_line = log_line

            logger.info(
                f"Log streaming completed. Total logs streamed: {log_count}, tokens: {self.total_tokens}, tool calls: {self.tool_calls_count}"
            )

        except asyncio.CancelledError:
            logger.info(f"Log streaming cancelled for {session_reference}")
        except Exception as e:
            logger.error(
                f"Error streaming logs for {session_reference}: {e}", exc_info=True
            )
            await self._publish_update(
                "agent_log_error", {"error": f"Log streaming error: {str(e)}"}
            )

    async def _process_agent_log_line(
        self,
        log_line: str,
        *,
        session_reference: str,
        previous_line: str,
        log_count: int,
        publish_raw: bool = True,
    ) -> None:
        """Interpret one runtime line independently of raw-log transport ownership."""
        # Store the log line for later summary
        self.execution_logger.log_agent_output(log_line)

        # Track the agent exec start marker — sentinel detection is
        # suppressed until this marker is seen, preventing false
        # positives from the prompt echo that contains the sentinel
        # instruction text.
        if not self._agent_exec_started and log_line.strip() == AGENT_EXEC_START_MARKER:
            self._agent_exec_started = True
            logger.info(f"Agent exec start marker seen at log line #{log_count}")

        # Detect success sentinel — but ONLY after the agent exec
        # start marker has been seen (to ignore prompt echo).
        stripped_line = log_line.strip()
        if stripped_line == FLOW_SUCCESS_SENTINEL:
            if not self._agent_exec_started:
                logger.warning(
                    f"[Sentinel] Ignoring sentinel match at line #{log_count} "
                    f"— agent exec start marker not yet seen (prompt echo?). "
                    f"Previous line: {previous_line[:120]!r}"
                )
            elif self._success_sentinel_seen.is_set():
                logger.warning(
                    f"[Sentinel] Duplicate sentinel match at line #{log_count} "
                    f"— already triggered. Previous line: {previous_line[:120]!r}"
                )
            else:
                logger.info(
                    f"[Sentinel] Success sentinel detected for {session_reference} "
                    f"at line #{log_count}. "
                    f"Previous line: {previous_line[:120]!r}"
                )
                self._success_sentinel_seen.set()

        # PR/MR opened by the wrapper's post-execution curl. The
        # response never reaches Python, so the line is the binding
        # channel (MCP create_pull_request binds directly instead).
        if PR_OPENED_MARKER in stripped_line:
            self._note_opened_pr(stripped_line)

        # Native CLI session id reported by the agent script, so a
        # later PR-comment resume can invoke the CLI resume flag.
        if any(
            marker in stripped_line
            for marker in (
                AGENT_SESSION_MARKER,
                "PRELOOP_NATIVE_SESSION_ARTIFACT ",
                "PRELOOP_NATIVE_RESUME ",
            )
        ):
            self._note_agent_session(stripped_line)

        # Publication-gate verdict printed by the post-execution
        # verifier. The gate always runs after the agent exits, so
        # the last marker wins over anything the agent printed.
        if stripped_line.startswith(VERIFICATION_MARKER + " "):
            self._note_verification_evidence(stripped_line)
        elif stripped_line.startswith(VERIFICATION_DENIED_MARKER):
            logger.warning("Publication gate denied publication")

        # In-place completion nudge markers printed by the agent
        # script. Order matters: the result marker shares the start
        # marker's prefix, so the exact match is tested first.
        if stripped_line == COMPLETION_NUDGE_MARKER:
            self._note_inplace_nudge(source="live_stream")
        elif stripped_line.startswith(COMPLETION_NUDGE_RESULT_MARKER):
            self._note_inplace_nudge_result(stripped_line)
        elif stripped_line.startswith(COMPLETION_NUDGE_UNSUPPORTED_MARKER):
            self._inplace_nudge_unsupported = True
            logger.info(
                "Agent runtime could not resume its session in place "
                "for the completion contract: %s",
                stripped_line,
            )

        previous_tool_calls_count = len(self.execution_logger.mcp_usage_logs)

        # Parse log line for structured data (includes tool call detection)
        self.execution_logger.parse_agent_logs([log_line])

        # Check for token usage pattern: "tokens used" followed by number on next line
        if "tokens used" in previous_line.lower():
            # Try to extract token count from current line
            # Pattern: number with optional commas (e.g., "1,234" or "1234")
            token_match = re.search(r"(\d{1,3}(?:,\d{3})*)", log_line.strip())
            if token_match:
                tokens = int(token_match.group(1).replace(",", ""))
                self.total_tokens += tokens

                logger.info(
                    "Detected token usage: %s tokens (total: %s). "
                    "Live cost remains unset until provider pricing is known.",
                    tokens,
                    self.total_tokens,
                )

                # Emit token usage update
                await self._publish_update(
                    "token_usage_update",
                    {
                        "total_tokens": self.total_tokens,
                        "pricing_available": False,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
                await self._persist_live_metrics()

        # Check if this log line indicates a tool call was detected
        updated_tool_calls_count = len(self.execution_logger.mcp_usage_logs)
        if updated_tool_calls_count > self.tool_calls_count:
            new_tool_entries = self.execution_logger.mcp_usage_logs[
                previous_tool_calls_count:updated_tool_calls_count
            ]
            self.tool_calls_count = updated_tool_calls_count
            logger.info(f"Tool call detected (total: {self.tool_calls_count})")

            for tool_entry in new_tool_entries:
                await self._publish_update(
                    "mcp_call",
                    {
                        **tool_entry,
                        "timestamp": tool_entry.get("timestamp")
                        or datetime.now(timezone.utc).isoformat(),
                    },
                )

            # Emit tool call count update
            await self._publish_update(
                "tool_calls_update",
                {
                    "tool_calls": self.tool_calls_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await self._persist_live_metrics()

        # Runner WebSockets already persisted and published this line.
        if publish_raw:
            await self._publish_update(
                "agent_log_line",
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "line": log_line,
                },
            )

    async def _consume_runner_log_page(self, session_reference: str) -> bool:
        """Consume a bounded stored-log page; True when caught up with the runner.

        The WebSocket owns persistence and raw log broadcast. This cursor only
        reconstructs controller markers, summaries and derived metrics. Terminal
        status is processed after all committed pages, because the runner flushes
        its final lines before reporting completion.
        """
        rows = crud_flow_execution_log.get_agent_log_page(
            self.db,
            self.execution_log.id,
            after=self._runner_log_cursor,
            limit=RUNNER_LOG_PAGE_SIZE,
        )
        # The parser may commit metric/binding updates. Materialize scalar
        # values first so an expired ORM page cannot cause a query per line.
        events = [(row.timestamp, row.id, row.message) for row in rows]
        for timestamp, event_id, message in events:
            if message is not None:
                self._runner_log_count += 1
                await self._process_agent_log_line(
                    message,
                    session_reference=session_reference,
                    previous_line=self._runner_log_previous_line,
                    log_count=self._runner_log_count,
                    publish_raw=False,
                )
                self._runner_log_previous_line = message
                # Raw history remains in the log table; retain only the bounded
                # tail used by summaries, completion checks and loop detection.
                del self.execution_logger.agent_output_lines[:-RUNNER_LOG_SUMMARY_LINES]
            self._runner_log_cursor = (timestamp, event_id)
        return len(events) < RUNNER_LOG_PAGE_SIZE

    async def _persist_live_metrics(self):
        """Persist live execution counters so reloads can rehydrate them."""
        if not self.execution_log:
            return

        self.execution_log.tool_calls_count = self.tool_calls_count
        self.execution_log.total_tokens = self.total_tokens
        self.execution_log.estimated_cost = self.estimated_cost
        self.execution_log.mcp_usage_logs = self.execution_logger.get_mcp_usage_logs()
        self.db.add(self.execution_log)
        self.db.commit()
        self.db.refresh(self.execution_log)

    def _get_runtime_tool_activity_count(self) -> int:
        """Return the persisted tool-call count for this execution."""
        if not self.execution_log:
            return self.tool_calls_count

        from preloop.models.crud import crud_runtime_session_activity

        return crud_runtime_session_activity.get_tool_call_count_by_flow_execution(
            self.db, flow_execution_id=self.execution_log.id
        )

    def _get_recent_runtime_tool_activity_signatures(
        self, limit: int = 12
    ) -> list[str]:
        """Return recent persisted tool-call signatures for loop detection."""
        if not self.execution_log:
            return []

        from preloop.models.crud import crud_runtime_session_activity

        activities = crud_runtime_session_activity.get_recent_successful_tool_calls_by_flow_execution(
            self.db, flow_execution_id=self.execution_log.id, limit=limit
        )

        signatures: list[str] = []
        timestamps: list[datetime] = []
        for activity in reversed(activities):
            metadata = activity.metadata_ or {}
            signatures.append(
                json.dumps(
                    {
                        "server_name": activity.server_name,
                        "tool_name": activity.tool_name,
                        "arguments": metadata.get("arguments"),
                    },
                    sort_keys=True,
                    default=str,
                )
            )
            timestamps.append(activity.timestamp)
        return self._dedupe_rapid_duplicate_signatures(signatures, timestamps)

    @staticmethod
    def _dedupe_rapid_duplicate_signatures(
        signatures: list[str],
        timestamps: list[datetime],
        *,
        max_delta_seconds: float = MCP_TOOL_LOOP_DUPLICATE_WINDOW_SECONDS,
    ) -> list[str]:
        """Drop paired duplicate signatures that arrive within a short window."""
        if not signatures:
            return []

        deduped_signatures: list[str] = [signatures[0]]
        last_kept_timestamp = timestamps[0] if timestamps else None
        for signature, timestamp in zip(signatures[1:], timestamps[1:], strict=False):
            if (
                signature == deduped_signatures[-1]
                and last_kept_timestamp is not None
                and timestamp is not None
                and abs((timestamp - last_kept_timestamp).total_seconds())
                <= max_delta_seconds
            ):
                continue
            deduped_signatures.append(signature)
            last_kept_timestamp = timestamp
        return deduped_signatures

    @staticmethod
    def _detect_repeated_tool_cycle(signatures: list[str]) -> Optional[Dict[str, Any]]:
        """Detect tight loops where the same tool+arguments repeat without progress.

        Legitimate flows (for example PR review) may call the same tool name several
        times with different arguments. Only identical consecutive signatures count
        toward a loop after rapid duplicate invocations are deduplicated.
        """
        if len(signatures) < MCP_TOOL_LOOP_MIN_REPETITIONS:
            return None

        for pattern_length in range(1, MCP_TOOL_LOOP_PATTERN_MAX_LENGTH + 1):
            repetitions = (
                MCP_TOOL_LOOP_SINGLE_CALL_REPETITIONS
                if pattern_length == 1
                else MCP_TOOL_LOOP_MIN_REPETITIONS
            )
            window_size = pattern_length * repetitions
            if len(signatures) < window_size:
                continue

            tail = signatures[-window_size:]
            pattern = tail[:pattern_length]
            if all(
                tail[index * pattern_length : (index + 1) * pattern_length] == pattern
                for index in range(repetitions)
            ):
                decoded_pattern = [json.loads(item) for item in pattern]
                return {
                    "pattern_length": pattern_length,
                    "repetitions": repetitions,
                    "pattern": decoded_pattern,
                }

        return None

    async def _sync_runtime_tool_activity_metrics(self) -> Optional[Dict[str, Any]]:
        """Sync persisted MCP activity into live metrics and detect tight loops."""
        persisted_tool_calls = self._get_runtime_tool_activity_count()
        if persisted_tool_calls > self.tool_calls_count:
            self.tool_calls_count = persisted_tool_calls
            logger.info(
                f"Persisted tool call count detected (total: {self.tool_calls_count})"
            )
            await self._publish_update(
                "tool_calls_update",
                {
                    "tool_calls": self.tool_calls_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await self._persist_live_metrics()

        recent_signatures = self._get_recent_runtime_tool_activity_signatures()
        return self._detect_repeated_tool_cycle(recent_signatures)

    async def _listen_for_commands(self):
        """
        Subscribe to NATS commands for user intervention.

        Listens on subject: flow-commands.{execution_id}
        """
        if not self.nats_client or not self.nats_client.is_connected:
            logger.warning("NATS not connected, cannot listen for commands")
            return

        command_subject = f"flow-commands.{self.execution_log.id}"

        try:

            async def command_handler(msg):
                try:
                    command_data = json.loads(msg.data.decode())
                    command_type = command_data.get("command")

                    logger.info(
                        f"Received command: {command_type} for execution {self.execution_log.id}"
                    )

                    if command_type == "stop":
                        logger.info("User requested stop")
                        self._stop_requested.set()
                    elif command_type == "send_message":
                        message = command_data.get("message", "")
                        logger.info(f"User sent message: {message}")
                        await self._user_messages.put(message)
                    elif command_type == "pause":
                        logger.info("User requested pause (not yet implemented)")
                        # TODO: Implement pause functionality
                    else:
                        logger.warning(f"Unknown command type: {command_type}")

                except Exception as e:
                    logger.error(f"Error handling command: {e}", exc_info=True)

            # Subscribe to commands
            self._command_subscription = await self.nats_client.subscribe(
                command_subject, cb=command_handler
            )
            logger.info(f"Listening for commands on {command_subject}")

        except Exception as e:
            logger.error(f"Failed to setup command subscription: {e}", exc_info=True)

    async def _cleanup_monitoring(self):
        """Cleanup monitoring resources (log streaming, command subscription)."""
        # Wait for log streaming task to complete naturally with a timeout
        # This ensures buffered logs are fully streamed before cleanup
        if self._log_streaming_task and not self._log_streaming_task.done():
            try:
                # Give the log streaming task time to finish naturally
                # (container may have buffered logs to yield)
                await asyncio.wait_for(self._log_streaming_task, timeout=30.0)
                logger.info("Log streaming task completed successfully")
            except asyncio.TimeoutError:
                logger.warning(
                    "Log streaming task did not complete within timeout, cancelling"
                )
                self._log_streaming_task.cancel()
                try:
                    await self._log_streaming_task
                except asyncio.CancelledError:
                    # Expected after cancelling the log streaming task on timeout.
                    pass
            except asyncio.CancelledError:
                # Task was cancelled while awaiting completion during cleanup.
                pass
            except Exception as e:
                logger.warning(f"Error waiting for log streaming task: {e}")

        # Unsubscribe from commands
        if self._command_subscription:
            try:
                await self._command_subscription.unsubscribe()
            except Exception as e:
                logger.error(f"Error unsubscribing from commands: {e}")

    async def _start_agent_session(
        self, execution_context: Dict[str, Any]
    ) -> tuple[str, Any]:
        """
        Launch an agent session via Agent Execution Infrastructure.

        Args:
            execution_context: Context for agent execution

        Returns:
            Tuple of (agent_session_reference, agent_executor)
            - agent_session_reference: Reference to the agent session (container ID, job ID, etc.)
            - agent_executor: The agent executor instance (caller must clean up)
        """
        agent_type = execution_context["agent_type"]
        agent_config = execution_context["agent_config"]

        logger.info(f"Starting {agent_type} agent session")

        agent_executor = None
        try:
            agent_executor = create_executor_for_execution(
                agent_type,
                agent_config,
                flow=self.flow,
                execution=self.execution_log,
                db=self.db,
                execution_context=execution_context,
            )

            # Admission and account activation share a row lock. If activation
            # wins, do not launch. If launch wins, activation persists stop intent.
            if not crud_flow_execution.admit_runtime_start(
                self.db,
                execution_id=self.execution_log.id,
            ):
                crud_flow_execution.cancel_unstarted_stop(
                    self.db, execution_id=self.execution_log.id
                )
                from preloop.services.kill_switch import FlowHaltActiveError

                raise FlowHaltActiveError("Account kill switch prevented agent launch")
            from preloop.agents.remote_runner import RemoteRunnerExecutor
            from preloop.services.checkpoint_runtime import checkpoint_context

            # Private state stays on its owning host. Resolve the actual executor
            # before minting any hosted artifact capabilities.
            execution_context["checkpoint_env"] = (
                {}
                if isinstance(agent_executor, RemoteRunnerExecutor)
                else checkpoint_context(self.db, execution_context)
            )

            # Start the agent
            session_reference = await agent_executor.start(execution_context)

            logger.info(f"Agent session started: {session_reference}")
            # Return both session reference and executor (caller is responsible for cleanup)
            return session_reference, agent_executor

        except Exception as e:
            logger.error(f"Failed to start {agent_type} agent: {e}", exc_info=True)
            # Cleanup agent executor on failure
            if agent_executor:
                try:
                    await agent_executor.cleanup()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during agent cleanup after failure: {cleanup_error}"
                    )
            raise

    def _resolve_workspace_restore_archive(self) -> Optional[bytes]:
        """Load the workspace snapshot of the execution this run resumes.

        Returns None when this is not a correlated resume, when the prior
        execution kept no snapshot (too large, or already reaped by the
        janitor), or when the lookup fails. In every one of those cases the
        runner falls back to the normal git clone.
        """
        resume = (self.trigger_event_data or {}).get("_resume")
        if not isinstance(resume, dict):
            return None
        prior_id = resume.get("execution_id")
        if not prior_id:
            return None
        try:
            prior = crud_flow_execution.get(db=self.db, id=prior_id)
        except Exception as e:
            logger.warning(
                f"Could not load prior execution {prior_id} for workspace restore: {e}"
            )
            return None
        if prior is None:
            return None
        if getattr(prior, "flow_id", None) != getattr(self.flow, "id", None):
            logger.warning(
                "Refusing workspace restore from execution %s: flow mismatch",
                prior_id,
            )
            return None
        snapshot = getattr(prior, "workspace_snapshot", None)
        if not snapshot:
            logger.info(
                "No workspace snapshot stored for prior execution %s; "
                "resume will re-clone",
                prior_id,
            )
            return None
        logger.info(
            "Restoring workspace snapshot from execution %s (%d bytes)",
            prior_id,
            len(snapshot),
        )
        return bytes(snapshot)

    def _needs_embedded_cli_session_archive(self) -> bool:
        """True when the runner cannot unpack the pack via volume restore.

        Hosted Docker ``put_archive``s the workspace snapshot before start, so
        ``.preloop-agent-session`` is already on disk. Kubernetes emptyDir
        cannot be seeded pre-start and needs the pack embedded in the script.
        """
        return detect_kubernetes_environment()

    def _resolve_cli_session_restore_archive(self) -> Optional[bytes]:
        """Extract the prior execution's packed CLI session from its snapshot.

        Returns a tar.gz of the ``.preloop-agent-session`` subtree (or None)
        for runs started from a PR comment on an execution that recorded a
        native CLI session. Built only for runners that cannot seed the
        filesystem pre-start (Kubernetes). Hosted Docker already unpacks the
        pack via workspace volume restore, so scanning the snapshot there is
        skipped.
        """
        if not self._needs_embedded_cli_session_archive():
            return None
        resume = (self.trigger_event_data or {}).get("_resume")
        if not isinstance(resume, dict):
            return None
        if not isinstance(resume.get("cli_session"), dict):
            return None
        prior_id = resume.get("execution_id")
        if not prior_id:
            return None
        try:
            prior = crud_flow_execution.get(db=self.db, id=prior_id)
        except Exception as e:
            logger.warning(
                f"Could not load prior execution {prior_id} for CLI session "
                f"restore: {e}"
            )
            return None
        if prior is None:
            return None
        if getattr(prior, "flow_id", None) != getattr(self.flow, "id", None):
            logger.warning(
                "Refusing CLI session restore from execution %s: flow mismatch",
                prior_id,
            )
            return None
        snapshot = getattr(prior, "workspace_snapshot", None)
        if not snapshot:
            logger.info(
                "No workspace snapshot stored for prior execution %s; "
                "CLI session cannot be restored",
                prior_id,
            )
            return None
        archive = extract_session_pack(bytes(snapshot))
        if archive is None:
            logger.info(
                "Prior execution %s carries no usable CLI session pack",
                prior_id,
            )
            return None
        logger.info(
            "Extracted CLI session pack from execution %s (%d bytes)",
            prior_id,
            len(archive),
        )
        return archive

    async def _capture_result_artifact(
        self, agent_executor: Any, session_reference: str
    ) -> Optional[Dict[str, Any]]:
        """Capture the agent's structured result artifact, if any.

        Eval/observe flows write ``/workspace/result.json`` as their final
        report; executors that support first-class capture expose
        ``get_result_artifact``. Best-effort: a missing artifact or an
        executor without support simply yields None.

        Also captures the evidence pack (``/workspace/evidence`` as tar.gz
        bytes) as a side effect, stashed on ``self._evidence_archive`` for
        persistence at finalize time. It must happen here because both
        artifacts are only readable before the executor is cleaned up, and
        this method is called on every terminal path.
        """
        await self._capture_evidence_archive(agent_executor, session_reference)
        await self._capture_workspace_snapshot(agent_executor, session_reference)
        getter = getattr(agent_executor, "get_result_artifact", None)
        if not callable(getter):
            return None
        try:
            artifact = await getter(session_reference)
        except Exception as e:
            logger.warning(f"Failed to capture result artifact: {e}")
            return None
        # Only plain JSON objects are persistable (also guards against mock
        # executors in tests returning non-dict values).
        if not isinstance(artifact, dict):
            return None
        # Reserved publisher state is control-plane owned, never agent JSON.
        artifact = dict(artifact)
        artifact.pop("trusted_publication", None)
        self.execution_logger.log_milestone(
            "result_artifact_captured",
            {"keys": sorted(artifact.keys())[:20]},
        )
        return artifact

    async def _capture_evidence_archive(
        self, agent_executor: Any, session_reference: str
    ) -> None:
        """Capture the evidence pack archive, if the executor supports it.

        Best-effort and captured at most once per execution: retries on the
        same terminal path must not overwrite an already captured archive
        with None after the container is gone.
        """
        if self._evidence_archive is not None:
            return
        getter = getattr(agent_executor, "get_evidence_archive", None)
        if not callable(getter):
            return
        try:
            archive = await getter(session_reference)
        except Exception as e:
            logger.warning(f"Failed to capture evidence archive: {e}")
            return
        if not isinstance(archive, (bytes, bytearray)) or not archive:
            return
        self._evidence_archive = bytes(archive)
        self.execution_logger.log_milestone(
            "evidence_archive_captured",
            {"size_bytes": len(self._evidence_archive)},
        )

    async def _capture_workspace_snapshot(
        self, agent_executor: Any, session_reference: str
    ) -> None:
        """Capture the workspace snapshot, if the executor supports it.

        Same contract as the evidence pack: best effort, at most once per
        execution, and only readable before the executor is cleaned up.
        """
        if self._workspace_snapshot is not None:
            return
        getter = getattr(agent_executor, "get_workspace_snapshot", None)
        if not callable(getter):
            return
        try:
            snapshot = await getter(session_reference)
        except Exception as e:
            logger.warning(f"Failed to capture workspace snapshot: {e}")
            return
        if not isinstance(snapshot, (bytes, bytearray)) or not snapshot:
            return
        self._workspace_snapshot = bytes(snapshot)
        self.execution_logger.log_milestone(
            "workspace_snapshot_captured",
            {"size_bytes": len(self._workspace_snapshot)},
        )

    async def _refetch_exited_session_logs(
        self, agent_executor: Any, session_reference: str
    ) -> List[str]:
        """Refetch the COMPLETE runtime logs after agent exit (layer 3).

        The live stream is best-effort: a late reconnect can silently lose
        the tail, taking the success sentinel with it. Batch log reads
        (docker logs / K8s pod logs) return the full buffer, so a post-exit
        refetch is the authoritative view. Best-effort: any error yields an
        empty list and the decision ladder simply moves on.
        """
        getter = getattr(agent_executor, "get_logs", None)
        if not callable(getter):
            return []
        try:
            logs = await getter(session_reference, tail=None)
        except Exception as e:
            logger.warning(f"Post-exit log refetch failed: {_exception_message(e)}")
            return []
        if not isinstance(logs, list):
            return []
        return [line for line in logs if isinstance(line, str)]

    def _note_opened_pr(self, line: str) -> None:
        """Remember the PR/MR the wrapper opened (first one wins)."""
        if getattr(self, "_isolated_publication_policy", None) is not None:
            return  # Only the trusted publisher can bind an isolated execution.
        parsed = parse_pr_opened_marker(line)
        if not parsed:
            return
        if self._opened_pr is not None:
            return
        self._opened_pr = parsed
        logger.info("Wrapper opened a pull request for this execution")
        self.execution_logger.log_milestone(
            "pull_request_opened",
            {
                "pr_url": parsed.get("url"),
                "branch": parsed.get("branch"),
                "provider": parsed.get("provider"),
            },
        )

    def _note_agent_session(self, line: str) -> None:
        """Remember the CLI session the agent reported (first per attempt).

        Persisted the moment it is seen so a later crash still leaves the
        session id on the row; a retried attempt resets ``_agent_session``
        and overwrites the record with the new session.
        """
        from preloop.agents.session_runtime import parse_native_artifact_marker

        if "PRELOOP_NATIVE_RESUME " in line and self.execution_log is not None:
            try:
                outcome = json.loads(line.split("PRELOOP_NATIVE_RESUME ", 1)[1])
                if isinstance(outcome, dict) and outcome.get("mode") in {
                    "native_resume",
                    "cold_handoff",
                    "resume_failed",
                }:
                    crud_flow_execution.record_native_resume(
                        self.db, execution_id=self.execution_log.id, outcome=outcome
                    )
                    self.execution_logger.log_milestone("native_resume", outcome)
            except ValueError:
                # Malformed PRELOOP_NATIVE_RESUME JSON is not a session record.
                pass
            return
        artifact = parse_native_artifact_marker(line)
        if artifact and self.execution_log is not None:
            expected_thread = (self.trigger_event_data or {}).get("_session_thread_id")
            if artifact.get("thread_id") == expected_thread:
                self._agent_session = artifact
                record_cli_session(self.db, self.execution_log.id, artifact)
            return
        parsed = parse_agent_session_marker(line)
        if not parsed:
            return
        if self._agent_session is not None:
            return
        self._agent_session = parsed
        logger.info(
            "Agent reported CLI session %s (%s)",
            parsed.get("session_id"),
            parsed.get("agent_type"),
        )
        self.execution_logger.log_milestone("cli_session_captured", dict(parsed))
        if self.execution_log is not None:
            record_cli_session(self.db, self.execution_log.id, parsed)

    def _note_verification_evidence(self, line: str) -> None:
        """Remember the last publication-gate evidence seen in the stream.

        Only the runner-controlled verifier prints this marker after the
        agent has exited, so it supersedes both earlier gate runs (e.g. from
        a previous attempt) and any look-alike line an agent printed.
        """
        parsed = extract_verification_evidence([line])
        if parsed is None:
            return
        self._verification_evidence = parsed
        logger.info(
            "Publication gate evidence captured: allowed=%s status=%s profile=%s@%s",
            parsed.get("allowed"),
            parsed.get("status"),
            parsed.get("profile_id"),
            parsed.get("profile_version"),
        )

    def _resolve_verification_evidence(self) -> Optional[Dict[str, Any]]:
        """Gate evidence from the live stream, or from the stored log tail.

        A lost stream reconnect can drop the marker; the execution logger
        keeps every streamed line, so the accumulated output is scanned as a
        fallback (same recovery pattern as the completion sentinel).
        """
        if self._verification_evidence is not None:
            return self._verification_evidence
        lines = self.execution_logger.get_agent_output_lines()
        if not lines:
            return None
        parsed = extract_verification_evidence(lines)
        if parsed is not None:
            self._verification_evidence = parsed
        return parsed

    def _bind_cli_session(self, output_summary: Optional[str]) -> None:
        """Persist the agent CLI session on the terminal path.

        Rescans ``output_summary`` when the live stream missed the marker
        (reconnects can drop the tail). ``_note_agent_session`` does the
        actual persistence, so this only rescues a missed line.
        """
        if output_summary:
            for line in output_summary.splitlines():
                if any(
                    marker in line
                    for marker in (
                        AGENT_SESSION_MARKER,
                        "PRELOOP_NATIVE_SESSION_ARTIFACT ",
                        "PRELOOP_NATIVE_RESUME ",
                    )
                ):
                    self._note_agent_session(line)

    async def _replay_persisted_runner_logs(self) -> None:
        """Drain remaining runner pages before terminal PR/session/metrics binding.

        Live monitoring normally consumed these rows already. Reuse its cursor
        so terminal recovery cannot duplicate raw logs or derived metrics, and
        yield between bounded pages when a completed run has a large backlog.
        """
        if self.execution_log is None:
            return
        ref = getattr(self.execution_log, "agent_session_reference", "") or ""
        if not isinstance(ref, str) or not ref.startswith("runner:"):
            return
        while not await self._consume_runner_log_page(ref):
            await asyncio.sleep(0)

    def _bind_opened_pr(self, output_summary: Optional[str]) -> None:
        """Persist the wrapper-opened PR on this execution's result.

        Runs on the terminal path so a later comment on that PR can resume
        this flow. ``output_summary`` is rescanned when the live stream
        missed the marker line (reconnects can drop the tail).
        """
        if self._opened_pr is None and output_summary:
            for line in output_summary.splitlines():
                if PR_OPENED_MARKER in line:
                    self._note_opened_pr(line)
                    break
        if self._opened_pr is None or self.execution_log is None:
            return
        record_opened_pr(
            self.db,
            self.execution_log.id,
            self._opened_pr.get("url", ""),
            source_branch=self._opened_pr.get("branch"),
        )

    async def _start_queued_followup(self) -> None:
        """Start the single follow-up resume queued while this run was going.

        Comments that arrive during a run set ``result.pending_followup``
        instead of starting a competing execution; many comments collapse into
        one flag, so exactly one follow-up run is started here.
        """
        if self.execution_log is None or self.flow is None:
            return
        try:
            followup = take_pending_followup(self.db, self.execution_log)
            if not followup:
                return
            event_data = dict(self.execution_log.trigger_event_details or {})
            resume = dict(event_data.get("_resume") or {})
            resume["execution_id"] = str(self.execution_log.id)
            # The session to resume is the one this execution just ran; a
            # fresh capture replaces whatever older id the metadata carried.
            # The in-memory capture wins: it is authoritative for this run
            # even when the row write was skipped or the object is stale.
            cli_session = self._agent_session or resume_cli_session_of(
                self.execution_log
            )
            if isinstance(cli_session, dict) and cli_session.get("session_id"):
                resume["cli_session"] = dict(cli_session)
            if followup.get("pr_url"):
                resume["pr_url"] = followup["pr_url"]
            if followup.get("source_branch"):
                resume["source_branch"] = followup["source_branch"]
            if followup.get("comment_url"):
                resume["comment_url"] = followup["comment_url"]
            resume["followup_of_execution_id"] = str(self.execution_log.id)
            event_data["_resume"] = resume
            event_data.pop("test_mode", None)

            pr_url = resume.get("pr_url")
            opener = self.execution_log
            if pr_url:
                found = find_bound_execution(self.db, self.flow.id, pr_url)
                if found is not None:
                    opener = found
            cap = max_resumes_per_pr(self.flow)
            started = resume_count(opener)
            if started >= cap:
                logger.info(
                    "Skipping queued follow-up for execution %s: already "
                    "started %s/%s resumes for this PR",
                    self.execution_log.id,
                    started,
                    cap,
                )
                return
            resume["resume_index"] = note_resume_started(self.db, opener)
            event_data["_resume"] = resume

            from preloop.services.flow_trigger_service import FlowTriggerService

            trigger_service = FlowTriggerService(self.db)
            await trigger_service._start_flow_execution(
                flow=self.flow,
                event_data=event_data,
                nats_client=self.nats_client,
                source_execution=self.execution_log,
            )
            logger.info(
                "Started queued follow-up resume after execution %s (index %s)",
                self.execution_log.id,
                resume.get("resume_index"),
            )
        except Exception:
            logger.warning(
                "Failed to start the queued follow-up resume for execution %s",
                getattr(self.execution_log, "id", None),
                exc_info=True,
            )

    def _note_inplace_nudge(self, *, source: str) -> None:
        """Record that the agent script ran its own completion reminder.

        Emitted once per execution as the ``completion_nudge`` timeline
        event, whether the marker was seen live or recovered from the
        post-exit log refetch. The orchestrator does not nudge again after
        this: the agent has already been asked, in the container where the
        work happened, and asking twice is two model calls for one answer.

        Args:
            source: Where the marker was observed (``live_stream`` or
                ``post_exit_rescan``).
        """
        self._inplace_nudge_seen = True
        if self._inplace_nudge_logged:
            return
        self._inplace_nudge_logged = True
        self.execution_logger.log_milestone(
            "completion_nudge",
            {
                "mode": "in_place",
                "source": source,
                "agent_type": self.agent_type,
            },
        )
        logger.info("In-place completion nudge observed (source=%s)", source)

    def _note_inplace_nudge_result(self, line: str) -> None:
        """Record the exit code of the in-place reminder round."""
        self._inplace_nudge_seen = True
        exit_code: Optional[int] = None
        _, _, tail = line.partition("exit=")
        try:
            exit_code = int(tail.strip().split()[0])
        except (ValueError, IndexError):
            exit_code = None
        self.execution_logger.log_milestone(
            "completion_nudge_result",
            {"mode": "in_place", "exit_code": exit_code},
        )

    def _scan_inplace_nudge_markers(self, lines: List[str]) -> None:
        """Recover the in-place nudge markers from refetched logs.

        The live stream is best-effort; the same reconnect that loses a
        sentinel can lose these. The complete post-exit logs are the
        authoritative view, so they are rescanned before the orchestrator
        decides whether to start a nudge session of its own.
        """
        for line in _armed_log_lines(lines):
            if line == COMPLETION_NUDGE_MARKER:
                self._note_inplace_nudge(source="post_exit_rescan")
            elif line.startswith(COMPLETION_NUDGE_RESULT_MARKER):
                if not self._inplace_nudge_logged:
                    self._note_inplace_nudge(source="post_exit_rescan")
                self._inplace_nudge_seen = True
            elif line.startswith(COMPLETION_NUDGE_UNSUPPORTED_MARKER):
                self._inplace_nudge_unsupported = True

    def _accept_wider_completion_signal(
        self, agent_executor: Any, result_artifact: Optional[Dict[str, Any]]
    ) -> bool:
        """Whether a bare result.json counts as completion for this runtime.

        The fallback for runtimes that cannot be resumed in place (gemini,
        aider, openhands, remote runners): there is nobody left to ask, so a
        report the agent actually wrote is the best evidence available. A
        non-empty JSON object at ``/workspace/result.json`` is accepted as
        completion even when its status vocabulary is not one Preloop
        recognizes, except for known failure and incomplete statuses
        (``timeout``, ``cancelled``, ``in_progress``, ...): those say the
        work did not finish and must not be recorded as success. An explicit
        failure status is usually decided earlier; the check here is the
        last line of defence on this path.

        Runtimes that CAN resume are deliberately excluded: for them the
        agent was asked directly and declined to confirm, which is a much
        stronger signal than the presence of a file.

        The capability check is a strict identity test against ``False``, the
        mirror of the ``supports_confirmation_nudge is True`` check: widening
        what counts as success is a safety decision, so it is taken only for
        a runtime that positively declares it cannot be resumed, never for an
        executor whose capability is merely unknown (a mock, a partially
        implemented runtime), which keeps failing closed.
        """
        if (
            getattr(agent_executor, "supports_inplace_completion_nudge", None)
            is not False
        ):
            return False
        if self._inplace_nudge_seen:
            return False
        if not isinstance(result_artifact, dict) or not result_artifact:
            return False
        status = result_artifact.get("status")
        if isinstance(status, str):
            normalized = status.strip().lower()
            if (
                normalized in RESULT_ARTIFACT_FAILURE_STATUSES
                or normalized in RESULT_ARTIFACT_INCOMPLETE_STATUSES
            ):
                return False
        return True

    async def _resolve_missing_confirmation(
        self,
        agent_executor: Any,
        session_reference: str,
        result: Any,
        result_artifact: Optional[Dict[str, Any]],
    ) -> tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        """Decision ladder for an exit-0 run with no success confirmation.

        Order (cheapest first):
          1. Layer 3 — refetch the complete runtime logs and rescan for the
             exact-line sentinel (covers the lost-stream-tail edge), and for
             the in-place completion-nudge markers.
          2. Layer 2 — one confirmation-round nudge, skipped when the agent
             script already nudged itself in place (the cheap layer) or when
             the run recorded actions a nudge could repeat.
          3. Wider completion signals for runtimes that cannot be resumed at
             all: a non-empty result.json is accepted as completion.
          4. Fail closed with the standard missing-confirmation message.

        Returns ``(final_status, error_message, result_artifact)``.
        """
        # Layer 3: post-exit full-log refetch + sentinel rescan.
        refetched_lines = await self._refetch_exited_session_logs(
            agent_executor, session_reference
        )
        self._scan_inplace_nudge_markers(refetched_lines)
        sentinel_found = _sentinel_in_log_lines(refetched_lines)
        self.execution_logger.log_milestone(
            "post_exit_log_rescan",
            {
                "sentinel_found": sentinel_found,
                "line_count": len(refetched_lines),
            },
        )
        if sentinel_found:
            logger.info(
                "Post-exit log rescan found the success sentinel that the "
                "live stream missed — treating the run as confirmed."
            )
            self._success_sentinel_seen.set()
            return result.status.value, result.error_message, result_artifact

        # Layer 2: one-shot confirmation round.
        nudge = await self._run_confirmation_nudge(agent_executor, refetched_lines)
        nudge_outcome = nudge.get("outcome")
        nudge_artifact = nudge.get("artifact")
        merged_artifact = result_artifact
        if isinstance(nudge_artifact, dict):
            # "Completing" result.json: keep the rich original report (if
            # any) and let the nudge's completion fields land on top.
            merged_artifact = (
                {**result_artifact, **nudge_artifact}
                if isinstance(result_artifact, dict)
                else nudge_artifact
            )

        if nudge_outcome == "confirmed_success":
            return result.status.value, result.error_message, merged_artifact

        if nudge_outcome == "explicit_failure":
            reason = nudge.get("reason") or "no reason given"
            return (
                "FAILED",
                "Agent stated in the confirmation round that the original "
                f"task did not complete: {reason}",
                merged_artifact,
            )

        # Wider completion signals, for runtimes that cannot be resumed.
        if self._accept_wider_completion_signal(agent_executor, merged_artifact):
            assert merged_artifact is not None
            logger.info(
                "Accepting a written result.json as the completion signal for "
                "a runtime that cannot be resumed in place."
            )
            self.execution_logger.log_milestone(
                "completion_signal_accepted",
                {
                    "signal": "result_artifact_present",
                    "agent_type": self.agent_type,
                    "keys": sorted(merged_artifact.keys())[:20],
                },
            )
            return result.status.value, result.error_message, merged_artifact

        # Fail closed: no confirmation even after the ladder.
        logger.warning(
            f"Agent exited with SUCCEEDED status (exit_code={result.exit_code}) "
            f"but neither success-confirmation channel was used "
            f"(no sentinel in logs, no result.json success status). "
            f"Overriding status to FAILED."
        )
        self.execution_logger.log_milestone(
            "success_confirmation_missing",
            {
                "original_status": result.status.value,
                "exit_code": result.exit_code,
                "sentinel_seen": False,
                "artifact_confirmation": _result_artifact_confirmation(result_artifact),
                "nudge_outcome": nudge_outcome,
                "inplace_nudge": self._inplace_nudge_seen,
                "inplace_nudge_unsupported": self._inplace_nudge_unsupported,
            },
        )
        # When no error heuristics fired (result.error_message is empty),
        # say explicitly that the run failed ONLY for missing confirmation,
        # and name both channels — operators must be able to tell this
        # class apart from real failures at a glance.
        error_message = result.error_message or (
            "Agent exited with code 0 but did not confirm "
            "success on either channel: the "
            f"{FLOW_SUCCESS_SENTINEL} sentinel was not printed "
            "and no result.json with a recognized completion "
            'status (top-level {"status": "success"} or an '
            "audit verdict such as pass, pass_with_findings, "
            "or fail) was written. The work may have completed "
            "without confirmation, or the agent died mid-task."
        )
        if self._inplace_nudge_seen and not result.error_message:
            # Name the reminder round: an operator reading this should not
            # have to open the timeline to learn the agent was asked again.
            error_message += (
                " The agent was reminded once in its own container and still "
                "did not confirm."
            )
        return "FAILED", error_message, result_artifact

    async def _run_confirmation_nudge(
        self, agent_executor: Any, prior_log_lines: List[str]
    ) -> Dict[str, Any]:
        """Layer 2: re-invoke the agent ONCE to confirm or deny completion.

        The nudge is a fresh, cheap invocation carrying prior context (head
        of the original prompt + tail of the previous run's output, bounded
        by ``flow_confirmation_nudge_max_tokens``). Its only job: if and only
        if the original task completed, confirm through the
        originally-instructed channel (result.json preferred, sentinel also
        accepted); otherwise state the failure plainly.

        Single-shot by construction (``_confirmation_nudge_attempted``) and a
        graceful no-op for runtimes that cannot cheaply re-invoke with prior
        context (``supports_confirmation_nudge`` is not ``True``). Every call
        logs the ``confirmation_nudge_used`` milestone with its outcome.

        Returns a dict with ``outcome`` and, when available, ``reason`` /
        ``artifact``. Outcomes: ``confirmed_success``, ``explicit_failure``,
        ``no_confirmation``, ``timeout``, ``error``,
        ``skipped_already_used``, ``skipped_unsupported_runtime``,
        ``skipped_no_execution_context``.
        """

        def _finish(
            outcome: str,
            *,
            reason: Optional[str] = None,
            artifact: Optional[Dict[str, Any]] = None,
            channel: Optional[str] = None,
            extra: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            details: Dict[str, Any] = {"outcome": outcome}
            if channel:
                details["channel"] = channel
            if reason:
                details["reason"] = str(reason)[:500]
            if extra:
                details.update(extra)
            self.execution_logger.log_milestone("confirmation_nudge_used", details)
            logger.info(f"Confirmation nudge outcome: {outcome}")
            return {"outcome": outcome, "reason": reason, "artifact": artifact}

        if self._confirmation_nudge_attempted:
            # One round max — even across flow-level retry attempts.
            return _finish("skipped_already_used")
        self._confirmation_nudge_attempted = True

        if self._inplace_nudge_seen:
            # The agent script already asked, in the container where the work
            # happened. A second round would be a second model call for the
            # same answer, from a session that knows strictly less.
            return _finish("skipped_inplace_nudge_used")

        actions_taken = self.execution_logger.get_actions_taken()
        if actions_taken:
            # A nudge re-invokes the agent. Once side effects are on record
            # (a posted comment, a push), a model that decides to "finish the
            # job" repeats them, and a duplicate review comment is worse than
            # a run reported as unconfirmed.
            return _finish(
                "skipped_actions_recorded",
                extra={"action_count": len(actions_taken)},
            )

        # Strict identity check: mock executors (AsyncMock) auto-create
        # truthy attributes, and an accidental nudge in tests or on an
        # unvalidated runtime must be impossible.
        if getattr(agent_executor, "supports_confirmation_nudge", False) is not True:
            return _finish(
                "skipped_unsupported_runtime",
                extra={"agent_type": getattr(agent_executor, "agent_type", None)},
            )

        context = self._execution_context
        if not isinstance(context, dict) or not context.get("prompt"):
            return _finish("skipped_no_execution_context")

        max_tokens = max(256, int(settings.flow_confirmation_nudge_max_tokens))
        timeout_seconds = max(30, int(settings.flow_confirmation_nudge_timeout_seconds))
        log_lines = prior_log_lines or self.execution_logger.get_agent_output_lines()

        nudge_context = dict(context)
        nudge_context["prompt"] = _build_confirmation_nudge_prompt(
            context["prompt"], log_lines, max_tokens
        )
        # No clone, no custom commands, no post-exec push/PR: the nudge must
        # not repeat side effects of the original run.
        nudge_context["git_clone_config"] = None
        nudge_context["custom_commands"] = None
        nudge_context["confirmation_nudge"] = True
        # The nudge is a second session for the SAME execution, started while
        # the original agent Job still exists (it lingers until its TTL). It
        # therefore needs its own runtime session name, or the nudge can only
        # ever fail with a 409 name conflict on Kubernetes.
        nudge_context[AGENT_SESSION_SUFFIX_KEY] = "nudge"
        # Token ceiling for runtimes that honor model parameters; the prompt
        # budget above enforces the input side regardless. The nudge ceiling
        # is a hard cap: a larger limit inherited from the flow's model is
        # clamped down, while a tighter pre-existing limit is respected.
        model_parameters = dict(context.get("model_parameters") or {})
        existing_limit = model_parameters.get("max_output_tokens")
        if isinstance(existing_limit, int) and 0 < existing_limit < max_tokens:
            model_parameters["max_output_tokens"] = existing_limit
        else:
            model_parameters["max_output_tokens"] = max_tokens
        nudge_context["model_parameters"] = model_parameters

        try:
            nudge_reference, nudge_executor = await self._start_agent_session(
                nudge_context
            )
        except Exception as start_error:
            return _finish("error", reason=_exception_message(start_error))

        try:
            poll_interval = 5
            elapsed = 0
            status: Optional[AgentStatus] = None
            terminal = (
                AgentStatus.SUCCEEDED,
                AgentStatus.FAILED,
                AgentStatus.STOPPED,
            )
            while elapsed < timeout_seconds:
                try:
                    status = await nudge_executor.get_status(nudge_reference)
                except Exception as status_error:
                    logger.warning(
                        f"Nudge status check failed: {_exception_message(status_error)}"
                    )
                if status in terminal:
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            if status not in terminal:
                try:
                    await nudge_executor.stop(nudge_reference)
                except Exception as stop_error:
                    # Best-effort stop of the timed-out nudge session; the
                    # finally block still runs cleanup, and we fail closed
                    # with the timeout outcome either way.
                    logger.warning(
                        "Failed to stop timed-out nudge session: "
                        f"{_exception_message(stop_error)}"
                    )
                return _finish(
                    "timeout",
                    extra={
                        "timeout_seconds": timeout_seconds,
                        "session_reference": nudge_reference,
                    },
                )

            nudge_logs: List[str] = []
            try:
                fetched = await nudge_executor.get_logs(nudge_reference, tail=None)
                if isinstance(fetched, list):
                    nudge_logs = [line for line in fetched if isinstance(line, str)]
            except Exception as log_error:
                logger.warning(
                    f"Failed to fetch nudge logs: {_exception_message(log_error)}"
                )

            artifact = await self._capture_result_artifact(
                nudge_executor, nudge_reference
            )
            artifact_confirmation = _result_artifact_confirmation(artifact)
            failure_reason = _failure_report_in_log_lines(nudge_logs)

            # An explicit failure statement wins over everything, mirroring
            # the main completion contract.
            if artifact_confirmation == "failure" or failure_reason is not None:
                reason = failure_reason
                if reason is None and isinstance(artifact, dict):
                    reason = (
                        artifact.get("reason")
                        or artifact.get("error")
                        or artifact.get("summary")
                        or f"result.json status={artifact.get('status')!r}"
                    )
                return _finish(
                    "explicit_failure",
                    reason=reason,
                    artifact=artifact,
                    channel=(
                        "result_artifact"
                        if artifact_confirmation == "failure"
                        else "failure_line"
                    ),
                )

            if artifact_confirmation == "success" or _sentinel_in_log_lines(nudge_logs):
                return _finish(
                    "confirmed_success",
                    artifact=artifact,
                    channel=(
                        "result_artifact"
                        if artifact_confirmation == "success"
                        else "sentinel"
                    ),
                )

            return _finish(
                "no_confirmation",
                extra={
                    "nudge_status": status.value
                    if isinstance(status, AgentStatus)
                    else None,
                    "session_reference": nudge_reference,
                },
            )
        except Exception as nudge_error:
            return _finish("error", reason=_exception_message(nudge_error))
        finally:
            try:
                await nudge_executor.cleanup()
            except Exception as cleanup_error:
                logger.warning(f"Error during nudge executor cleanup: {cleanup_error}")

    def _execution_timeout_budget(self) -> TimeoutBudget:
        """Resolve the wall-clock budget for this execution.

        A flow that carries ``timeout_seconds`` sets its own budget; every
        other flow keeps the global default. The value is clamped so that a
        bad number cannot make a flow unrunnable or let one hold a worker
        slot indefinitely.
        """
        default_seconds = int(settings.flow_execution_max_wait_seconds)
        default_seconds = max(
            FLOW_TIMEOUT_SECONDS_MIN,
            min(FLOW_TIMEOUT_SECONDS_MAX, default_seconds),
        )
        configured = getattr(self.flow, "timeout_seconds", None)
        if configured is None:
            return TimeoutBudget(seconds=default_seconds, source="default")
        try:
            seconds = int(configured)
        except (TypeError, ValueError):
            logger.warning(
                f"Flow {getattr(self.flow, 'id', None)} has a non-numeric "
                f"timeout_seconds ({configured!r}); using the default budget"
            )
            return TimeoutBudget(seconds=default_seconds, source="default")
        clamped = max(FLOW_TIMEOUT_SECONDS_MIN, min(FLOW_TIMEOUT_SECONDS_MAX, seconds))
        if clamped != seconds:
            logger.warning(
                f"Flow {getattr(self.flow, 'id', None)} timeout_seconds "
                f"{seconds} is outside [{FLOW_TIMEOUT_SECONDS_MIN}, "
                f"{FLOW_TIMEOUT_SECONDS_MAX}]; clamped to {clamped}"
            )
        return TimeoutBudget(seconds=clamped, source="flow")

    async def _monitor_agent_execution(
        self, session_reference: str, agent_executor: Any
    ) -> Dict[str, Any]:
        """
        Monitor agent execution until completion with real-time log streaming.

        Args:
            session_reference: Reference to the agent session
            agent_executor: Agent executor instance to use for monitoring

        Returns:
            Dict with execution results including status, output, errors
        """
        logger.info(f"Monitoring agent execution {session_reference}")
        timeout_budget = self._execution_timeout_budget()
        self.execution_logger.log_milestone(
            "agent_monitoring_started",
            {
                "session_reference": session_reference,
                "timeout_seconds": timeout_budget.seconds,
                "timeout_source": timeout_budget.source,
            },
        )

        try:
            # Start listening for user commands
            await self._listen_for_commands()

            # Start background task for log streaming
            self._log_streaming_task = asyncio.create_task(
                self._stream_logs_to_nats(agent_executor, session_reference)
            )

            # Poll agent status until completion, bounded by this flow's
            # timeout budget (flow.timeout_seconds, else the global default).
            max_wait_time = timeout_budget.seconds
            poll_interval = 5  # Check status every 5 seconds
            elapsed = 0
            consecutive_failures = 0
            max_consecutive_failures = (
                3  # Fail after 3 consecutive status check failures
            )
            # Grace period after success sentinel is seen in logs.
            # The agent may have finished but post-exec commands (git push,
            # PR creation) keep the container alive.  Once the sentinel
            # appears we give at most this many extra seconds before
            # treating the execution as succeeded.
            post_sentinel_grace = 120  # seconds
            sentinel_seen_at: Optional[float] = None
            last_heartbeat_at = -30  # force first heartbeat near start
            heartbeat_interval = 30

            while elapsed < max_wait_time:
                if (
                    self._orchestrator_worker_id
                    and self.execution_log is not None
                    and elapsed - last_heartbeat_at >= heartbeat_interval
                ):
                    try:
                        crud_flow_execution.touch_heartbeat(
                            self.db,
                            execution_id=self.execution_log.id,
                            worker_id=self._orchestrator_worker_id,
                        )
                        last_heartbeat_at = elapsed
                    except Exception as heartbeat_error:
                        logger.warning(
                            "Failed to touch orchestrator heartbeat for %s: %s",
                            self.execution_log.id,
                            heartbeat_error,
                        )

                stop_request = crud_flow_execution.get_stop_request(
                    self.db,
                    execution_id=self.execution_log.id,
                )
                if stop_request:
                    # Persisted intent survives a worker restart and a quick
                    # scope re-enable. A runner halt flag is only a request.
                    try:
                        await agent_executor.stop(session_reference)
                        if await agent_executor.is_stopped(session_reference) is True:
                            crud_flow_execution.confirm_stop(
                                self.db, execution_id=self.execution_log.id
                            )
                            return {
                                "status": "STOPPED",
                                "error_message": "Execution terminated after account kill-switch request",
                                "actions_taken": self.execution_logger.get_actions_taken(),
                                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                                "result": await self._capture_result_artifact(
                                    agent_executor, session_reference
                                ),
                            }
                    except Exception:
                        logger.exception(
                            "Stop requested but not confirmed for %s",
                            self.execution_log.id,
                        )
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue

                # Check if user requested stop
                if self._stop_requested.is_set():
                    logger.info(
                        f"User requested stop for execution {self.execution_log.id}"
                    )
                    await agent_executor.stop(session_reference)
                    await self._publish_update("user_stopped", {"elapsed": elapsed})
                    self.execution_logger.log_milestone(
                        "user_requested_stop", {"elapsed": elapsed}
                    )
                    # Return explicitly: falling through to the end of the
                    # while loop would report this as "Execution timed out
                    # after {max_wait_time} seconds", which is wrong and very
                    # confusing (executions stopped after 45s were reported as
                    # 3600s timeouts).
                    return {
                        "status": "STOPPED",
                        "error_message": (
                            f"Execution stopped by user request after {elapsed} seconds."
                        ),
                        "actions_taken": self.execution_logger.get_actions_taken(),
                        "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                        # An eval run may have already written result.json
                        # before the user stopped it; the container is kept
                        # (AutoRemove=False) so capture still works.
                        "result": await self._capture_result_artifact(
                            agent_executor, session_reference
                        ),
                    }

                # Get status with error handling
                try:
                    status = await agent_executor.get_status(session_reference)
                    logger.debug(f"Agent status at {elapsed}s: {status.value}")
                    consecutive_failures = 0  # Reset failure counter on success
                except Exception as status_error:
                    status_error_message = _exception_message(status_error)
                    logger.error(
                        f"Error getting agent status at {elapsed}s: {status_error_message}",
                        exc_info=True,
                    )
                    # Retry once after a short delay
                    await asyncio.sleep(2)
                    try:
                        status = await agent_executor.get_status(session_reference)
                        logger.info(f"Status check recovered: {status.value}")
                        consecutive_failures = 0  # Reset on successful retry
                    except Exception as retry_error:
                        retry_error_message = _exception_message(retry_error)
                        logger.error(
                            f"Status check retry failed: {retry_error_message}",
                            exc_info=True,
                        )
                        consecutive_failures += 1

                        # Fail execution if too many consecutive failures
                        if consecutive_failures >= max_consecutive_failures:
                            logger.error(
                                f"Agent monitoring failed after {consecutive_failures} consecutive failures"
                            )
                            self.execution_logger.log_milestone(
                                "agent_monitoring_failed",
                                {"consecutive_failures": consecutive_failures},
                            )
                            return {
                                "status": "FAILED",
                                "error_message": f"Monitoring error: {retry_error_message}",
                                "actions_taken": self.execution_logger.get_actions_taken(),
                                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                                # Best-effort: the daemon may be the very
                                # thing that is failing, but if the artifact
                                # is reachable we must not lose it.
                                "result": await self._capture_result_artifact(
                                    agent_executor, session_reference
                                ),
                            }

                        # Continue polling for transient errors
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval
                        continue

                if getattr(agent_executor, "streams_logs_externally", False) is True:
                    caught_up = await self._consume_runner_log_page(session_reference)
                    if not caught_up and status in (
                        AgentStatus.SUCCEEDED,
                        AgentStatus.FAILED,
                        AgentStatus.STOPPED,
                    ):
                        # Drain the final backlog one bounded page at a time,
                        # yielding between pages before deciding completion.
                        await asyncio.sleep(0)
                        continue

                # Publish status update (best effort - don't fail if NATS is down)
                try:
                    await self._publish_update(
                        "agent_status", {"status": status.value, "elapsed": elapsed}
                    )
                except Exception as publish_error:
                    logger.warning(f"Failed to publish status update: {publish_error}")

                loop_detection = await self._sync_runtime_tool_activity_metrics()
                if loop_detection:
                    repeated_tools = ", ".join(
                        f"{item.get('server_name')}/{item.get('tool_name')}"
                        for item in loop_detection["pattern"]
                    )
                    error_message = (
                        "Execution stopped after detecting a repeated MCP tool loop: "
                        f"{repeated_tools} repeated "
                        f"{loop_detection['repetitions']} times with identical "
                        "arguments."
                    )
                    logger.warning(error_message)
                    self.execution_logger.log_milestone(
                        "mcp_tool_loop_detected",
                        {**loop_detection, "elapsed": elapsed},
                    )
                    await self._publish_update(
                        "agent_loop_detected",
                        {
                            "error": error_message,
                            "elapsed": elapsed,
                            **loop_detection,
                        },
                    )
                    await agent_executor.stop(session_reference)
                    return {
                        "status": "FAILED",
                        "error_message": error_message,
                        "actions_taken": self.execution_logger.get_actions_taken(),
                        "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                        # Capture whatever the agent reported before it got
                        # stuck in the tool loop.
                        "result": await self._capture_result_artifact(
                            agent_executor, session_reference
                        ),
                    }

                if status in (
                    AgentStatus.SUCCEEDED,
                    AgentStatus.FAILED,
                    AgentStatus.STOPPED,
                ):
                    # Agent finished, get final result
                    logger.info(
                        f"Agent finished with status {status.value} at {elapsed}s"
                    )
                    result = await agent_executor.get_result(session_reference)

                    self.execution_logger.log_milestone(
                        "agent_execution_completed",
                        {
                            "status": result.status.value,
                            "exit_code": result.exit_code,
                            "container_termination": (
                                asdict(result.termination)
                                if result.termination is not None
                                else None
                            ),
                        },
                    )

                    # Structured result artifact (/workspace/result.json)
                    # captured first-class from the workspace — the
                    # eval/observe contract surface (no log scraping). Fetched
                    # BEFORE the status decision: it is confirmation channel 2.
                    result_artifact = await self._capture_result_artifact(
                        agent_executor, session_reference
                    )
                    artifact_confirmation = _result_artifact_confirmation(
                        result_artifact
                    )

                    # Positive-confirmation contract (fail-closed):
                    # Exit code 0 is NEVER sufficient for success — agent CLIs
                    # exit 0 even when the agent died mid-task. A run reported
                    # SUCCEEDED by the container must be explicitly confirmed
                    # through one of two channels, either of which suffices:
                    #   1. the FLOW_EXECUTION_SUCCESS sentinel printed in logs
                    #   2. a result.json artifact with a success status
                    # An explicit failure status in result.json wins over
                    # everything, including a printed sentinel.
                    # Guard: only apply the override when the agent-exec-start
                    # marker was actually seen in logs.  If we never streamed
                    # real logs (e.g. mocks, or the log stream failed before
                    # any output), the sentinel's absence is not meaningful.
                    final_status = result.status.value
                    error_message = result.error_message

                    if (
                        result.status == AgentStatus.SUCCEEDED
                        and artifact_confirmation == "failure"
                    ):
                        assert result_artifact is not None
                        artifact_status = result_artifact.get("status")
                        logger.warning(
                            "Agent exited with SUCCEEDED status but result.json "
                            "reports an explicit failure status "
                            f"({artifact_status!r}). "
                            "Overriding status to FAILED."
                        )
                        self.execution_logger.log_milestone(
                            "result_artifact_failure_override",
                            {
                                "original_status": result.status.value,
                                "exit_code": result.exit_code,
                                "artifact_status": artifact_status,
                                "sentinel_seen": self._success_sentinel_seen.is_set(),
                            },
                        )
                        final_status = "FAILED"
                        error_message = result.error_message or (
                            "Agent reported an explicit failure in result.json "
                            f"(status={artifact_status!r})."
                        )
                    elif (
                        result.status == AgentStatus.SUCCEEDED
                        and self._agent_exec_started
                        and not self._success_sentinel_seen.is_set()
                        and artifact_confirmation != "success"
                    ):
                        # Neither confirmation channel was used. Before
                        # failing closed, walk the recovery ladder:
                        #   1. refetch the COMPLETE runtime logs and rescan
                        #      for the sentinel (a late stream reconnect can
                        #      lose the tail of an otherwise confirmed run),
                        #   2. one confirmation-round nudge (supported
                        #      runtimes only),
                        #   3. FAILED with the standard missing-confirmation
                        #      message.
                        (
                            final_status,
                            error_message,
                            result_artifact,
                        ) = await self._resolve_missing_confirmation(
                            agent_executor,
                            session_reference,
                            result,
                            result_artifact,
                        )
                    elif (
                        result.status == AgentStatus.SUCCEEDED
                        and not self._success_sentinel_seen.is_set()
                        and artifact_confirmation == "success"
                    ):
                        # The artifact alone confirmed the run — record it so
                        # operators can see which channel was used.
                        assert result_artifact is not None
                        self.execution_logger.log_milestone(
                            "result_artifact_confirmed_success",
                            {"artifact_status": result_artifact.get("status")},
                        )

                    return {
                        "status": final_status,
                        "output_summary": result.output_summary,
                        "error_message": error_message,
                        "actions_taken": self.execution_logger.get_actions_taken(),
                        "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                        "result": result_artifact,
                        "exit_code": result.exit_code,
                        "container_termination": (
                            asdict(result.termination)
                            if result.termination is not None
                            else None
                        ),
                        # First-pass failure classification made against the
                        # FULL container logs. error_message keeps only the
                        # generated sentence, which cannot encode the
                        # transient/terminal verdict — _retry_decision needs
                        # this to survive.
                        "failure_analysis": (
                            asdict(result.failure_analysis)
                            if result.failure_analysis is not None
                            else None
                        ),
                    }

                # Check if the success sentinel was seen in logs while
                # the container is still running (post-exec commands).
                # The sentinel is only armed after AGENT_EXEC_START_MARKER is
                # seen, so prompt echoes cannot trigger it.
                if self._success_sentinel_seen.is_set():
                    if sentinel_seen_at is None:
                        sentinel_seen_at = elapsed
                        logger.info(
                            f"Success sentinel seen at {elapsed}s, "
                            f"allowing {post_sentinel_grace}s grace period"
                        )
                    elif elapsed - sentinel_seen_at >= post_sentinel_grace:
                        logger.info(
                            f"Grace period expired ({post_sentinel_grace}s) "
                            f"after success sentinel — treating as SUCCEEDED"
                        )
                        self.execution_logger.log_milestone(
                            "sentinel_grace_period_expired",
                            {"sentinel_seen_at": sentinel_seen_at, "elapsed": elapsed},
                        )
                        # Container is still alive here (post-exec
                        # commands); the archive API works either way.
                        result_artifact = await self._capture_result_artifact(
                            agent_executor, session_reference
                        )
                        if _result_artifact_confirmation(result_artifact) == "failure":
                            # Same invariant as the terminal path: an explicit
                            # failure in result.json wins over the sentinel.
                            assert result_artifact is not None
                            artifact_status = result_artifact.get("status")
                            # Container may still be in post-exec; fetch the
                            # result so _retry_decision sees exit_code instead
                            # of treating it as unknown.
                            result = await agent_executor.get_result(session_reference)
                            self.execution_logger.log_milestone(
                                "result_artifact_failure_override",
                                {
                                    "artifact_status": artifact_status,
                                    "sentinel_seen": True,
                                    "exit_code": result.exit_code,
                                },
                            )
                            return {
                                "status": "FAILED",
                                "error_message": (
                                    "Agent reported an explicit failure in "
                                    "result.json (status="
                                    f"{artifact_status!r})."
                                ),
                                "actions_taken": self.execution_logger.get_actions_taken(),
                                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                                "result": result_artifact,
                                "exit_code": result.exit_code,
                                "failure_analysis": (
                                    asdict(result.failure_analysis)
                                    if result.failure_analysis is not None
                                    else None
                                ),
                            }
                        return {
                            "status": "SUCCEEDED",
                            "output_summary": self.execution_logger.get_agent_output_summary(),
                            "error_message": None,
                            "actions_taken": self.execution_logger.get_actions_taken(),
                            "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                            "result": result_artifact,
                        }

                # Wait before next poll
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            # Timeout reached
            logger.warning(
                f"Agent execution {session_reference} timed out after {max_wait_time}s"
            )
            self.execution_logger.log_milestone(
                "agent_execution_timeout",
                {
                    "timeout_seconds": timeout_budget.seconds,
                    "timeout_source": timeout_budget.source,
                },
            )
            await agent_executor.stop(session_reference)

            return {
                "status": "FAILED",
                "error_message": timeout_budget.timeout_message(),
                "actions_taken": self.execution_logger.get_actions_taken(),
                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                # A timed-out eval run may still have written result.json;
                # the stopped container is kept, so the artifact is reachable.
                "result": await self._capture_result_artifact(
                    agent_executor, session_reference
                ),
            }

        except Exception as e:
            error_message = _exception_message(e)
            logger.error(
                f"Error monitoring agent execution {session_reference}: {error_message}",
                exc_info=True,
            )
            self.execution_logger.log_milestone(
                "agent_execution_error", {"error": error_message}
            )
            return {
                "status": "FAILED",
                "error_message": f"Monitoring error: {error_message}",
                "actions_taken": self.execution_logger.get_actions_taken(),
                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                # _capture_result_artifact never raises; best-effort capture
                # so an unexpected monitor error does not lose the artifact.
                "result": await self._capture_result_artifact(
                    agent_executor, session_reference
                ),
            }
        finally:
            # Always cleanup monitoring resources
            await self._cleanup_monitoring()
            # Cleanup agent executor resources (close Kubernetes/Docker clients)
            try:
                await agent_executor.cleanup()
                self._publication_runtime_stopped = True
            except Exception as cleanup_error:
                self._publication_runtime_stopped = False
                logger.warning(f"Error during agent cleanup: {cleanup_error}")

    def _create_execution_log(self):
        """Create an initial record in FlowExecutions.

        No-op when ``execution_log`` was pre-created (manual trigger / worker
        dispatch) so ``run()`` can be reused for both paths.
        """
        if self.execution_log is not None:
            logger.info("Using pre-created execution log: %s", self.execution_log.id)
            self._sync_runtime_session()
            return

        logger.info("Creating initial execution log")

        # Ensure trigger_event_data is JSON serializable (convert UUIDs, datetimes, etc.)
        serializable_event_data = _make_json_serializable(self.trigger_event_data)
        attach_trigger_subject(serializable_event_data)
        attach_workspace_file_paths(serializable_event_data)

        execution_create = schemas.FlowExecutionCreate(
            flow_id=self.flow_id,
            status="PENDING",
            trigger_event_details=serializable_event_data,
            trigger_event_id=self.trigger_event_data.get("event_id"),
        )

        db_execution_log = crud_flow_execution.create(self.db, obj_in=execution_create)
        self.db.commit()
        self.db.refresh(db_execution_log)
        self.execution_log = db_execution_log
        self._sync_runtime_session()

        logger.info(f"Execution log created with ID: {self.execution_log.id}")

    async def _update_execution_log(self, status: str, **kwargs):
        """Update the execution log and publish the update to NATS."""
        logger.info(f"Updating execution log to status: {status}")

        # Every terminal write goes through here, so this is the one place
        # that guarantees a failed execution carries a failure_category.
        # Callers that hold richer evidence (an agent result with a failure
        # analysis, or the exception that aborted the run) pass an explicit
        # category and it is respected; everyone else gets one derived from
        # the message being stored, falling back to the message already on
        # the row when this update only moves the status.
        if kwargs.get("failure_category") is None:
            derived = derive_failure_category(
                status=status,
                error_message=(
                    kwargs.get("error_message")
                    or getattr(self.execution_log, "error_message", None)
                ),
            )
            existing = getattr(self.execution_log, "failure_category", None)
            if derived == FAILURE_CATEGORY_UNKNOWN and existing:
                # Never downgrade a category an earlier, better-informed
                # write already established.
                derived = existing
            if derived is not None:
                kwargs["failure_category"] = derived

        # Debug logging for metrics
        if "tool_calls_count" in kwargs or "total_tokens" in kwargs:
            logger.info(
                f"Updating execution metrics: tool_calls_count={kwargs.get('tool_calls_count')}, "
                f"total_tokens={kwargs.get('total_tokens')}, estimated_cost={kwargs.get('estimated_cost')}"
            )

        update_data = schemas.FlowExecutionUpdate(status=status, **kwargs)

        # Debug: Log what fields are actually in the update
        update_dict = update_data.model_dump(exclude_unset=True)
        logger.info(f"Update data fields: {list(update_dict.keys())}")
        if "tool_calls_count" in update_dict or "total_tokens" in update_dict:
            logger.info(
                f"Update dict metrics: tool_calls_count={update_dict.get('tool_calls_count')}, "
                f"total_tokens={update_dict.get('total_tokens')}, estimated_cost={update_dict.get('estimated_cost')}"
            )

        updated_log = crud_flow_execution.update(
            self.db, db_obj=self.execution_log, obj_in=update_data
        )
        self.db.commit()
        self.db.refresh(updated_log)
        self.execution_log = updated_log

        # Debug: Verify the values were actually set
        if "tool_calls_count" in kwargs or "total_tokens" in kwargs:
            logger.info(
                f"After update - DB values: tool_calls_count={updated_log.tool_calls_count}, "
                f"total_tokens={updated_log.total_tokens}, estimated_cost={updated_log.estimated_cost}"
            )

        # Publish update to NATS for real-time UI updates
        # Convert datetime objects to ISO format strings for JSON serialization
        serializable_kwargs = {}
        for key, value in kwargs.items():
            if isinstance(value, datetime):
                serializable_kwargs[key] = value.isoformat()
            else:
                serializable_kwargs[key] = value

        await self._publish_update(
            "status_update", {"status": status, **serializable_kwargs}
        )

        logger.debug(f"Execution log updated: status={status}")

    async def _notify_terminal(
        self,
        status: str,
        failure_category: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Post configured tracker comments after a terminal status write.

        Never raises: a notification failure must not rewrite the execution
        status that was just persisted.
        """
        try:
            if not self.flow or not self.execution_log:
                return
            notifications = getattr(self.flow, "notifications", None)
            if not notifications:
                return

            log_lines: List[str] = []
            if self.execution_logger:
                summary = self.execution_logger.get_agent_output_summary(tail_lines=20)
                if summary:
                    log_lines = summary.splitlines()

            tracker_client = None
            if needs_tracker_comment(notifications, status, failure_category):
                tracker_client = await self._get_tracker_client_for_status()

            execution_id = str(self.execution_log.id)
            trigger_details = (
                getattr(self.execution_log, "trigger_event_details", None)
                or self.trigger_event_data
            )
            result_payload = (
                result
                if result is not None
                else getattr(self.execution_log, "result", None)
            )
            await notify_terminal_execution(
                notifications=notifications,
                status=status,
                failure_category=failure_category
                or getattr(self.execution_log, "failure_category", None),
                execution_id=execution_id,
                execution_url=execution_console_url(execution_id),
                trigger_event_details=trigger_details,
                result=result_payload if isinstance(result_payload, dict) else None,
                log_lines=log_lines,
                tracker_client=tracker_client,
            )
        except Exception:
            logger.warning(
                "Flow terminal notification failed for execution %s",
                getattr(self.execution_log, "id", "unknown"),
                exc_info=True,
            )

    def _retry_decision(self, agent_result: Dict[str, Any]) -> Optional[str]:
        """Decide whether a failed attempt may be retried.

        Returns ``None`` when the attempt is retryable, or a short reason
        string explaining why it is not. The reason is logged so an operator
        can see *why* a failure was treated as terminal.

        The safety boundary is the container's post-execution block (git push,
        pull-request/merge-request creation), which the agent entrypoints run
        only when the agent process exited ``0``. A non-zero exit therefore
        means no external side effect was produced by the container and the
        attempt can be repeated safely. Anything else — an exit code of 0, an
        unknown exit code, or side effects already recorded on the timeline —
        is treated as unsafe, because re-running it risks a duplicate comment,
        push or pull request. A wrong retry is worse than no retry.
        """
        status = agent_result.get("status")
        if status != "FAILED":
            # STOPPED (user requested) and SUCCEEDED are never retried.
            return f"status is {status}, not FAILED"

        exit_code = agent_result.get("exit_code")
        if exit_code is None:
            return "agent exit code is unknown, so external side effects cannot be ruled out"
        if exit_code == 0:
            return (
                "agent exited 0, so the container ran its post-execution "
                "push/pull-request block; retrying could double-post"
            )

        if self.execution_logger.get_actions_taken():
            return "the agent already recorded actions; retrying could repeat them"

        if not self._failure_is_transient(agent_result):
            return "the failure is not a transient upstream failure"

        return None

    def _failure_is_transient(self, agent_result: Dict[str, Any]) -> bool:
        """Whether the failed attempt is worth retrying.

        Prefers the first-pass verdict the agent executor classified against
        the FULL container logs (``failure_analysis`` on the result). The
        stored ``error_message`` is a lossy summary — a raw severed-stream
        stack yields a fallback message that re-analyses as non-transient,
        and a no-status "unreachable" sentence re-analyses as transient even
        for a policy-terminated run — so the message is only consulted for
        legacy results that carry no attached verdict.
        """
        # A publication-gate denial is a verdict about the code, not a
        # provider blip: retrying the identical attempt would fail the same
        # gate. The work goes back to the agent instead (issue #428). The
        # guard runs before the analysis because the agent phase of a
        # denied run can still contain transient-looking provider noise.
        error_message = agent_result.get("error_message") or ""
        if VERIFICATION_DENIED_MARKER in str(error_message) or any(
            VERIFICATION_DENIED_MARKER in str(line)
            for line in self.execution_logger.get_agent_output_lines()[-200:]
        ):
            return False

        analysis_payload = agent_result.get("failure_analysis")
        if isinstance(analysis_payload, dict) and "transient" in analysis_payload:
            return bool(analysis_payload["transient"])
        if analysis_payload is not None and hasattr(analysis_payload, "transient"):
            # Tolerate an un-serialised AgentFailureAnalysis instance.
            return bool(analysis_payload.transient)

        return analyze_agent_failure(error_message).transient

    async def _run_agent_with_retries(
        self, execution_context: Dict[str, Any]
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """Start and monitor the agent, retrying transient upstream failures.

        One provider having a bad minute should not kill a whole review. Each
        attempt gets a fresh agent session; between attempts we back off so a
        briefly-overloaded provider has time to recover.

        Retries are deliberately conservative — see :meth:`_retry_decision`
        for the side-effect boundary that governs them — and always visible:
        every retry is recorded as an ``execution_retry_scheduled`` milestone
        and surfaced on the execution timeline, so flakiness is never hidden.

        Args:
            execution_context: Context prepared for the agent.

        Returns:
            Tuple of ``(agent_result, session_reference)`` for the final
            attempt made.
        """
        max_attempts = max(1, int(settings.flow_execution_max_attempts))
        backoff_seconds = max(0, int(settings.flow_execution_retry_backoff_seconds))

        # Kept for the completion-confirmation round: the nudge re-invokes
        # the agent with a minimal follow-up prompt derived from this context.
        self._execution_context = execution_context

        agent_result: Dict[str, Any] = {}
        session_reference: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            # Each attempt starts a NEW agent session, so it must ask the
            # runtime for a new session name. Without this, attempt 2 asked
            # Kubernetes for the Job name attempt 1 still owns and died with
            # "Failed to start agent Job: (409) Conflict" — the retry that
            # was supposed to rescue a transient provider failure became the
            # thing that failed the run. Attempt 1 keeps the historic
            # unsuffixed name so an in-flight run stays addressable by the
            # session reference stored before this change.
            execution_context[AGENT_SESSION_SUFFIX_KEY] = (
                f"a{attempt}" if attempt > 1 else None
            )
            # Each attempt runs its own CLI session; forget the previous
            # attempt's marker so the retry's session overwrites it.
            self._agent_session = None
            from preloop.services.kill_switch import FlowHaltActiveError

            try:
                session_reference, agent_executor = await self._start_agent_session(
                    execution_context
                )
            except FlowHaltActiveError:
                return {
                    "status": "STOPPED",
                    "error_message": "Account kill switch prevented agent launch",
                }, None

            await self._update_execution_log(
                status="RUNNING",
                agent_session_reference=session_reference,
            )
            self._sync_runtime_session(session_reference=session_reference)

            agent_result = await self._monitor_agent_execution(
                session_reference, agent_executor
            )

            if attempt >= max_attempts:
                if agent_result.get("status") == "FAILED" and max_attempts > 1:
                    logger.info(
                        "Execution %s exhausted all %s attempts",
                        self.execution_log.id if self.execution_log else "unknown",
                        max_attempts,
                    )
                    # Mirror the per-retry milestone: without this, the final
                    # failure of a retried run looks identical on the timeline
                    # to a first-attempt failure.
                    self.execution_logger.log_milestone(
                        "execution_retries_exhausted",
                        {
                            "attempts": max_attempts,
                            "session_reference": session_reference,
                        },
                    )
                    await self._emit_execution_warning(
                        f"All {max_attempts} attempts failed; giving up.",
                        details={"attempts": max_attempts},
                    )
                break

            reason = self._retry_decision(agent_result)
            if reason is not None:
                logger.info(
                    "Not retrying execution %s: %s",
                    self.execution_log.id if self.execution_log else "unknown",
                    reason,
                )
                break

            delay = backoff_seconds * (2 ** (attempt - 1))
            failure_summary = (agent_result.get("error_message") or "").strip()
            logger.warning(
                "Attempt %s/%s of execution %s hit a transient upstream failure; "
                "retrying in %ss",
                attempt,
                max_attempts,
                self.execution_log.id if self.execution_log else "unknown",
                delay,
            )
            self.execution_logger.log_milestone(
                "execution_retry_scheduled",
                {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": delay,
                    "reason": failure_summary,
                    "session_reference": session_reference,
                },
            )
            # Surfaced on the timeline so a user can see the run was retried
            # rather than silently taking twice as long.
            await self._emit_execution_warning(
                f"Attempt {attempt} of {max_attempts} failed with a transient "
                f"upstream error; retrying. ({failure_summary})",
                details={"attempt": attempt, "max_attempts": max_attempts},
            )
            await self._publish_update(
                "execution_retry",
                {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": delay,
                },
            )

            if delay:
                await asyncio.sleep(delay)

        return agent_result, session_reference

    async def _finish_isolated_publication(self, agent_result: Dict[str, Any]) -> None:
        """Run trusted publication after runtime cleanup; failure changes status."""
        policy = getattr(self, "_isolated_publication_policy", None)
        if policy is None:
            return
        import httpx
        from preloop.services.isolated_publication import finish_isolated_publication
        from preloop.services.publication_credentials import revoke_repository_lease
        from preloop.services.trusted_publisher import PublicationError

        try:
            if agent_result.get("status") != "SUCCEEDED":
                return
            if not getattr(self, "_publication_runtime_stopped", False):
                raise PublicationError(
                    "Agent runtime cleanup was not confirmed; refusing to issue publication credentials"
                )
            publication = await finish_isolated_publication(
                self.db,
                policy,
                agent_result,
                self._evidence_archive,
                getattr(self, "_publication_verification", None),
            )
            result = dict(agent_result.get("result") or {})
            result["trusted_publication"] = publication
            agent_result["result"] = result
            self._opened_pr = publication
            self.execution_logger.log_milestone(
                "trusted_publication_succeeded",
                {
                    "url": publication["url"],
                    "head_sha": publication["head_sha"],
                    "metadata_warnings": publication.get("metadata_warnings", []),
                },
            )
        except PublicationError as exc:
            agent_result["status"] = "FAILED"
            agent_result["error_message"] = str(exc)
            self.execution_logger.log_milestone(
                "trusted_publication_failed", {"reason": str(exc)}
            )
        finally:
            async with httpx.AsyncClient() as client:
                try:
                    await revoke_repository_lease(policy.read_lease, client)
                except PublicationError as exc:
                    self.execution_logger.log_milestone(
                        "publication_credential_revocation_failed", {"reason": str(exc)}
                    )

    async def run(self):
        """
        Execute the flow through its full lifecycle.

        Lifecycle stages:
        1. PENDING: Execution log created
        2. INITIALIZING: Flow and AI model details retrieved
        3. RUNNING: Agent session started
        4. SUCCEEDED/FAILED: Execution completed

        A failed attempt whose cause was a transient upstream model-provider
        failure is retried (see :meth:`_run_agent_with_retries`).
        """
        try:
            # Stage 1: Retrieve flow details first (needed for account_id in messages)
            self._get_flow_details()

            # Stage 2: Create execution log
            self._create_execution_log()

            # Publish execution_started event for UI notification
            # This allows the flow executions list to update automatically
            await self._publish_update(
                "execution_started",
                {
                    "status": "PENDING",
                    "flow_id": str(self.flow_id),
                    "flow_name": self.flow.name if self.flow else None,
                },
            )

            await self._publish_update("status_update", {"status": "PENDING"})
            logger.info(f"Flow execution started: {self.execution_log.id}")

            # Update commit status to pending (appears in GitHub/GitLab checks)
            await self._update_commit_status(
                state="pending",
                description=f"Preloop is reviewing: {self.flow.name}"
                if self.flow
                else "Preloop is reviewing",
            )

            # Stage 3: Mark as initializing
            await self._update_execution_log(status="INITIALIZING")

            # Stage 3: Prepare execution context
            execution_context = await self._prepare_execution_context()

            # Store resolved prompt for debugging/audit and mark as STARTING
            await self._update_execution_log(
                status="STARTING",
                resolved_input_prompt=execution_context["prompt"],
            )

            # Stages 4 and 5: start the agent and monitor it, retrying the
            # whole attempt when the upstream model provider failed in a way
            # that another attempt could plausibly survive.
            agent_result, session_reference = await self._run_agent_with_retries(
                execution_context
            )

            # Private runners persist lines on the WebSocket; the live stream
            # skips them so they are not published twice. Fold them in before
            # terminal PR/session/metrics binding.
            await self._replay_persisted_runner_logs()

            await self._finish_isolated_publication(agent_result)

            # Update execution log with final results including detailed logs
            final_status = agent_result.get("status", "FAILED")

            # Use output_summary from agent result, or fallback to stored logs
            output_summary = agent_result.get("output_summary")
            if not output_summary:
                logger.warning(
                    "Agent result has no output_summary, using stored logs as fallback"
                )
                output_summary = self.execution_logger.get_agent_output_summary()
                if output_summary:
                    logger.info(
                        f"Using stored logs for output_summary ({len(output_summary)} chars)"
                    )

            # Sync metrics one last time before final status
            try:
                from preloop.services.execution_metrics import ExecutionMetricsService

                metrics_service = ExecutionMetricsService(self.db)
                final_metrics = metrics_service.get_execution_metrics(
                    str(self.execution_log.id)
                )
                self.tool_calls_count = final_metrics.get(
                    "tool_calls", self.tool_calls_count
                )
                self.total_tokens = final_metrics.get("token_usage", {}).get(
                    "total_tokens", self.total_tokens
                )
                self.estimated_cost = final_metrics.get(
                    "estimated_cost", self.estimated_cost
                )
            except Exception as e:
                logger.error(f"Failed to calculate final metrics for execution: {e}")

            # Persist the evidence pack (if one was captured before executor
            # cleanup) in the same transaction as the terminal status update.
            have_binary_artifacts = (
                self._evidence_archive is not None
                or self._workspace_snapshot is not None
            )
            if have_binary_artifacts and self.execution_log is not None:
                try:
                    if self._evidence_archive is not None:
                        crud_flow_execution.set_evidence_archive(
                            self.db,
                            db_obj=self.execution_log,
                            archive=self._evidence_archive,
                        )
                    if self._workspace_snapshot is not None:
                        crud_flow_execution.set_workspace_snapshot(
                            self.db,
                            db_obj=self.execution_log,
                            archive=self._workspace_snapshot,
                        )
                except Exception as evidence_error:
                    logger.warning(
                        "Failed to persist evidence archive / workspace "
                        f"snapshot: {evidence_error}"
                    )
                    # A failed flush must not poison the terminal status
                    # update that follows.
                    #
                    # NOTE: rollback() discards ALL pending session state,
                    # not just the failed archive. That is safe here only
                    # because the archive is the sole pending change at this
                    # point and _update_execution_log() below re-persists
                    # status/result/metrics and commits. If you add another
                    # pending write to this terminal block BEFORE this line,
                    # it would be silently dropped — narrow this recovery
                    # (expire + rebind the execution row) instead.
                    self.db.rollback()

            # The wrapper opens PRs with a raw curl whose response never
            # reaches Python; bind it here, before the refresh below, so the
            # merged result keeps pr_url for comment-driven resume.
            self._bind_opened_pr(output_summary)
            # Same channel pattern for the native CLI session id: rescued
            # from the output summary when the live stream missed it.
            self._bind_cli_session(output_summary)

            # MCP create_pull_request writes pr_url onto result in another
            # session. Refresh so a None agent result cannot wipe the binding.
            try:
                self.db.refresh(self.execution_log)
            except Exception:
                logger.debug(
                    "Could not refresh execution log before merging result",
                    exc_info=True,
                )
            merged_result = merge_result_preserving_pr_binding(
                getattr(self.execution_log, "result", None),
                agent_result.get("result"),
            )

            # Only the executor's runtime observation owns this key. Agent
            # result.json claims cannot overwrite or fabricate termination.
            if isinstance(merged_result, dict):
                merged_result.pop("container_termination", None)
            termination = agent_result.get("container_termination")
            if isinstance(termination, dict):
                if not isinstance(merged_result, dict):
                    merged_result = {}
                merged_result["container_termination"] = termination

            # Publication-gate evidence (issue #428): the runner-captured
            # verdict owns the ``verification`` key of the stored result, so
            # the implemented status (the agent's ``status`` field) and the
            # verification status (``verification.status``) stay two
            # different, separately auditable things. An agent-authored
            # ``verification`` claim survives renamed as
            # ``verification_reported``.
            verification_evidence = self._resolve_verification_evidence()
            if verification_evidence is not None and isinstance(merged_result, dict):
                separate_agent_verification_claim(merged_result)
                merged_result["verification"] = {
                    **verification_evidence,
                    "source": "sandbox_log",
                    "authenticated": False,
                }

            await self._update_execution_log(
                status=final_status,
                model_output_summary=output_summary,
                error_message=agent_result.get("error_message"),
                # The agent executor already classified the failure against
                # the FULL container logs; that verdict is strictly better
                # evidence than the truncated error message, so pass it in
                # rather than letting _update_execution_log re-derive from
                # prose.
                failure_category=derive_failure_category(
                    status=final_status,
                    error_message=agent_result.get("error_message"),
                    failure_analysis=agent_result.get("failure_analysis"),
                ),
                actions_taken_summary=agent_result.get("actions_taken"),
                mcp_usage_logs=agent_result.get("mcp_usage_logs"),
                result=merged_result,
                end_time=datetime.now(timezone.utc),
                tool_calls_count=self.tool_calls_count,
                total_tokens=self.total_tokens,
                estimated_cost=self.estimated_cost,
            )
            self._sync_runtime_session(ended_at=datetime.now(timezone.utc))

            # Update commit status to success/failure
            status_state = "success" if final_status == "SUCCEEDED" else "failure"
            status_description = (
                f"Preloop review completed: {self.flow.name}"
                if self.flow
                else "Preloop review completed"
            )
            if final_status != "SUCCEEDED":
                status_description = f"Preloop review failed: {agent_result.get('error_message', 'Unknown error')[:80]}"
            await self._update_commit_status(
                state=status_state,
                description=status_description,
            )
            await self._notify_terminal(
                status=final_status,
                failure_category=derive_failure_category(
                    status=final_status,
                    error_message=agent_result.get("error_message"),
                    failure_analysis=agent_result.get("failure_analysis"),
                ),
                result=merged_result,
            )

            logger.info(
                f"Flow execution completed with status {final_status}: {self.execution_log.id}"
            )

            # Comments that arrived while this run was going were queued as a
            # single follow-up; start it now that the run is terminal.
            await self._start_queued_followup()

        except asyncio.CancelledError:
            # Deploy drain: the worker cancels in-flight handlers, releases the
            # claim and re-dispatches so a peer resumes monitoring the agent,
            # which keeps running. Nothing is finalized here (the execution is
            # not over) and, critically, the runtime token is left active for
            # the agent that is still streaming.
            logger.info(
                "Flow execution %s interrupted; leaving it to the worker that "
                "resumes it (agent and runtime token untouched)",
                self.execution_log.id if self.execution_log else "unknown",
            )
            raise
        except Exception as e:
            logger.error(
                f"Flow execution {self.execution_log.id if self.execution_log else 'unknown'} failed: {e}",
                exc_info=True,
            )

            # Update commit status to failure
            await self._update_commit_status(
                state="failure",
                description=f"Preloop execution failed: {str(e)[:80]}",
            )

            if self.execution_log:
                try:
                    # Sync metrics one last time before final status
                    try:
                        from preloop.services.execution_metrics import (
                            ExecutionMetricsService,
                        )

                        metrics_service = ExecutionMetricsService(self.db)
                        final_metrics = metrics_service.get_execution_metrics(
                            str(self.execution_log.id)
                        )
                        self.tool_calls_count = final_metrics.get(
                            "tool_calls", self.tool_calls_count
                        )
                        self.total_tokens = final_metrics.get("token_usage", {}).get(
                            "total_tokens", self.total_tokens
                        )
                        self.estimated_cost = final_metrics.get(
                            "estimated_cost", self.estimated_cost
                        )
                    except Exception as metrics_error:
                        logger.error(
                            f"Failed to calculate final metrics for failed execution: {metrics_error}"
                        )

                    exception_category = derive_failure_category(
                        status="FAILED",
                        error_message=str(e),
                        exception=e,
                    )
                    await self._update_execution_log(
                        status="FAILED",
                        error_message=str(e),
                        # The exception type carries the category for failures
                        # raised by the runtime itself (e.g. AgentStartError on
                        # an unresolvable Job name conflict), which no amount
                        # of message matching would recover as reliably.
                        failure_category=exception_category,
                        end_time=datetime.now(timezone.utc),
                        tool_calls_count=self.tool_calls_count,
                        total_tokens=self.total_tokens,
                        estimated_cost=self.estimated_cost,
                    )
                    self._sync_runtime_session(ended_at=datetime.now(timezone.utc))
                    await self._notify_terminal(
                        status="FAILED",
                        failure_category=exception_category,
                    )
                except Exception as update_error:
                    logger.error(
                        f"Failed to update execution log after error: {update_error}",
                        exc_info=True,
                    )
                # A failed run is still terminal: release the queued
                # follow-up so a review comment is not lost with it.
                await self._start_queued_followup()
            else:
                logger.error("Cannot update execution log - not created yet")
        finally:
            # Retire the runtime token, but only if this execution is over.
            self._cleanup_temporary_api_token()
