"""The control plane posts a finished read-only review and a commit status.

The run itself stays ``publication_mode: read_only``: the container, the
model, the approval hook and the CI identity never hold a credential that can
write to the code host. After the execution is terminal and its result passed
the flow's ``result_schema``, this module mints a GitHub App installation
token for the one repository of the execution's own binding, carrying
``pull_requests: write`` and ``statuses: write`` and no ``contents``
permission, uses it for one review and one commit status on the reviewed
commit, reads both back, and revokes it.

Design note and threat model: ``docs/architecture/platform-posted-review.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import crud, models
from preloop.models.crud.platform_review_publication import CLAIM_STALE_AFTER
from preloop.models.models.platform_review_publication import (
    STATE_FAILED,
    STATE_POSTED,
    STATE_PUBLISHING,
    STATE_SKIPPED,
    STATE_STATUS_ONLY,
)
from preloop.schemas.ci_execution import (
    CiReviewBinding,
    EventReviewBinding,
    parse_stored_review_binding,
)
from preloop.schemas.ci_principal import CiAction
from preloop.services.flow_result_schema import validate_result_document
from preloop.services.publication_credentials import (
    LeaseRefusedError,
    mint_repository_lease,
    revoke_repository_lease,
    validate_publication_tracker,
)
from preloop.services.trusted_publisher import PublicationError, PublicationLease

logger = logging.getLogger(__name__)

#: Key of the opt-in block inside ``git_clone_config``.
CONFIG_KEY = "platform_review"
#: Posting modes. Automatic posts when the execution finishes; manual posts
#: only on the explicit publish call.
MODE_OFF = "off"
MODE_AUTOMATIC = "automatic"
MODE_MANUAL = "manual"
#: Stored values of ``platform_review.mode``; off is ``enabled: false``.
STORED_MODES: frozenset[str] = frozenset({MODE_AUTOMATIC, MODE_MANUAL})
#: How the review of an execution reached the pull request.
POSTED_AUTOMATICALLY = "automatic"
POSTED_MANUALLY = "manual"
NOT_POSTED = "none"
#: Completion fact on the execution recording who posted its review.
POSTED_BY_FACT = "platform_review_posted_by"
#: Completion fact recording which route admitted the run.
SOURCE_FACT = "platform_review_source"
ReviewBinding = CiReviewBinding | EventReviewBinding
#: Flow value to GitHub review event.
REVIEW_EVENTS: dict[str, str] = {
    "comment": "COMMENT",
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
}
#: Review state GitHub reports for each event once it is submitted.
_SUBMITTED_STATES: dict[str, str] = {
    "COMMENT": "COMMENTED",
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}
#: Commit status states a verdict may map to. ``pending`` is never posted.
STATUS_STATES: frozenset[str] = frozenset({"success", "failure", "error"})
#: States for a run that produced no valid verdict.
INCOMPLETE_STATES: frozenset[str] = frozenset({"error", "failure"})
DEFAULT_STATUS_CONTEXT = "preloop/review"
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/:-]{0,99}$")
_VERDICT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

#: Length caps of the rendered review.
MAX_BODY_CHARS = 60000
MAX_SUMMARY_CHARS = 4000
MAX_FINDINGS = 50
MAX_FINDING_TITLE_CHARS = 200
MAX_FINDING_BODY_CHARS = 2000
MAX_INLINE_COMMENTS = 20
MAX_CHECKED_ITEMS = 30
MAX_CHECKED_ITEM_CHARS = 200
#: Extra finding fields (everything except severity, title, the main text
#: and the location) rendered under the finding. A longer value is cut and
#: the finding says so; further fields are counted, not rendered.
MAX_EXTRA_FIELDS = 8
MAX_EXTRA_FIELD_CHARS = 2000
MAX_EXTRA_LABEL_CHARS = 80
#: Waits between read-backs when the object just written is not visible yet.
#: A few seconds in total. Tests replace :func:`_pause` and do not sleep.
READBACK_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)
#: Read-back checks that mean the object is not visible yet. Anything else
#: that fails is present and different, and stays failed.
READBACK_ABSENT: frozenset[str] = frozenset({"review_absent", "status_absent"})
#: Guide section for what a posted review shows of a finding.
REVIEW_FINDING_DOCS_URL = (
    "https://docs.preloop.ai/guide/flows/restricted-ci/#what-your-result-must-contain"
)
POSTING_SCOPE_DOCS_URL = (
    "https://docs.preloop.ai/guide/flows/restricted-ci/"
    "#post-for-some-of-a-shared-flows-projects"
)
#: Pages (of 100) read when listing the files or reviews of a pull request.
MAX_PAGES = 10
#: Automatic attempts before a failed publication waits for an explicit call.
MAX_AUTOMATIC_ATTEMPTS = 6
#: Recovery only reaches back this far for runs with no publication row.
UNCLAIMED_LOOKBACK = timedelta(hours=24)
_API = "https://api.github.com"
_ZWSP = "\u200b"
#: Characters that can start or delimit Markdown or HTML anywhere in a line.
#: Each is written as a numeric character reference, which GitHub renders as
#: the literal character and never treats as markup.
_MARKUP = frozenset("\\`*_[]<>&#@~|!$")
#: Characters that only mean something at the start of a line (lists, rules,
#: setext headings, quotes, slash commands).
_LINE_START = frozenset("-+=/:.)(%^{}'\",;?")
_CODE_SPAN = re.compile(r"`([^`\n]{1,200})`")

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "TIMEOUT",
        "TIMED_OUT",
        "ABORTED",
        "CANCELLED",
        "CANCELED",
    }
)


class PlatformReviewRefusedError(Exception):
    """An explicit publish request that cannot be attempted at all.

    Attributes:
        reason: Stable machine reason.
        status_code: HTTP status the API answers with.
    """

    def __init__(self, reason: str, status_code: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class PlatformReviewError(Exception):
    """One publication attempt failed; nothing else about the run changes.

    Attributes:
        reason: Stable machine reason stored on the publication row.
        retryable: Whether a later automatic attempt may succeed.
    """

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        check: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        #: HTTP status, when the failure came from the provider.
        self.status_code = status_code
        #: Which read-back comparison failed (``review_body``, ``status_absent``).
        self.check = check


# --- caller preconditions --------------------------------------------------

#: Request members of the explicit publish call, in the order they are checked.
PRECONDITION_FIELDS: tuple[str, ...] = (
    "expected_head_sha",
    "expected_base_sha",
    "expected_result_sha256",
    "expected_flow_revision",
    "expected_evidence",
)
#: How complete the stored tool evidence must be before a review is posted.
EVIDENCE_LEVELS: tuple[str, ...] = ("any", "captured", "complete")
#: Caller preconditions may only name the two levels that tighten posting.
EVIDENCE_PRECONDITIONS: tuple[str, ...] = ("captured", "complete")
_EVIDENCE_RANK = {"any": 0, "captured": 1, "complete": 2}
#: Streaming gateway evidence is written after the response body, so a
#: publication in this window can see a run whose last request has not
#: landed. Same deferral as result-capture delivery.
EVIDENCE_SETTLE_AFTER = timedelta(seconds=30)
_RESULT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class PreconditionMalformedError(ValueError):
    """A precondition value that is not well formed (HTTP 400).

    Attributes:
        field: The request member that is malformed.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class PreconditionFailedError(Exception):
    """One or more caller preconditions do not hold (HTTP 412).

    Attributes:
        failures: One entry per failed precondition, in
            :data:`PRECONDITION_FIELDS` order, each with ``precondition``,
            ``expected``, ``current`` and ``source``.
    """

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        super().__init__("review_precondition_failed")
        self.failures = failures


@dataclass(frozen=True)
class ReviewPreconditions:
    """What the caller validated; each member is optional."""

    head_sha: str | None = None
    base_sha: str | None = None
    result_sha256: str | None = None
    flow_revision: int | None = None
    evidence: str | None = None

    @property
    def any(self) -> bool:
        """Whether the caller supplied at least one precondition."""
        return any(
            value is not None
            for value in (
                self.head_sha,
                self.base_sha,
                self.result_sha256,
                self.flow_revision,
                self.evidence,
            )
        )


def parse_preconditions(body: Any) -> ReviewPreconditions:
    """Read the optional request body of the explicit publish call.

    Args:
        body: Decoded JSON body; None or ``{}`` means no preconditions.

    Returns:
        The preconditions. Commit SHAs are compared lowercase.

    Raises:
        PreconditionMalformedError: The body is not an object, names an
            unknown member, or a value has the wrong form.
    """
    if body is None:
        return ReviewPreconditions()
    if not isinstance(body, Mapping):
        raise PreconditionMalformedError("body", "The request body must be an object")
    unknown = sorted(str(key) for key in body if key not in PRECONDITION_FIELDS)
    if unknown:
        raise PreconditionMalformedError(
            unknown[0], f"Unknown request member: {unknown[0]}"
        )
    values: dict[str, Any] = {}
    for field in ("expected_head_sha", "expected_base_sha"):
        value = body.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value.lower()):
            raise PreconditionMalformedError(
                field, f"{field} must be a full 40 or 64 character commit SHA"
            )
        values[field] = value.lower()
    digest_value = body.get("expected_result_sha256")
    if digest_value is not None:
        if not isinstance(digest_value, str) or not _RESULT_DIGEST_RE.fullmatch(
            digest_value.lower()
        ):
            raise PreconditionMalformedError(
                "expected_result_sha256",
                "expected_result_sha256 must be 64 hexadecimal characters",
            )
        values["expected_result_sha256"] = digest_value.lower()
    revision = body.get("expected_flow_revision")
    if revision is not None:
        if type(revision) is not int or revision < 1:
            raise PreconditionMalformedError(
                "expected_flow_revision",
                "expected_flow_revision must be a positive integer",
            )
        values["expected_flow_revision"] = revision
    evidence = body.get("expected_evidence")
    if evidence is not None:
        if evidence not in EVIDENCE_PRECONDITIONS:
            raise PreconditionMalformedError(
                "expected_evidence",
                "expected_evidence must be captured or complete",
            )
        values["expected_evidence"] = evidence
    return ReviewPreconditions(
        head_sha=values.get("expected_head_sha"),
        base_sha=values.get("expected_base_sha"),
        result_sha256=values.get("expected_result_sha256"),
        flow_revision=values.get("expected_flow_revision"),
        evidence=values.get("expected_evidence"),
    )


def stored_result_sha256(execution: Any) -> str:
    """The published digest of an execution's stored result.

    The same value as ``completion.result_captured.result_sha256`` and the CI
    callback: SHA-256 over the canonical JSON of the restricted result
    projection. See :func:`preloop.services.review_completion_evidence.
    result_sha256` for the canonical form.
    """
    from preloop.services.review_completion_evidence import result_sha256

    return result_sha256(getattr(execution, "result", None))


def _failure(field: str, expected: Any, current: Any, source: str) -> dict[str, Any]:
    return {
        "precondition": field,
        "expected": expected,
        "current": current,
        "source": source,
    }


def _block_evidence_level(block: Mapping[str, Any]) -> str:
    """The evidence value of an already enabled posting block."""
    value = block.get("evidence", "any")
    if value is None or value not in EVIDENCE_LEVELS:
        return "any"
    return str(value)


def evidence_level(config: Any) -> str:
    """The flow's evidence gate: any, captured or complete.

    Args:
        config: The flow's ``git_clone_config``.

    Returns:
        ``any`` when posting is off, the key is absent, or the value is
        ``any``.
    """
    return _block_evidence_level(platform_review_block(config) or {})


def stricter_evidence(flow_level: str, caller_level: str | None) -> str:
    """The tighter of the flow setting and a caller precondition.

    A caller can require more evidence. A weaker precondition does not
    lower the flow setting.

    Args:
        flow_level: Stored ``platform_review.evidence``.
        caller_level: ``expected_evidence``, or None.

    Returns:
        The higher of the two ranks.
    """
    caller = caller_level if caller_level in _EVIDENCE_RANK else "any"
    if _EVIDENCE_RANK[flow_level] >= _EVIDENCE_RANK[caller]:
        return flow_level
    return caller


def evidence_met_level(summary: Mapping[str, Any] | None) -> str:
    """The strongest evidence level the stored summary meets.

    Args:
        summary: :func:`tool_evidence_summary` output.

    Returns:
        ``complete``, ``captured`` or ``any`` (does not meet ``captured``).
    """
    if evidence_satisfied(summary, "complete"):
        return "complete"
    if evidence_satisfied(summary, "captured"):
        return "captured"
    return "any"


def evidence_satisfied(summary: Mapping[str, Any] | None, level: str) -> bool:
    """Whether stored counts meet ``level``.

    ``captured`` allows records that are incomplete only because a named
    masking rule fired. ``complete`` allows no incomplete record. ``any``
    does not look.

    Args:
        summary: Counts from stored evidence. None does not meet a gate.
        level: ``any``, ``captured`` or ``complete``.

    Returns:
        Whether the level is met.
    """
    if level == "any":
        return True
    if not isinstance(summary, Mapping) or level not in _EVIDENCE_RANK:
        return False
    if summary.get("missing_records"):
        return False
    incomplete = summary.get("incomplete_records")
    masking = summary.get("masking_only_records")
    if type(incomplete) is not int or type(masking) is not int:
        return False
    if level == "complete":
        return incomplete == 0
    return incomplete == masking


def stored_tool_evidence(db: Session, execution: Any) -> dict[str, Any]:
    """The public tool-evidence counts for one execution.

    Args:
        db: Database session.
        execution: Execution whose records are counted.

    Returns:
        Counts with no observation values, paths or text.
    """
    from preloop.models.crud.execution_tool_evidence import tool_evidence_summary

    return tool_evidence_summary(db, execution_id=execution.id)


def evidence_may_still_arrive(execution: Any) -> bool:
    """Whether the last gateway request can still be written.

    Streaming usage and its tool evidence are committed after the response
    body, in the same transaction, and that write can outlast the moment
    the execution is marked terminal (the record waits up to the gateway
    pool timeout). A publication inside :data:`EVIDENCE_SETTLE_AFTER` of
    ``end_time`` therefore waits, the way result delivery waits while
    artifact capture is pending, instead of posting or refusing on a
    partial record.

    Args:
        execution: A terminal execution.

    Returns:
        True when ``end_time`` is within the settle window.
    """
    due = evidence_settled_at(execution)
    if due is None:
        return False
    return datetime.now(timezone.utc).replace(tzinfo=None) < due


def evidence_settled_at(execution: Any) -> datetime | None:
    """When the settle window after ``end_time`` closes, as naive UTC.

    Args:
        execution: An execution.

    Returns:
        ``end_time`` plus :data:`EVIDENCE_SETTLE_AFTER`, or None without an
        end time.
    """
    end = getattr(execution, "end_time", None)
    if not isinstance(end, datetime):
        return None
    # Timestamp columns are naive UTC. An aware value is converted; a naive
    # one is already that convention, including after a round trip.
    if end.tzinfo is not None:
        end = end.astimezone(timezone.utc).replace(tzinfo=None)
    return end + EVIDENCE_SETTLE_AFTER


#: A row-less pending publication whose run is still running.
PENDING_EXECUTION_NOT_FINISHED = "execution_not_finished"
#: A row-less pending publication waiting out :data:`EVIDENCE_SETTLE_AFTER`.
PENDING_EVIDENCE_SETTLING = "evidence_settling"
#: The settle deadline passed; the monitor remains the restart safety net.
PENDING_AWAITING_SWEEP = "awaiting_sweep"
#: A finished manual-mode run needs an explicit publish call.
PENDING_MANUAL_PUBLISH = "manual_publish_required"

# Keep sleeping tasks alive, and coalesce repeated completion notifications.
_settle_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}


def _schedule_settled_publication(
    *, account_id: Any, execution_id: Any, due: datetime
) -> None:
    """Wake a deferred automatic publication without holding a DB session.

    A lost timer is harmless: the committed terminal execution is selected
    by the sweep. Every caller still uses the same exclusive publication claim.

    Args:
        account_id: Account that owns the execution.
        execution_id: Finished execution to attempt at the deadline.
        due: End of the evidence settle window, as naive UTC.
    """
    key = (str(account_id), str(execution_id))
    if key in _settle_tasks:
        return

    async def publish_when_settled() -> None:
        """Wait out the full window and enter the normal completion publisher."""
        try:
            # Recheck UTC after waking, including if the wall clock moved back.
            while (
                delay := (
                    due - datetime.now(timezone.utc).replace(tzinfo=None)
                ).total_seconds()
            ) > 0:
                await asyncio.sleep(delay)
            await publish_finished_execution(
                account_id=account_id, execution_id=execution_id
            )
        except Exception:
            # The sweep retries the durable execution after a worker/I/O failure.
            logger.error("Deferred platform review failed for %s", execution_id)

    def finished(task: asyncio.Task[None]) -> None:
        """Release the timer reference after completion or cancellation."""
        if _settle_tasks.get(key) is task:
            del _settle_tasks[key]

    task = asyncio.create_task(publish_when_settled())
    _settle_tasks[key] = task
    task.add_done_callback(finished)


def _automatic_recovery_available(execution: Any, flow: Any) -> bool:
    """Whether the unclaimed-run sweep can still pick up this execution.

    Mirror the sweep's opt-in cutoff and lookback without reading evidence.
    An excluded run can still be published through the explicit publish call.

    Args:
        execution: Finished execution with no publication row.
        flow: Its opted-in flow.

    Returns:
        Whether the execution is within the sweep's opt-in cutoff and lookback.
    """
    settled = evidence_settled_at(execution)
    if settled is None:
        return False
    end = settled - EVIDENCE_SETTLE_AFTER
    block = platform_review_block(getattr(flow, "git_clone_config", None)) or {}
    stamp = block.get("enabled_at")
    if stamp is not None:
        try:
            since = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return False
    else:
        since = getattr(flow, "updated_at", None)
    if not isinstance(since, datetime):
        return False
    if since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return end >= since and end >= now - UNCLAIMED_LOOKBACK


def _pending_cause(execution: Any, flow: Any) -> tuple[str, str | None]:
    """Why a publication with no attempt yet is pending, and until when.

    Args:
        execution: The execution, with no publication row.
        flow: Its flow.

    Returns:
        ``(reason, due_at)``. The settle deadline while evidence can arrive;
        afterwards the fallback sweep deadline. A past deadline stays past
        so an overdue sweep is visible. Manual mode has no automatic deadline.
    """
    if getattr(execution, "status", None) not in TERMINAL_STATUSES:
        return PENDING_EXECUTION_NOT_FINISHED, None
    config = getattr(flow, "git_clone_config", None)
    if evidence_level(config) != "any" and evidence_may_still_arrive(execution):
        return PENDING_EVIDENCE_SETTLING, _iso(evidence_settled_at(execution))
    if (
        parse_stored_review_binding(getattr(execution, "ci_review_binding", None))
        is None
    ):
        return "no_review_binding", None
    if platform_review_mode(config) == MODE_MANUAL:
        return PENDING_MANUAL_PUBLISH, None
    if not _automatic_recovery_available(execution, flow):
        return "explicit_publish_required", None
    from preloop.services.execution_monitor import publication_sweep_interval

    settled = evidence_settled_at(execution)
    return PENDING_AWAITING_SWEEP, _iso(
        settled + timedelta(seconds=publication_sweep_interval())
        if settled is not None
        else None
    )


def check_stored_preconditions(
    pre: ReviewPreconditions,
    *,
    flow: Any,
    execution: Any,
    binding: ReviewBinding,
    evidence: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compare the preconditions with what Preloop stores.

    Args:
        pre: Caller preconditions.
        flow: The execution's flow, as it will be read for posting.
        execution: The execution, as it will be read for posting.
        binding: The execution's review binding.
        evidence: Stored tool-evidence counts, required when the caller
            set ``expected_evidence``.

    Returns:
        Failures, empty when every supplied precondition holds.
    """
    failures: list[dict[str, Any]] = []
    if pre.head_sha is not None and pre.head_sha != binding.head_sha.lower():
        failures.append(
            _failure("expected_head_sha", pre.head_sha, binding.head_sha, "binding")
        )
    if pre.base_sha is not None:
        bound_base = (binding.base_sha or "").lower() or None
        if bound_base is not None and pre.base_sha != bound_base:
            failures.append(
                _failure("expected_base_sha", pre.base_sha, bound_base, "binding")
            )
    if pre.result_sha256 is not None:
        current = stored_result_sha256(execution)
        if pre.result_sha256 != current:
            failures.append(
                _failure("expected_result_sha256", pre.result_sha256, current, "result")
            )
    if pre.flow_revision is not None:
        current_revision = getattr(flow, "config_revision", None)
        if pre.flow_revision != current_revision:
            failures.append(
                _failure(
                    "expected_flow_revision",
                    pre.flow_revision,
                    current_revision,
                    "flow",
                )
            )
    if pre.evidence is not None:
        # The flow setting is not applied here. A weaker precondition must
        # not lower it; the publication plan enforces the stored level.
        current = evidence_met_level(evidence)
        if _EVIDENCE_RANK[current] < _EVIDENCE_RANK[pre.evidence]:
            failures.append(
                _failure("expected_evidence", pre.evidence, current, "tool_evidence")
            )
    return failures


def check_live_preconditions(
    pre: ReviewPreconditions, pull: Any
) -> list[dict[str, Any]]:
    """Compare the commit preconditions with the live pull request.

    Args:
        pre: Caller preconditions.
        pull: The pull request as the provider returns it now.

    Returns:
        Failures, empty when every supplied commit precondition holds.
    """
    failures: list[dict[str, Any]] = []
    head = pull.get("head") if isinstance(pull, Mapping) else None
    base = pull.get("base") if isinstance(pull, Mapping) else None
    live_head = head.get("sha") if isinstance(head, Mapping) else None
    live_base = base.get("sha") if isinstance(base, Mapping) else None
    if pre.head_sha is not None and (
        not isinstance(live_head, str) or live_head.lower() != pre.head_sha
    ):
        failures.append(_failure("expected_head_sha", pre.head_sha, live_head, "live"))
    if pre.base_sha is not None and (
        not isinstance(live_base, str) or live_base.lower() != pre.base_sha
    ):
        failures.append(_failure("expected_base_sha", pre.base_sha, live_base, "live"))
    return failures


# --- configuration ---------------------------------------------------------


def platform_review_block(config: Any) -> dict[str, Any] | None:
    """The enabled opt-in block of a flow's ``git_clone_config``, or None."""
    block = config.get(CONFIG_KEY) if isinstance(config, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    return dict(block)


def platform_review_mode(config: Any) -> str:
    """When Preloop posts the review of a flow: off, automatic or manual.

    Stored shape (``git_clone_config.platform_review``): ``enabled: false`` or
    no block is off; ``enabled: true`` is automatic unless ``mode`` is
    ``"manual"``. Blocks saved before the mode existed have no ``mode`` and
    keep meaning automatic.

    Args:
        config: The flow's ``git_clone_config``.

    Returns:
        One of :data:`MODE_OFF`, :data:`MODE_AUTOMATIC`, :data:`MODE_MANUAL`.
    """
    block = platform_review_block(config)
    if block is None:
        return MODE_OFF
    return MODE_MANUAL if block.get("mode") == MODE_MANUAL else MODE_AUTOMATIC


#: Reason for a run whose project is outside ``platform_review.project_ids``.
OUT_OF_SCOPE_REASON = "project_not_in_posting_scope"


def posting_project_ids(config: Any) -> list[str] | None:
    """The projects an enabled flow posts for, or None for all of them.

    ``platform_review.project_ids`` absent or null means every project of
    the flow. A stored value that is not a well-formed list posts for none:
    when in doubt nothing is posted.

    Args:
        config: The flow's ``git_clone_config``.

    Returns:
        Canonical project id strings, or None when posting is not limited.
    """
    from preloop.models.schemas.flow import normalize_posting_project_ids

    block = platform_review_block(config)
    if block is None or block.get("project_ids") is None:
        return None
    try:
        return normalize_posting_project_ids(block["project_ids"])
    except ValueError:
        return []


def project_in_posting_scope(config: Any, project_id: Any) -> bool:
    """Whether Preloop posts for ``project_id`` on this flow.

    Membership in the flow's ``trigger_project_ids`` is checked separately
    by each caller; this is only the posting list.

    Args:
        config: The flow's ``git_clone_config``.
        project_id: Project of the run's stored binding.

    Returns:
        True when posting is not limited or the project is listed.
    """
    scope = posting_project_ids(config)
    if scope is None:
        return True
    try:
        return str(UUID(str(project_id))) in scope
    except ValueError:
        return False


def validate_platform_review_projects(config: Any, trigger_project_ids: Any) -> None:
    """Refuse a posting list that names a project the flow does not admit.

    Args:
        config: ``git_clone_config`` the flow will have after the save.
        trigger_project_ids: The flow's ``trigger_project_ids`` after the save.

    Raises:
        ValueError: An id is not in ``trigger_project_ids``. The message
            names the key and the id.
    """
    from preloop.models.schemas.flow import POSTING_PROJECT_IDS_KEY

    block = config.get(CONFIG_KEY) if isinstance(config, Mapping) else None
    if not isinstance(block, Mapping) or block.get("project_ids") is None:
        return
    scope = posting_project_ids({CONFIG_KEY: {**block, "enabled": True}}) or []
    admitted: set[str] = set()
    for item in trigger_project_ids or []:
        try:
            admitted.add(str(UUID(str(item))))
        except ValueError:
            continue
    for project_id in scope:
        if project_id not in admitted:
            raise ValueError(
                f"{POSTING_PROJECT_IDS_KEY}: {project_id} is not one of the "
                "flow's trigger_project_ids"
            )


def _status_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    status = block.get("status")
    return dict(status) if isinstance(status, Mapping) else None


def validate_platform_review_block(block: Any) -> None:
    """Check the shape of a stored ``platform_review`` block.

    The API schema already validates the block on save; this is the same
    check for configuration read back from storage, where nothing guarantees
    the shape.

    Args:
        block: Value of ``git_clone_config.platform_review`` (None is valid).

    Raises:
        ValueError: The block is malformed. Nothing is posted for such a flow.
    """
    if block is None:
        return
    if not isinstance(block, Mapping) or not isinstance(block.get("enabled"), bool):
        raise ValueError("platform_review must be an object with a boolean enabled")
    unknown = set(block) - {
        "enabled",
        "mode",
        "action",
        "verdict_actions",
        "status",
        "enabled_at",
        "evidence",
        "project_ids",
    }
    if unknown:
        raise ValueError("platform_review has unsupported keys")
    if block.get("project_ids") is not None:
        from preloop.models.schemas.flow import (
            POSTING_PROJECT_IDS_KEY,
            normalize_posting_project_ids,
        )

        normalize_posting_project_ids(block["project_ids"])
        if block.get("enabled") is not True:
            raise ValueError(
                f"{POSTING_PROJECT_IDS_KEY} is only valid while posting is enabled"
            )
    if block.get("mode", MODE_AUTOMATIC) not in STORED_MODES:
        raise ValueError("platform_review.mode must be automatic or manual")
    # A dumped block carries evidence: null when the key was omitted. That is
    # the same as any, not a stored gate.
    evidence = block.get("evidence", "any")
    if evidence is None:
        evidence = "any"
    if evidence not in EVIDENCE_LEVELS:
        raise ValueError("platform_review.evidence must be any, captured or complete")
    if block.get("action", "comment") != "comment":
        raise ValueError(
            "platform_review.action must be comment; other review actions "
            "need an explicit verdict_actions mapping"
        )
    actions = block.get("verdict_actions")
    if actions is not None and (
        not isinstance(actions, Mapping)
        or any(
            not isinstance(verdict, str)
            or not _VERDICT_RE.fullmatch(verdict)
            or action not in REVIEW_EVENTS
            for verdict, action in actions.items()
        )
    ):
        raise ValueError(
            "platform_review.verdict_actions maps verdicts to comment, "
            "approve or request_changes"
        )
    status = block.get("status")
    if status is None:
        return
    if not isinstance(status, Mapping) or set(status) - {
        "context",
        "verdict_states",
        "incomplete_state",
    }:
        raise ValueError("platform_review.status has unsupported keys")
    context = status.get("context", DEFAULT_STATUS_CONTEXT)
    if not isinstance(context, str) or not _CONTEXT_RE.fullmatch(context):
        raise ValueError("platform_review.status.context is not a valid context name")
    states = status.get("verdict_states")
    if (
        not isinstance(states, Mapping)
        or not states
        or any(
            not isinstance(verdict, str)
            or not _VERDICT_RE.fullmatch(verdict)
            or state not in STATUS_STATES
            for verdict, state in states.items()
        )
    ):
        raise ValueError(
            "platform_review.status.verdict_states maps every verdict to "
            "success, failure or error"
        )
    incomplete = status.get("incomplete_state")
    if incomplete is not None and incomplete not in INCOMPLETE_STATES:
        raise ValueError(
            "platform_review.status.incomplete_state must be error or failure"
        )


def stamp_enabled_at(config: Any, previous: Any) -> None:
    """Record, on a flow being saved, when posting was turned on.

    The stamp bounds the automatic sweep: a run that finished before posting
    was enabled is not published by it. It is server-owned: a value in the
    submitted block is replaced, an already enabled flow keeps its stamp
    across unrelated saves, and turning posting off clears it.

    Args:
        config: The submitted ``GitCloneConfig`` (mutated in place), or None.
        previous: The stored ``git_clone_config`` of the flow, if any.
    """
    block = getattr(config, "platform_review", None)
    if block is None:
        return
    if block.enabled is not True:
        block.enabled_at = None
        return
    before = platform_review_block(previous) or {}
    kept = before.get("enabled_at")
    block.enabled_at = (
        kept
        if isinstance(kept, str) and kept
        else datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    )


def verdict_enum(schema: Any) -> list[str] | None:
    """The closed list of verdicts a flow's result schema admits.

    Args:
        schema: The flow's ``result_schema``.

    Returns:
        The enum of the required top-level string property ``verdict``, or
        None when the schema does not declare one.
    """
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    required = schema.get("required")
    verdict = properties.get("verdict") if isinstance(properties, Mapping) else None
    if (
        not isinstance(verdict, Mapping)
        or verdict.get("type") != "string"
        or not isinstance(required, list)
        or "verdict" not in required
    ):
        return None
    values = verdict.get("enum")
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(v, str) or not _VERDICT_RE.fullmatch(v) for v in values)
    ):
        return None
    return list(values)


def validate_platform_review_flow(
    flow: Any, *, enforce_content_capture: bool = False
) -> None:
    """Require a validated result with a closed, fully mapped verdict.

    Args:
        flow: Flow row or namespace.
        enforce_content_capture: When True (the save path), reject
            ``captured`` or ``complete`` while model-gateway content capture
            is off. Publish does not pass this: a record stored with content
            capture off already fails the gate, and turning the instance flag
            off later must not refuse a run whose records were captured
            while it was on.

    Raises:
        ValueError: The flow opts in but its result is not validated, its
            schema has no closed ``verdict`` enum, or a verdict has no
            explicit status state.
    """
    config = getattr(flow, "git_clone_config", None)
    validate_platform_review_block(
        config.get(CONFIG_KEY) if isinstance(config, Mapping) else None
    )
    block = platform_review_block(config)
    if block is None or not isinstance(config, Mapping):
        return
    if enforce_content_capture and block.get("evidence") in {"captured", "complete"}:
        from preloop.config import settings

        # Content capture is the instance setting that writes
        # content_capture_enabled on each record. With it off, gateway
        # observations store no values, so captured and complete cannot pass
        # for a run that called the model. Rejecting the save matches the
        # other posting checks: a gate that cannot succeed is not stored.
        if not settings.model_gateway_capture_content:
            raise ValueError(
                "platform_review.evidence requires content capture to be on "
                "(MODEL_GATEWAY_CAPTURE_CONTENT); without it the gate cannot pass"
            )
    if config.get("publication_mode") != "read_only":
        raise ValueError("platform_review requires read_only publication mode")
    schema = getattr(flow, "result_schema", None)
    if (
        not isinstance(schema, Mapping)
        or not schema
        or getattr(flow, "validate_result", True) is not True
    ):
        raise ValueError(
            "platform_review requires a result_schema with validate_result on: "
            "only a validated result is posted"
        )
    verdicts = verdict_enum(schema)
    if verdicts is None:
        raise ValueError(
            "platform_review requires result_schema to declare a required "
            "string property verdict with an enum"
        )
    status = _status_block(block)
    if status is not None:
        missing = [v for v in verdicts if v not in status.get("verdict_states", {})]
        if missing:
            raise ValueError(
                "platform_review.status.verdict_states has no state for "
                f"verdict {missing[0]!r}"
            )
    unknown = [v for v in (block.get("verdict_actions") or {}) if v not in verdicts]
    if unknown:
        raise ValueError(
            f"platform_review.verdict_actions names verdict {unknown[0]!r}, "
            "which result_schema does not admit"
        )


def validate_platform_review_trackers(
    db: Session, *, account_id: Any, config: Any
) -> None:
    """Reject the opt-in at save time unless every repository is a GitHub App.

    Args:
        db: Database session.
        account_id: Account saving the flow.
        config: The flow's effective ``git_clone_config``.

    Raises:
        ValueError: A repository's tracker is missing, is not GitHub, or is
            not a GitHub App installation (for example a personal token).
    """
    if platform_review_block(config) is None:
        return
    repositories = config.get("repositories") or []
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("platform_review requires an explicit repository")
    for repository in repositories:
        tracker_id = (
            repository.get("tracker_id") if isinstance(repository, Mapping) else None
        )
        tracker = (
            crud.crud_tracker.get_by_id_and_account(
                db, id=str(tracker_id), account_id=str(account_id)
            )
            if tracker_id
            else None
        )
        if tracker is None:
            raise ValueError(
                "platform_review requires each repository's own GitHub App tracker"
            )
        try:
            validate_publication_tracker(tracker)
        except PublicationError:
            raise ValueError(
                "platform_review is available for GitHub App installations "
                "only; a personal access token cannot be limited to one "
                "repository and two permissions"
            ) from None


# --- rendering -------------------------------------------------------------


def _reference(char: str) -> str:
    return f"&#{ord(char)};"


def _encode_text(text: str) -> str:
    """Encode one run of text that is not inside a code span."""
    out: list[str] = []
    for index, char in enumerate(text):
        if char in _MARKUP:
            out.append(_reference(char))
            if char in "@#":
                out.append(_ZWSP)
        elif char == ":" and text[index + 1 : index + 3] == "//":
            out.append(":" + _ZWSP)
        else:
            out.append(char)
    encoded = "".join(out)
    encoded = re.sub(r"(?i)(www)(\.)", rf"\1{_ZWSP}\2", encoded)
    return re.sub(r"(?i)\b(GH-)(?=\d)", rf"\1{_ZWSP}", encoded)


def _encode_line(line: str, code: bool) -> str:
    """Encode one line, keeping well-formed inline code spans when allowed."""
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(line) if code else ():
        inner = match.group(1).strip()
        if not inner:
            continue
        parts.append(_encode_text(line[position : match.start()]))
        parts.append(f"`{inner}`")
        position = match.end()
    parts.append(_encode_text(line[position:]))
    encoded = "".join(parts)
    # Nothing model-written may open a block: a list, a rule, a heading
    # underline, an ordered list, a slash command.
    if encoded[:1] in _LINE_START:
        return _reference(encoded[0]) + encoded[1:]
    ordered = re.match(r"^(\d{1,9})([.)])", encoded)
    if ordered:
        return (
            ordered.group(1) + _reference(ordered.group(2)) + encoded[ordered.end() :]
        )
    return encoded


def plain_text(
    value: Any, limit: int, *, single_line: bool = False, code: bool = True
) -> str:
    """Render untrusted text so GitHub shows it as literal characters.

    The rule, in order:

    1. Anything that is not a string becomes the empty string.
    2. Control, format, surrogate, private-use and unassigned characters are
       dropped (this removes bidirectional overrides and zero-width
       characters); line breaks are kept, or turned into spaces for a
       single-line field.
    3. The text is cut to ``limit`` characters.
    4. A well-formed inline code span (one pair of backticks on one line, no
       backtick inside) is kept as a code span, so identifiers and file names
       the model marked as code read as code. GitHub interprets nothing
       inside a code span.
    5. Everywhere else, every character that can start or delimit Markdown or
       HTML (``\\ ` * _ [ ] < > & # @ ~ | ! $``) is written as a numeric
       character reference. GitHub shows the character and never treats a
       reference as markup, so no tag, comment, link, image, emphasis, code,
       table, strike-through or math survives, and ordinary text, dots,
       dashes and parentheses stay as typed.
    6. A character that only matters at the start of a line (list markers,
       rules, heading underlines, ordered-list numbers, a leading ``/``) is
       written as a reference when it is first on its line.
    7. A zero-width space is inserted after ``@`` and ``#``, between a URL
       scheme's ``:`` and ``//``, inside ``www.`` and inside ``GH-<digit>``,
       so no mention, issue or pull request reference, closing keyword
       target or bare-URL autolink is recognised.

    Args:
        value: Model-written value.
        limit: Maximum number of source characters kept.
        single_line: Replace line breaks with spaces.
        code: Keep well-formed inline code spans (step 4).

    Returns:
        Markdown source that renders as the literal text.
    """
    if not isinstance(value, str):
        return ""
    kept: list[str] = []
    for char in value.replace("\r\n", "\n").replace("\r", "\n"):
        if char in "\n\t":
            kept.append(" " if single_line or char == "\t" else "\n")
        elif unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"} and (
            char not in "\u2028\u2029"
        ):
            kept.append(char)
    text = "".join(kept).strip()
    if len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "\u2026"
    return "\n".join(_encode_line(line.strip(), code) for line in text.split("\n"))


def code_span(value: Any, limit: int) -> str:
    """Render a file path or identifier as an inline code span.

    GitHub interprets nothing inside a code span, so the value is shown
    exactly. A value that could end the span early (a backtick) or is not a
    single line is rendered through :func:`plain_text` instead.

    Args:
        value: Model-written path or identifier.
        limit: Maximum number of characters kept.

    Returns:
        Markdown source, or the empty string for an unusable value.
    """
    if not isinstance(value, str):
        return ""
    text = "".join(
        char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
    ).strip()
    if not text:
        return ""
    if "`" in text or len(text) > limit:
        return plain_text(value, limit, single_line=True, code=False)
    return f"`{text}`"


def _quote(text: str) -> str:
    """Emit escaped multi-line text as a block quote, never at column zero."""
    lines = [line.strip() for line in text.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(f"> {line}" if line else ">" for line in collapsed)


@dataclass(frozen=True)
class Finding:
    """One structured finding of a validated result, already escaped."""

    severity: str
    title: str
    body: str
    #: True when the main text was cut to :data:`MAX_FINDING_BODY_CHARS`.
    body_shortened: bool
    path: str | None
    raw_path: str | None
    line: int | None
    #: Labelled blocks for every other member, including the cut notes.
    details: str


#: Finding members, in the order a value is chosen when several are present.
_SEVERITY_FIELDS: tuple[str, ...] = ("severity",)
_TITLE_FIELDS: tuple[str, ...] = ("title",)
_TEXT_FIELDS: tuple[str, ...] = (
    "body",
    "description",
    "message",
    "detail",
    "problem",
    "issue",
    "finding",
    "summary",
)
_LOCATION_FIELDS: tuple[str, ...] = ("path", "file", "line", "location", "where")
_LOCATION_HASH = re.compile(r"^(.+)#L([0-9]+)$")
_LOCATION_RANGE = re.compile(r"^(.+):([0-9]+)-([0-9]+)$")
_LOCATION_LINE = re.compile(r"^(.+):([0-9]+)$")


def _first_text(source: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(source.get(name), str) and source[name].strip():
            return source[name]
    return None


def _first_name(source: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    """The first of ``names`` that holds a non-empty string."""
    for name in names:
        if isinstance(source.get(name), str) and source[name].strip():
            return name
    return None


def _parse_line(value: Any) -> int | None:
    """A line number: an int, or a decimal string, inside the accepted range.

    ``True`` is rejected (it is an ``int`` subclass). A string counts only
    when it is all digits and the value is in ``1 .. 10**7 - 1``.
    """
    if type(value) is int:
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal() or len(text) > 7:
            return None
        number = int(text)
    else:
        return None
    if type(number) is bool or not 0 < number < 10**7:
        return None
    return number


def _parse_location_string(
    value: str,
) -> tuple[str, int | None, int | None] | None:
    """Parse ``path``, ``path:LINE``, ``path:LINE-LINE`` or ``path#LLINE``.

    A range keeps both lines. The range pattern is greedy, so a
    comma-separated value matches on its last ``:START-END`` and the
    earlier locations stay in the path. The end line is returned so the
    posted text keeps that last range instead of rebuilding ``path:line``.

    Returns:
        The path, the first line, and the end line when the value is a
        validated range. None when the string is not one of those forms,
        or when a range's ends are not positive line numbers with the end
        on or after the start. The caller shows an unparsed value as
        literal code.
    """
    text = value.strip()
    if not text or "\n" in value or "\r" in value:
        return None
    ranged = _LOCATION_RANGE.fullmatch(text)
    if ranged is not None:
        path = ranged.group(1).strip()
        start = _parse_line(ranged.group(2))
        end = _parse_line(ranged.group(3))
        if not path or start is None or end is None or end < start:
            return None
        return path, start, end
    for pattern in (_LOCATION_HASH, _LOCATION_LINE):
        match = pattern.fullmatch(text)
        if match is None:
            continue
        path = match.group(1).strip()
        line = _parse_line(match.group(2))
        if not path or line is None:
            return None
        return path, line, None
    if ":" in text or "#" in text:
        return None
    return text, None, None


def _location(path: Any, line: Any, end: Any = None) -> str:
    """``path:line`` or ``path:start-end`` as a code span.

    The line is included only when it is a valid line number. A range is
    included only when both ends are valid and the end is on or after the
    start. Anything else is the path alone, still inside the same span.
    """
    if not isinstance(path, str):
        return ""
    start = _parse_line(line)
    finish = _parse_line(end) if end is not None else None
    if start is not None and type(line) is int:
        if finish is not None and type(end) is int and finish >= start:
            shown = f"{path.strip()}:{start}-{finish}"
        else:
            shown = f"{path.strip()}:{line}"
    else:
        shown = path
    return code_span(shown, 320)


def _finding_field_role(name: str) -> str:
    """How a declared finding field is shown: severity, title, text, location
    or a labelled block.
    """
    if name in _SEVERITY_FIELDS:
        return "severity"
    if name in _TITLE_FIELDS:
        return "title"
    if name in _TEXT_FIELDS:
        return "text"
    if name in _LOCATION_FIELDS:
        return "location"
    return "labelled block"


def _object_schema(schema: Any) -> Mapping[str, Any] | None:
    """An object schema, following one ``allOf`` / ``anyOf`` / ``oneOf``."""
    if not isinstance(schema, Mapping):
        return None
    if isinstance(schema.get("properties"), Mapping):
        return schema
    for key in ("allOf", "anyOf", "oneOf"):
        options = schema.get(key)
        if not isinstance(options, list):
            continue
        for option in options:
            found = _object_schema(option)
            if found is not None:
                return found
    return None


def _findings_item_schema(schema: Any) -> Mapping[str, Any] | None:
    """The object schema of ``findings.items``, when the result declares one."""
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    findings = properties.get("findings") if isinstance(properties, Mapping) else None
    if not isinstance(findings, Mapping):
        return None
    items = findings.get("items")
    if isinstance(items, list):
        for candidate in items:
            found = _object_schema(candidate)
            if found is not None:
                return found
        return None
    return _object_schema(items)


def finding_property_names(schema: Any) -> list[str]:
    """Finding field names in the order the result schema declares them.

    Args:
        schema: The flow's ``result_schema``.

    Returns:
        Property names of the findings item, or an empty list when the
        schema does not declare an object of finding fields.
    """
    item = _findings_item_schema(schema)
    if item is None:
        return []
    props = item.get("properties")
    if not isinstance(props, Mapping):
        return []
    return [name for name in props if isinstance(name, str)]


def finding_field_appearances(schema: Any) -> list[dict[str, str]]:
    """How each declared finding field appears in the posted review.

    Args:
        schema: The flow's ``result_schema``.

    Returns:
        ``{"field", "appears_as"}`` in schema order. ``appears_as`` is
        ``severity``, ``title``, ``text``, ``location`` or ``labelled block``.
    """
    return [
        {"field": name, "appears_as": _finding_field_role(name)}
        for name in finding_property_names(schema)
    ]


def _posting_scope_notes(flow: Any) -> list[dict[str, Any]]:
    """Say that posting is limited to N of the flow's M projects."""
    config = getattr(flow, "git_clone_config", None)
    scope = posting_project_ids(config)
    if scope is None:
        return []
    admitted: list[str] = []
    for item in getattr(flow, "trigger_project_ids", None) or []:
        try:
            admitted.append(str(UUID(str(item))))
        except ValueError:
            continue
    listed = len([project for project in scope if project in admitted])
    return [
        {
            "code": "review_posting_project_scope",
            "message": (
                f"Posting is limited to {listed} of {len(admitted)} projects "
                "of this flow (platform_review.project_ids). "
                "A run of another project is not posted, and a pull request "
                "event for one starts no review."
            ),
            "docs_url": POSTING_SCOPE_DOCS_URL,
        }
    ]


def review_posting_warnings(flow: Any) -> list[dict[str, Any]]:
    """Save-time description of how finding fields are posted.

    Information only. A flow that declares findings and no text field is
    warned; the save is not refused.

    Args:
        flow: Flow row or namespace.

    Returns:
        Warnings with ``code``, ``message`` and ``docs_url``: one when
        posting is limited to some of the flow's projects
        (``review_posting_project_scope``), and zero or one about the
        finding fields.
    """
    config = getattr(flow, "git_clone_config", None)
    if platform_review_block(config) is None:
        return []
    notes = _posting_scope_notes(flow)
    schema = getattr(flow, "result_schema", None)
    if _findings_item_schema(schema) is None:
        return notes
    appearances = finding_field_appearances(schema)
    listed = (
        ", ".join(f"{item['field']} ({item['appears_as']})" for item in appearances)
        or "none"
    )
    has_text = any(item["appears_as"] == "text" for item in appearances)
    if has_text:
        return notes + [
            {
                "code": "review_finding_fields",
                "message": f"A posted review shows these finding fields: {listed}.",
                "docs_url": REVIEW_FINDING_DOCS_URL,
            }
        ]
    return notes + [
        {
            "code": "review_finding_text_missing",
            "message": (
                "A posted review shows these finding fields: "
                f"{listed}. The schema declares no text field for a finding "
                "(body, description, message, detail, problem, issue, finding "
                "or summary), so the review has no problem statement for it."
            ),
            "docs_url": REVIEW_FINDING_DOCS_URL,
        }
    ]


def _limited_plain(
    value: Any, limit: int, *, single_line: bool = False, code: bool = True
) -> tuple[str, bool]:
    """:func:`plain_text` plus whether the cap cut the value."""
    shown = plain_text(value, limit, single_line=single_line, code=code)
    if not isinstance(value, str) or len(value) <= limit:
        return shown, False
    full = plain_text(value, len(value), single_line=single_line, code=code)
    return shown, full != shown


def _extra_keys(
    item: Mapping[str, Any], used: set[str], schema_names: list[str]
) -> list[str]:
    """Members not used for severity, title, text or location.

    Schema order first, then any key the document carries that the schema
    does not name.
    """
    ordered: list[str] = []
    for name in schema_names:
        if name in item and name not in used and name not in ordered:
            ordered.append(name)
    for name in item:
        if name not in used and name not in ordered:
            ordered.append(name)
    return ordered


def _render_extra_value(value: Any) -> tuple[str, bool] | None:
    """Literal text for a string, number, boolean or list of strings.

    Returns:
        ``(markdown, shortened)``, or None when the value is counted instead
        of rendered.
    """
    if isinstance(value, str):
        shown, shortened = _limited_plain(value, MAX_EXTRA_FIELD_CHARS)
        return (_quote(shown) if shown else ""), shortened
    if isinstance(value, bool):
        return _quote("true" if value else "false"), False
    if isinstance(value, int):
        return _quote(str(value)), False
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        shown, shortened = _limited_plain(str(value), 40, single_line=True)
        return (_quote(shown) if shown else ""), shortened
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        lines: list[str] = []
        shortened = False
        used = 0
        for entry in value:
            line, cut = _limited_plain(entry, MAX_CHECKED_ITEM_CHARS, single_line=True)
            if cut:
                shortened = True
            if not line:
                continue
            piece = f"- {line}"
            if used + len(piece) + 1 > MAX_EXTRA_FIELD_CHARS:
                shortened = True
                break
            lines.append(piece)
            used += len(piece) + 1
        if len(lines) < len([entry for entry in value if entry.strip()]):
            shortened = True
        return "\n".join(lines), shortened
    return None


def _extra_details(
    item: Mapping[str, Any], used: set[str], schema_names: list[str]
) -> str:
    """Labelled blocks for the finding members that were not otherwise used."""
    blocks: list[str] = []
    shown = 0
    hidden = 0
    for name in _extra_keys(item, used, schema_names):
        if shown >= MAX_EXTRA_FIELDS:
            hidden += 1
            continue
        rendered = _render_extra_value(item.get(name))
        if rendered is None:
            hidden += 1
            continue
        text, shortened = rendered
        label, label_cut = _limited_plain(name, MAX_EXTRA_LABEL_CHARS, single_line=True)
        if not label:
            hidden += 1
            continue
        block = f"**{label}**"
        if text:
            block += "\n\n" + text
        if shortened or label_cut:
            block += "\n\ntext shortened"
        blocks.append(block)
        shown += 1
    if hidden:
        noun = "field" if hidden == 1 else "fields"
        blocks.append(f"{hidden} {noun} not shown")
    return "\n\n".join(blocks)


def structured_findings(
    result: Mapping[str, Any], schema: Mapping[str, Any] | None = None
) -> tuple[list[Finding], int]:
    """Read ``findings`` from a validated result.

    The main text is the first non-empty string among ``body``,
    ``description``, ``message``, ``detail``, ``problem``, ``issue``,
    ``finding`` and ``summary``. A location is ``path`` or ``file`` with
    ``line`` (a decimal string counts), or, when those name no path, a
    ``location`` or ``where`` string of the form ``path``, ``path:LINE``,
    ``path:LINE-LINE`` or ``path#LLINE``. A range is posted with both
    lines. Every other member is a labelled block, in the order the result
    schema declares the properties, then the order of the document.

    Args:
        result: The stored, validated result document.
        schema: The flow's ``result_schema``, for finding-field order.

    Returns:
        The findings rendered (at most :data:`MAX_FINDINGS`) and the number
        of further entries that were not rendered.
    """
    raw = result.get("findings")
    entries = (
        [item for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, list)
        else []
    )
    schema_names = finding_property_names(schema)
    findings: list[Finding] = []
    for item in entries[:MAX_FINDINGS]:
        used: set[str] = set()
        severity_name = _first_name(item, _SEVERITY_FIELDS)
        if severity_name is not None:
            used.add(severity_name)
        title_name = _first_name(item, _TITLE_FIELDS)
        if title_name is not None:
            used.add(title_name)
        text_name = _first_name(item, _TEXT_FIELDS)
        if text_name is not None:
            used.add(text_name)
        body, body_shortened = _limited_plain(
            item.get(text_name) if text_name else None, MAX_FINDING_BODY_CHARS
        )
        raw_path = _first_text(item, ("path", "file"))
        path_name = _first_name(item, ("path", "file"))
        line: int | None = None
        end_line: int | None = None
        literal: str | None = None
        if path_name is not None:
            used.add(path_name)
            if "line" in item and _parse_line(item.get("line")) is not None:
                used.add("line")
                line = _parse_line(item.get("line"))
        else:
            where_name = _first_name(item, ("location", "where"))
            if where_name is not None:
                used.add(where_name)
                parsed = _parse_location_string(str(item.get(where_name)))
                if parsed is None:
                    literal = str(item.get(where_name))
                else:
                    raw_path, line, end_line = parsed
        if literal is not None:
            path_span = code_span(literal, 320) or None
            stored_path: str | None = None
            stored_line: int | None = None
        else:
            shown_line = line if type(line) is int else None
            path_span = _location(raw_path, shown_line, end_line) or None
            stored_path = raw_path if isinstance(raw_path, str) else None
            stored_line = line
        findings.append(
            Finding(
                severity=plain_text(
                    item.get(severity_name) if severity_name else None,
                    32,
                    single_line=True,
                ),
                title=plain_text(
                    item.get(title_name) if title_name else None,
                    MAX_FINDING_TITLE_CHARS,
                    single_line=True,
                ),
                body=body,
                body_shortened=body_shortened,
                path=path_span,
                raw_path=stored_path if isinstance(stored_path, str) else None,
                line=stored_line,
                details=_extra_details(item, used, schema_names),
            )
        )
    total = len(raw) if isinstance(raw, list) else 0
    return findings, max(total - len(findings), 0)


def review_marker(execution_id: Any, head_sha: str, nonce: str) -> str:
    """Hidden first line that lets a retry recognise its own review."""
    return (
        f"<!-- preloop-platform-review v1 execution={execution_id} "
        f"head={head_sha} nonce={nonce} -->"
    )


def execution_url(execution_id: Any) -> str:
    """Console URL of an execution, built from server configuration only."""
    return f"{settings.preloop_url.rstrip('/')}/console/flows/executions/{execution_id}"


def _finding_heading(index: int, finding: Finding, *, location: bool) -> str:
    severity = f"&#91;{finding.severity}&#93; " if finding.severity else ""
    where = f" ({finding.path})" if location and finding.path else ""
    if finding.title:
        return f"**{index}&#46; {severity}{finding.title}**{where}"
    # No placeholder word. The heading is the number, the severity and the
    # location; the severity prefix's trailing space is only for a title.
    if finding.severity:
        return f"**{index}&#46; {severity.rstrip()}**{where}"
    return f"**{index}&#46;**{where}"


def _finding_block(index: int, finding: Finding, *, location: bool) -> str:
    """One finding as it appears in the body and in its inline comment."""
    block = _finding_heading(index, finding, location=location)
    if finding.body:
        block += "\n\n" + _quote(finding.body)
        if finding.body_shortened:
            block += "\n\ntext shortened"
    if finding.details:
        block += "\n\n" + finding.details
    return block


def diff_lines(files: list[Any]) -> dict[str, set[int]]:
    """Lines of the new side of each file that are part of the diff.

    Args:
        files: Entries of GitHub's "list pull request files".

    Returns:
        File name to the set of right-side line numbers present in a hunk.
    """
    lines: dict[str, set[int]] = {}
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        name, patch = entry.get("filename"), entry.get("patch")
        if not isinstance(name, str) or not isinstance(patch, str):
            continue
        current = 0
        found: set[int] = set()
        for row in patch.split("\n"):
            header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            if header:
                current = int(header.group(1))
            elif current and row[:1] in {"+", " "}:
                found.add(current)
                current += 1
        lines[name] = found
    return lines


def render_review(
    result: Mapping[str, Any],
    *,
    verdict: str,
    execution_id: Any,
    head_sha: str,
    nonce: str,
    inline: Mapping[str, set[int]],
    schema: Mapping[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the review body and inline comments from a validated result.

    The summary is ``summary``, else ``review``, else ``overview``. Findings
    are read by :func:`structured_findings`. ``checked`` (or ``checks``) is
    unchanged. The target, the commit, the review action and every link come
    from the caller, never from the result. Nothing a finding holds is
    dropped without the review saying so.

    Args:
        result: The stored, validated result document.
        verdict: The verdict, already checked against the schema's enum.
        execution_id: Execution the review belongs to.
        head_sha: Reviewed commit.
        nonce: Random value of the publication row.
        inline: Diff lines per file; a finding becomes an inline comment only
            when its path and line are in it.
        schema: The flow's ``result_schema``, for the order of extra fields.

    Returns:
        ``(body, comments)`` for GitHub's "create a review".
    """
    findings, omitted = structured_findings(result, schema)
    comments: list[dict[str, Any]] = []
    parts = [
        review_marker(execution_id, head_sha, nonce),
        f"### Preloop review: {plain_text(verdict, 64, single_line=True)}",
        f"Reviewed commit `{head_sha}`. This review covers that commit only.",
    ]
    summary = plain_text(
        _first_text(result, ("summary", "review", "overview")), MAX_SUMMARY_CHARS
    )
    if summary:
        parts += ["**Summary**", _quote(summary)]
    raw_checked = result.get("checked", result.get("checks"))
    checked = [
        plain_text(item, MAX_CHECKED_ITEM_CHARS, single_line=True)
        for item in (raw_checked if isinstance(raw_checked, list) else [])[
            :MAX_CHECKED_ITEMS
        ]
        if isinstance(item, str)
    ]
    checked = [item for item in checked if item][:MAX_CHECKED_ITEMS]
    checked_block = "\n".join(f"- {item}" for item in checked)[:12000]
    total = len(findings) + omitted
    parts.append(f"**Findings ({total})**" if total else "**Findings:** none reported")
    used = sum(len(part) + 2 for part in parts) + len(checked_block)
    rendered = 0
    for index, finding in enumerate(findings, start=1):
        block = _finding_block(index, finding, location=True)
        if used + len(block) + 2 > MAX_BODY_CHARS - 2000:
            break
        parts.append(block)
        used += len(block) + 2
        rendered += 1
        if (
            len(comments) < MAX_INLINE_COMMENTS
            and finding.raw_path is not None
            and finding.line is not None
            and finding.line in inline.get(finding.raw_path, set())
        ):
            comments.append(
                {
                    "path": finding.raw_path,
                    "line": finding.line,
                    "side": "RIGHT",
                    "body": _finding_block(index, finding, location=False),
                }
            )
    hidden = total - rendered
    if hidden > 0:
        parts.append(
            f"{hidden} further finding(s) are not shown here; the execution "
            "holds the full result."
        )
    if checked_block:
        parts += ["**What was checked**", checked_block]
    parts.append(
        f"Execution: {execution_url(execution_id)}\n\n"
        "Posted by Preloop from the validated result of a read-only review run."
    )
    return "\n\n".join(parts)[:MAX_BODY_CHARS], comments


# --- plan ------------------------------------------------------------------


@dataclass(frozen=True)
class PublicationPlan:
    """What one execution is allowed to put on the provider."""

    review_action: str | None
    status_value: str | None
    status_context: str | None
    status_description: str | None
    verdict: str | None
    #: Why no review is planned; None when one is.
    reason: str | None


def plan_publication(
    flow: Any,
    execution: Any,
    integrity: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> PublicationPlan:
    """Decide the writes from the flow configuration and the stored outcome.

    A review needs all of: a SUCCEEDED execution, a stored result that
    passed the schema gate at capture, the same result passing the flow's
    current schema now, and a verdict the schema admits. Anything else
    yields no review and, when the flow asks for it, the neutral
    ``incomplete_state`` status.

    A review also needs a verified result binding and evidence floor
    (``integrity``, from :func:`preloop.services.review_integrity.
    evaluate_execution`). Without one, or when it was rejected, no review
    is planned and the commit status, when configured, is the flow's
    ``incomplete_state`` or ``error``: never the verdict's state.

    When the flow's ``evidence`` is ``captured`` or ``complete``, the stored
    tool-evidence counts (``evidence``) are checked after the result is
    otherwise postable. A miss is ``evidence_incomplete`` and uses the same
    neutral status as an integrity refusal.

    Args:
        flow: The execution's flow.
        execution: A terminal execution.
        integrity: The controller's integrity outcome; None means unverified.
        evidence: Stored tool-evidence counts. Required when the flow sets
            an evidence gate.

    Returns:
        The plan. ``review_action`` and ``status_value`` may both be None.
    """
    from preloop.services.review_integrity import (
        CATEGORY_BINDING_MISMATCH,
        CATEGORY_NOT_INSPECTED,
        CATEGORY_DEGENERATE,
        STATUS_VERIFIED,
    )

    block = platform_review_block(getattr(flow, "git_clone_config", None)) or {}
    status = _status_block(block)
    context = (status or {}).get("context", DEFAULT_STATUS_CONTEXT)
    result = getattr(execution, "result", None)
    stored = getattr(execution, "result_schema_validation", None)
    schema = getattr(flow, "result_schema", None)
    reason: str | None = None
    verdict: str | None = None
    rejected = getattr(execution, "failure_category", None) in {
        CATEGORY_BINDING_MISMATCH,
        CATEGORY_NOT_INSPECTED,
        CATEGORY_DEGENERATE,
    }
    if rejected:
        reason = str(execution.failure_category)
    elif getattr(execution, "status", None) != "SUCCEEDED":
        reason = "run_not_succeeded"
    elif (
        not isinstance(result, Mapping)
        or not isinstance(stored, Mapping)
        or stored.get("valid") is not True
        or not isinstance(schema, Mapping)
        or getattr(flow, "validate_result", True) is not True
        or validate_result_document(result, schema).get("valid") is not True
    ):
        reason = "result_not_valid"
    else:
        candidate = result.get("verdict")
        if (
            not isinstance(candidate, str)
            or candidate not in (verdict_enum(schema) or [])
            or (status is not None and candidate not in status["verdict_states"])
        ):
            reason = "verdict_unmapped"
        elif not isinstance(integrity, Mapping) or (
            integrity.get("status") != STATUS_VERIFIED
        ):
            category = (
                integrity.get("category") if isinstance(integrity, Mapping) else None
            )
            reason = (
                category
                if category
                in {
                    CATEGORY_BINDING_MISMATCH,
                    CATEGORY_NOT_INSPECTED,
                    CATEGORY_DEGENERATE,
                }
                else "review_unverified"
            )
            rejected = True
        elif not evidence_satisfied(evidence, _block_evidence_level(block)):
            reason = "evidence_incomplete"
            rejected = True
        else:
            verdict = candidate
    if verdict is None:
        incomplete = (status or {}).get("incomplete_state")
        if rejected and status is not None:
            # A result Preloop could not verify is an error on the commit,
            # even when the flow configured no incomplete state.
            incomplete = incomplete or "error"
        return PublicationPlan(
            review_action=None,
            status_value=incomplete,
            status_context=context if incomplete else None,
            status_description=(
                "Review not verified; no verdict for this commit"
                if incomplete and rejected
                else "Review did not complete; no verdict for this commit"
                if incomplete
                else None
            ),
            verdict=None,
            reason=reason,
        )
    action = (block.get("verdict_actions") or {}).get(verdict, "comment")
    return PublicationPlan(
        review_action=action if action in REVIEW_EVENTS else "comment",
        status_value=status["verdict_states"][verdict] if status else None,
        status_context=context if status else None,
        status_description=f"Review verdict: {verdict}" if status else None,
        verdict=verdict,
        reason=None,
    )


# --- provider --------------------------------------------------------------


class _GitHub:
    """The complete set of requests the publish lease is ever used for.

    Writes are limited to two fixed endpoints of one repository: create a
    review on one pull request and create a status on one commit. Everything
    else is a read of that pull request or commit.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        lease: PublicationLease,
        *,
        slug: str,
        number: int,
        sha: str,
    ) -> None:
        if not _SLUG_RE.fullmatch(slug) or not _SHA_RE.fullmatch(sha) or number < 1:
            raise PlatformReviewError("binding_invalid")
        self._client = client
        self._token = lease.token
        self._pull = f"/repos/{slug}/pulls/{number}"
        self._commit = f"/repos/{slug}/commits/{sha}"
        self._status = f"/repos/{slug}/statuses/{sha}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        allowed = (
            path in {f"{self._pull}/reviews", self._status}
            if method == "POST"
            else method == "GET"
            and (
                path
                in {
                    self._pull,
                    f"{self._pull}/files",
                    f"{self._pull}/reviews",
                    f"{self._commit}/statuses",
                }
                or re.fullmatch(re.escape(f"{self._pull}/reviews/") + r"\d+", path)
            )
        )
        if not allowed:
            raise PlatformReviewError("request_not_allowed")
        try:
            response = await self._client.request(
                method,
                _API + path,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=json,
                params=params,
                timeout=30,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise PlatformReviewError("provider_unavailable", retryable=True) from None
        code = response.status_code
        if code >= 500 or code == 429:
            raise PlatformReviewError("provider_unavailable", retryable=True)
        if code == 422:
            raise PlatformReviewError("provider_rejected")
        if code >= 300:
            # 403 covers secondary rate limits as well as missing access.
            # 404 is reported with its status so a review read can tell
            # "not visible yet" from a denial.
            raise PlatformReviewError(
                "provider_denied", retryable=code == 403, status_code=code
            )
        try:
            return response.json()
        except ValueError:
            raise PlatformReviewError("provider_unavailable", retryable=True) from None

    async def _pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = await self._request(
                "GET", path, params={"per_page": 100, "page": page}
            )
            if not isinstance(batch, list):
                raise PlatformReviewError("provider_unavailable", retryable=True)
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    async def pull(self) -> Any:
        """Read the bound pull request."""
        return await self._request("GET", self._pull)

    async def files(self) -> list[Any]:
        """List the files of the bound pull request."""
        return await self._pages(f"{self._pull}/files")

    async def reviews(self) -> list[Any]:
        """List the reviews of the bound pull request."""
        return await self._pages(f"{self._pull}/reviews")

    async def create_review(self, payload: dict[str, Any]) -> Any:
        """Create one submitted review on the bound pull request."""
        return await self._request("POST", f"{self._pull}/reviews", json=payload)

    async def review(self, review_id: str) -> Any:
        """Read one review back."""
        if not review_id.isdecimal():
            raise PlatformReviewError("readback_mismatch")
        return await self._request("GET", f"{self._pull}/reviews/{review_id}")

    async def create_status(self, payload: dict[str, Any]) -> Any:
        """Create one status on the bound commit."""
        return await self._request("POST", self._status, json=payload)

    async def statuses(self) -> list[Any]:
        """List the newest statuses of the bound commit."""
        batch = await self._request(
            "GET", f"{self._commit}/statuses", params={"per_page": 100}
        )
        if not isinstance(batch, list):
            raise PlatformReviewError("provider_unavailable", retryable=True)
        return batch


def _digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").strip().encode()).hexdigest()


def _is_bot(entry: Any) -> bool:
    user = entry.get("user") if isinstance(entry, Mapping) else None
    return isinstance(user, Mapping) and user.get("type") == "Bot"


def _review_check(
    review: Any,
    *,
    review_id: str,
    head_sha: str,
    event: str,
    digests: Collection[str],
) -> str | None:
    """The read-back check that failed, or None when the review matches.

    Not found and a body-less object are ``review_absent`` (not visible yet).
    A present review that differs is ``review_actor``, ``review_commit``,
    ``review_state`` or ``review_body``.
    """
    if (
        not isinstance(review, Mapping)
        or str(review.get("id") or "") != str(review_id)
        or not isinstance(review.get("body"), str)
    ):
        return "review_absent"
    if not _is_bot(review):
        return "review_actor"
    if review.get("commit_id") != head_sha:
        return "review_commit"
    if review.get("state") != _SUBMITTED_STATES.get(event):
        return "review_state"
    if _digest(review["body"]) not in digests:
        return "review_body"
    return None


def _status_check(
    entries: Any,
    *,
    status_id: str,
    state: str | None,
    context: str | None,
    target_url: str,
) -> str | None:
    """The read-back check that failed, or None when the status matches."""
    if not isinstance(entries, list):
        return "status_absent"
    found = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("id")) == str(status_id)
    ]
    if len(found) != 1:
        return "status_absent"
    entry = found[0]
    if entry.get("state") != state:
        return "status_state"
    if entry.get("context") != context:
        return "status_context"
    if entry.get("target_url") != target_url:
        return "status_target_url"
    return None


def _readback_error(
    check: str, *, values: dict[str, Any] | None = None
) -> PlatformReviewError:
    """``readback_pending`` while the object is invisible, else a mismatch.

    A pending failure is retryable. A present difference is final.
    """
    absent = check in READBACK_ABSENT
    reason = "readback_pending" if absent else "readback_mismatch"
    if values is not None:
        return _RecordedError(reason, values, retryable=absent, check=check)
    return PlatformReviewError(reason, retryable=absent, check=check)


async def _pause(seconds: float) -> None:
    """Wait before another read-back. Tests replace this so they do not sleep.

    Args:
        seconds: Delay from :data:`READBACK_RETRY_DELAYS`.
    """
    await asyncio.sleep(seconds)


async def _reread(load: Any, classify: Any) -> None:
    """Read until the object matches, differs, or stays invisible.

    Only an absent check is read again. A present difference fails on the
    first read. After :data:`READBACK_RETRY_DELAYS` the object is still
    absent and the failure stays pending.

    Args:
        load: Async callable returning the provider object.
        classify: Returns a check name, or None when the object matches.

    Raises:
        PlatformReviewError: The read-back did not match.
    """
    check = classify(await load())
    if check is None:
        return
    if check not in READBACK_ABSENT:
        raise _readback_error(check)
    for delay in READBACK_RETRY_DELAYS:
        await _pause(delay)
        check = classify(await load())
        if check is None:
            return
        if check not in READBACK_ABSENT:
            raise _readback_error(check)
    raise _readback_error(check)


async def _load_review(github: _GitHub, review_id: str) -> Any:
    """One review, or None when the provider has not shown it yet."""
    if not str(review_id).isdecimal():
        raise PlatformReviewError("readback_mismatch", check="review_absent")
    try:
        return await github.review(review_id)
    except PlatformReviewError as error:
        if error.status_code == 404:
            return None
        raise


async def _confirm_review(
    github: _GitHub,
    *,
    review_id: str,
    head_sha: str,
    event: str,
    digests: Collection[str],
) -> None:
    """Re-read one review until it matches or the difference is decided.

    Raises:
        PlatformReviewError: Absent after the waits, or present and different.
    """

    def classify(review: Any) -> str | None:
        return _review_check(
            review,
            review_id=review_id,
            head_sha=head_sha,
            event=event,
            digests=digests,
        )

    async def load() -> Any:
        return await _load_review(github, review_id)

    await _reread(load, classify)


async def _confirm_status(
    github: _GitHub,
    *,
    status_id: str,
    state: str | None,
    context: str | None,
    target_url: str,
    values: dict[str, Any],
) -> None:
    """Re-read the status list until the new id matches or the difference is decided.

    Args:
        values: Columns to keep on the row when the check fails. The id is
            already known, so a later attempt can read it without creating.

    Raises:
        _RecordedError: Absent after the waits, or present and different.
    """
    if not str(status_id).isdecimal():
        raise _RecordedError("readback_mismatch", values, check="status_absent")

    def classify(entries: Any) -> str | None:
        return _status_check(
            entries,
            status_id=status_id,
            state=state,
            context=context,
            target_url=target_url,
        )

    async def load() -> Any:
        return await github.statuses()

    try:
        await _reread(load, classify)
    except PlatformReviewError as error:
        raise _RecordedError(
            error.reason,
            values,
            retryable=error.retryable,
            check=error.check,
        ) from None


async def _post_review(
    github: _GitHub,
    row: models.PlatformReviewPublication,
    *,
    binding: ReviewBinding,
    execution: Any,
    plan: PublicationPlan,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Post the review of one claimed publication, or decide to skip it.

    Returns:
        Column values to record on the publication row.
    """
    assert plan.review_action is not None and plan.verdict is not None
    event = REVIEW_EVENTS[plan.review_action]
    marker = review_marker(execution.id, binding.head_sha, row.nonce)
    pull = await github.pull()
    head = pull.get("head") if isinstance(pull, Mapping) else None
    base = pull.get("base") if isinstance(pull, Mapping) else None
    repositories = [
        side.get("repo") if isinstance(side, Mapping) else None for side in (head, base)
    ]
    if (
        not isinstance(head, Mapping)
        or pull.get("number") != binding.pr_number
        or str(pull.get("id")) != binding.provider_pr_id
        or any(
            not isinstance(repo, Mapping)
            or str(repo.get("id")) != binding.repository_identifier
            or repo.get("full_name") != binding.repository_slug
            for repo in repositories
        )
    ):
        raise PlatformReviewError("binding_changed")
    if pull.get("state") != "open":
        return {"review_state": STATE_SKIPPED, "review_reason": "pull_request_closed"}
    if head.get("sha") != binding.head_sha:
        # The verdict belongs to the reviewed commit. It is never attached to
        # a head that was not reviewed, and no review is left on a stale one.
        return {"review_state": STATE_SKIPPED, "review_reason": "head_moved"}
    if not isinstance(base, Mapping) or base.get("ref") != binding.base_branch:
        # The verdict was computed against the admitted base. A retarget
        # makes the live diff a different change, so the review is not posted.
        # The base SHA may advance on the same branch; only the ref is compared.
        return {"review_state": STATE_SKIPPED, "review_reason": "base_moved"}
    if row.attempts > 1:
        # A previous attempt may have posted and then lost its claim. The
        # nonce is unguessable before the first post, so a match is ours.
        for existing in await github.reviews():
            if (
                isinstance(existing, Mapping)
                and isinstance(existing.get("body"), str)
                and existing["body"].startswith(marker)
                and existing.get("commit_id") == binding.head_sha
                and _is_bot(existing)
            ):
                return {
                    "review_state": STATE_POSTED,
                    "review_action": plan.review_action,
                    "review_id": str(existing.get("id")),
                    "body_digest": _digest(existing["body"]),
                    "_verify": None,
                }
    body, comments = render_review(
        execution.result,
        verdict=plan.verdict,
        execution_id=execution.id,
        head_sha=binding.head_sha,
        nonce=row.nonce,
        inline=diff_lines(await github.files()),
        schema=schema,
    )
    payload: dict[str, Any] = {
        "commit_id": binding.head_sha,
        "body": body,
        "event": event,
    }
    try:
        created = await github.create_review({**payload, "comments": comments})
    except PlatformReviewError as error:
        if error.reason != "provider_rejected" or not comments:
            raise
        # An inline position GitHub does not accept must not cost the review:
        # every finding is in the body already.
        created = await github.create_review(payload)
    review_id = str(created.get("id")) if isinstance(created, Mapping) else ""
    if not review_id.isdecimal():
        raise PlatformReviewError("readback_mismatch", check="review_absent")
    digest = _digest(body)
    return {
        "review_state": STATE_POSTED,
        "review_action": plan.review_action,
        "review_id": review_id,
        "body_digest": digest,
        "_verify": {"event": event, "digest": digest},
    }


async def _post_status(
    github: _GitHub, *, execution: Any, plan: PublicationPlan
) -> dict[str, Any]:
    """Create the commit status on the reviewed commit and read it back."""
    target_url = execution_url(execution.id)
    payload = {
        "state": plan.status_value,
        "context": plan.status_context,
        "description": (plan.status_description or "")[:140],
        "target_url": target_url,
    }
    created = await github.create_status(payload)
    status_id = str(created.get("id")) if isinstance(created, Mapping) else ""
    if not status_id.isdecimal():
        raise PlatformReviewError("readback_mismatch", check="status_absent")
    values = {
        "status_state": STATE_POSTED,
        "status_value": plan.status_value,
        "status_context": plan.status_context,
        "status_id": status_id,
        "status_target_url": target_url,
    }
    await _confirm_status(
        github,
        status_id=status_id,
        state=plan.status_value,
        context=plan.status_context,
        target_url=str(payload["target_url"]),
        values=values,
    )
    return values


class _RecordedError(PlatformReviewError):
    """A failure after the provider accepted a write that must be recorded."""

    def __init__(
        self,
        reason: str,
        values: dict[str, Any],
        *,
        retryable: bool = False,
        check: str | None = None,
    ) -> None:
        super().__init__(reason, retryable=retryable, check=check)
        self.values = values


async def _mint(
    tracker: models.Tracker, url: str, *, review: bool, status: bool
) -> tuple[PublicationLease, bool]:
    """Mint the narrowest publish lease.

    Returns:
        The lease and whether it carries ``statuses: write``. The commit
        status is dropped, with a recorded reason, only when GitHub refuses
        the request that includes it and grants the one without it.
    """
    profile = (
        "review_and_status" if review and status else ("review" if review else "status")
    )
    async with _http_client() as client:
        try:
            lease = await mint_repository_lease(
                tracker, url, profile=profile, client=client
            )
            return lease, status
        except LeaseRefusedError as error:
            if error.status_code != 422 or profile != "review_and_status":
                raise
        return (
            await mint_repository_lease(tracker, url, profile="review", client=client),
            False,
        )


def _http_client() -> httpx.AsyncClient:
    """Client for provider requests; tests replace this with a transport."""
    return httpx.AsyncClient()


async def _confirm_recorded_review(
    github: _GitHub,
    row: models.PlatformReviewPublication,
    *,
    execution: Any,
    plan: PublicationPlan,
    binding: CiReviewBinding,
    schema: Any,
) -> None:
    """Re-read a review this row already recorded. Does not create one.

    The comparison uses the body digest stored when the review was sent.
    A row posted before that digest was stored is compared with a fresh
    render. When that differs, the row stays failed.

    Raises:
        PlatformReviewError: The review is still absent, or present and
            different.
    """
    result = execution.result if isinstance(execution.result, Mapping) else {}
    verdict = plan.verdict
    if not isinstance(verdict, str):
        stored = result.get("verdict")
        verdict = stored if isinstance(stored, str) else ""
    if row.body_digest:
        digests = {row.body_digest}
    else:
        current, _comments = render_review(
            result,
            verdict=verdict,
            execution_id=execution.id,
            head_sha=binding.head_sha,
            nonce=row.nonce,
            inline={},
            schema=schema if isinstance(schema, Mapping) else None,
        )
        digests = {_digest(current)}
    await _confirm_review(
        github,
        review_id=str(row.review_id),
        head_sha=binding.head_sha,
        event=REVIEW_EVENTS.get(row.review_action or "comment", "COMMENT"),
        digests=digests,
    )


async def _confirm_recorded_status(
    github: _GitHub,
    row: models.PlatformReviewPublication,
    *,
    execution: Any,
) -> None:
    """Re-read a status this row already recorded. Does not create one.

    Raises:
        PlatformReviewError: The status is still absent, or present and
            different. The recorded id is left as it is.
    """
    try:
        await _confirm_status(
            github,
            status_id=str(row.status_id),
            state=row.status_value,
            context=row.status_context,
            target_url=row.status_target_url or execution_url(execution.id),
            values={},
        )
    except _RecordedError as error:
        raise PlatformReviewError(
            error.reason, retryable=error.retryable, check=error.check
        ) from None


async def _publish_claimed(
    db: Session,
    row: models.PlatformReviewPublication,
    *,
    flow: models.Flow,
    execution: models.FlowExecution,
    preconditions: ReviewPreconditions | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Run one attempt for a claimed row and record its outcome.

    Raises:
        PreconditionFailedError: A caller precondition no longer holds on
            the re-read flow and execution, or on the live pull request.
            Raised before the first write to the provider.
    """
    from preloop.services.read_only_review import validate_read_only_flow

    pre = preconditions or ReviewPreconditions()

    record = crud.crud_platform_review_publication.record
    # The execution's own identity must still be admitted: active, its
    # project on the flow, its snapshot unchanged, and holding the grant it
    # used to trigger or the explicit publish action.
    binding: ReviewBinding | None = None
    dispatched = False
    for action in (CiAction.TRIGGER, CiAction.PUBLISH_REVIEW):
        try:
            binding = crud.crud_ci_execution.authorize_dispatch(
                db, execution=execution, action=action
            )
            dispatched = True
            break
        except PermissionError:
            continue
    if not dispatched:
        raise PlatformReviewError("admission_revoked")
    if binding is None:
        from preloop.services.event_review import authorize_event_review

        stored = parse_stored_review_binding(execution.ci_review_binding)
        if not isinstance(stored, EventReviewBinding):
            raise PlatformReviewError("binding_changed")
        binding = authorize_event_review(db, execution)
    if (
        binding is None
        or binding.tracker_type != "github"
        or str(flow.id) != str(binding.flow_id)
        or str(flow.account_id) != str(binding.account_id)
        or (row.repository, row.pull_request, row.head_sha)
        != (binding.repository_slug.lower(), str(binding.pr_number), binding.head_sha)
    ):
        raise PlatformReviewError("binding_changed")
    # The list may have changed since the entry check: re-read it.
    db.refresh(flow)
    if not project_in_posting_scope(flow.git_clone_config, binding.project_id):
        raise PlatformReviewError("admission_revoked")
    if pre.any:
        # Posting re-reads the flow and the result: check against what is
        # stored now, not what the caller's earlier check saw.
        db.refresh(flow)
        db.refresh(execution)
        failures = check_stored_preconditions(
            pre,
            flow=flow,
            execution=execution,
            binding=binding,
            evidence=evidence,
        )
        if failures:
            raise PreconditionFailedError(failures)
    try:
        validate_read_only_flow(flow)
        validate_platform_review_flow(flow)
    except ValueError:
        raise PlatformReviewError("configuration_invalid") from None
    selected = [
        repo
        for repo in dict(flow.git_clone_config or {}).get("repositories") or []
        if isinstance(repo, Mapping)
        and repo.get("project_id") == str(binding.project_id)
    ]
    if (
        len(selected) != 1
        or selected[0].get("tracker_id") != str(binding.tracker_id)
        or selected[0].get("repository_url")
    ):
        raise PlatformReviewError("binding_changed")
    tracker = crud.crud_tracker.get_by_id_and_account(
        db, id=str(binding.tracker_id), account_id=str(binding.account_id)
    )
    try:
        if tracker is None:
            raise PublicationError("missing")
        validate_publication_tracker(tracker)
    except PublicationError:
        raise PlatformReviewError("tracker_unsupported") from None
    from preloop.services.review_integrity import evaluate_execution

    # Recomputed from stored evidence at every attempt, never read back from
    # a stored outcome or the result itself.
    plan = plan_publication(
        flow,
        execution,
        integrity=evaluate_execution(db, flow, execution, require_head=True),
        evidence=evidence,
    )
    if (
        plan.review_action is not None
        and row.review_state == STATE_SKIPPED
        and row.review_reason == "evidence_incomplete"
    ):
        # The refusal posted no review. A later call, once the stored
        # records meet the gate, posts it. The incomplete commit status is
        # replaced by the verdict's state.
        reopen: dict[str, Any] = {"review_state": None, "review_reason": None}
        if (
            plan.status_value is not None
            and row.status_state == STATE_POSTED
            and row.status_value != plan.status_value
        ):
            reopen["status_state"] = None
            reopen["status_reason"] = None
            reopen["status_value"] = None
        row = record(db, row=row, **reopen)
    if row.status_reason == "statuses_permission_missing":
        # Only an explicit call reaches here again; the permission may have
        # been approved on the App installation since.
        row = record(db, row=row, status_state=None, status_reason=None)
    want_review = plan.review_action is not None and row.review_state is None
    want_status = plan.status_value is not None and row.status_state is None
    if plan.review_action is None and row.review_state is None:
        row = record(db, row=row, review_state=STATE_SKIPPED, review_reason=plan.reason)
    if plan.status_value is None and row.status_state is None:
        row = record(
            db,
            row=row,
            status_state=STATE_SKIPPED,
            status_reason="not_configured"
            if plan.review_action is not None
            else (plan.reason or "not_configured"),
        )
    # Re-read ids only while a read-back comparison is still open. A review
    # or status that was already confirmed stays as recorded, so a later
    # status does not request pull requests and a later review does not
    # request statuses. ``readback_check`` names the comparison. A row
    # recorded before that column existed keeps ``readback_mismatch`` or
    # ``readback_pending`` in ``reason`` (the claim does not clear those),
    # and that is enough to owe the read. The pass never creates.
    owed = bool(row.readback_check) or row.reason in {
        "readback_mismatch",
        "readback_pending",
    }
    confirm_review = (
        owed
        and not want_review
        and row.review_state == STATE_POSTED
        and bool(row.review_id)
    )
    confirm_status = (
        owed
        and not want_status
        and row.status_state == STATE_POSTED
        and bool(row.status_id)
    )
    failure: PlatformReviewError | None = None
    need_review = want_review or confirm_review
    need_status = want_status or confirm_status
    if need_review or need_status:
        url = f"https://github.com/{binding.repository_slug}.git"
        try:
            lease, has_status = await _mint(
                tracker, url, review=need_review, status=need_status
            )
        except LeaseRefusedError as error:
            if error.status_code == 422 and not need_review:
                raise PlatformReviewError("statuses_permission_missing") from None
            raise PlatformReviewError(
                "lease_refused", retryable=error.status_code >= 500
            ) from None
        except PublicationError:
            raise PlatformReviewError("lease_unavailable", retryable=True) from None
        if (
            lease.repository_url != url
            or lease.expires_at.tzinfo is None
            or lease.expires_at <= datetime.now(timezone.utc)
        ):
            raise PlatformReviewError("lease_unavailable", retryable=True)
        if want_status and not has_status:
            row = record(
                db,
                row=row,
                status_state=STATE_SKIPPED,
                status_reason="statuses_permission_missing",
            )
            want_status = False
        async with _http_client() as client:
            try:
                github = _GitHub(
                    client,
                    lease,
                    slug=binding.repository_slug,
                    number=binding.pr_number,
                    sha=binding.head_sha,
                )
                if want_review and (
                    pre.head_sha is not None or pre.base_sha is not None
                ):
                    # The status-only lease cannot read the pull request.
                    # Commit preconditions are checked here only when a review
                    # is about to be posted; a refusal writes the status on
                    # the bound commit either way.
                    failures = check_live_preconditions(pre, await github.pull())
                    if failures:
                        raise PreconditionFailedError(failures)
                if want_review:
                    try:
                        values = await _post_review(
                            github,
                            row,
                            binding=binding,
                            execution=execution,
                            plan=plan,
                            schema=getattr(flow, "result_schema", None),
                        )
                        posted = "_verify" in values
                        verify = values.pop("_verify", None)
                        row = record(db, row=row, **values)
                        if posted:
                            _record_receipt(db, row, binding=binding)
                        if verify is not None:
                            await _confirm_review(
                                github,
                                review_id=str(row.review_id),
                                head_sha=binding.head_sha,
                                event=verify["event"],
                                digests={verify["digest"]},
                            )
                    except PlatformReviewError as error:
                        failure = error
                elif confirm_review:
                    try:
                        await _confirm_recorded_review(
                            github,
                            row,
                            execution=execution,
                            plan=plan,
                            binding=binding,
                            schema=getattr(flow, "result_schema", None),
                        )
                    except PlatformReviewError as error:
                        failure = error
                if want_status:
                    try:
                        row = record(
                            db,
                            row=row,
                            **await _post_status(
                                github, execution=execution, plan=plan
                            ),
                        )
                    except _RecordedError as error:
                        row = record(db, row=row, **error.values)
                        failure = failure or error
                    except PlatformReviewError as error:
                        failure = failure or error
                elif confirm_status and failure is None:
                    try:
                        await _confirm_recorded_status(github, row, execution=execution)
                    except PlatformReviewError as error:
                        failure = error
            finally:
                try:
                    await revoke_repository_lease(lease, client)
                except PublicationError:
                    logger.warning(
                        "Publish lease revocation failed for execution %s; "
                        "expiry remains enforced",
                        execution.id,
                    )
    if failure is not None:
        raise failure
    if row.status_reason == "statuses_permission_missing":
        # The review is posted and recorded; the publication stays open so
        # that a publish call can add the status once the App may set it.
        raise PlatformReviewError("statuses_permission_missing")
    wrote = STATE_POSTED in (row.review_state, row.status_state)
    if row.review_state == STATE_POSTED:
        state = STATE_POSTED
    elif row.status_state == STATE_POSTED:
        # A commit status without a review (the run did not produce a
        # postable verdict, or the head moved) is never called ``posted``.
        state = STATE_STATUS_ONLY
    else:
        state = STATE_SKIPPED
    record(
        db,
        row=row,
        state=state,
        reason=row.review_reason
        or (row.status_reason if row.status_reason != "not_configured" else None),
        retryable=False,
        next_attempt_at=None,
        readback_check=None,
        posted_at=datetime.now(timezone.utc).replace(tzinfo=None) if wrote else None,
    )


def _record_receipt(
    db: Session, row: models.PlatformReviewPublication, *, binding: ReviewBinding
) -> None:
    """Add the server-owned review receipt other features already read."""
    try:
        crud.crud_review_publication_receipt.record(
            db,
            account_id=binding.account_id,
            execution_id=row.execution_id,
            provider="github",
            repository=binding.repository_slug,
            pull_request=binding.pr_number,
            review_action=row.review_action or "comment",
            head_sha=binding.head_sha,
            provider_review_id=row.review_id,
        )
    except Exception:
        db.rollback()
        logger.warning("Could not record the review receipt of %s", row.execution_id)


def _backoff(attempts: int) -> datetime:
    delay = min(60 * 2 ** max(attempts - 1, 0), 3600)
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)


async def publish_platform_review(
    db: Session,
    *,
    account_id: Any,
    execution_id: Any,
    explicit: bool = False,
    preconditions: ReviewPreconditions | None = None,
    actor_user_id: Any = None,
) -> dict[str, Any] | None:
    """Publish, or publish again, the stored result of a finished execution.

    No model runs. The call is idempotent: a write the publication row
    already records is never repeated, and a concurrent caller gets the
    current state instead of a second attempt.

    An automatic call (``explicit=False``) does nothing for a flow in manual
    mode. Caller preconditions are checked against stored state before the
    attempt is claimed, again after the flow and execution are re-read for
    the attempt, and against the live pull request before the first write.

    Args:
        db: Database session.
        account_id: Account that owns the execution.
        execution_id: Execution whose stored result is published.
        explicit: A person or the execution's own CI identity asked for it.
            Explicit calls also retry attempts that ended ``skipped`` or
            failed for a reason automatic retries do not repeat.
        preconditions: What the caller validated (explicit calls only).
        actor_user_id: Account user who made an explicit call, for the audit
            entry; None for a CI identity or the completion path.

    Returns:
        The public projection of the publication, or None when the flow has
        not opted in (or posts manually) and the call is not explicit.

    Raises:
        LookupError: The execution does not exist in the account.
        PlatformReviewRefusedError: An explicit call for an execution that cannot
            be published at all (opt-in off, not finished, no binding).
        PreconditionFailedError: A caller precondition does not hold. Nothing
            was posted and the publication is not recorded as posted.
    """
    pre = preconditions or ReviewPreconditions()
    execution = crud.crud_flow_execution.get_without_workspace(
        db, execution_id, account_id=account_id, refresh=True
    )
    flow = (
        crud.crud_flow.get(
            db, id=str(execution.flow_id), account_id=account_id, refresh=True
        )
        if execution is not None
        else None
    )
    if execution is None or flow is None:
        raise LookupError("Flow execution not found")
    mode = platform_review_mode(flow.git_clone_config)
    if mode == MODE_OFF:
        # A pending retry of a flow that opted out must not stay due forever.
        _stop_retrying(db, execution.id, "platform_review_disabled")
        if explicit:
            raise PlatformReviewRefusedError("platform_review_disabled")
        return None
    if mode == MODE_MANUAL and not explicit:
        # Manual mode: only the explicit call posts. A retry left over from
        # automatic mode stops here instead of posting unrequested.
        _stop_retrying(db, execution.id, "manual_mode")
        return None
    binding = parse_stored_review_binding(execution.ci_review_binding)
    if binding is None:
        if explicit:
            raise PlatformReviewRefusedError("no_review_binding")
        return None
    # The project of the stored binding decides, never anything the caller
    # sends. A retry left from before the list changed stops here. A finished
    # run seen by the automatic path is closed as a terminal skip, so the
    # sweep does not publish it later should the list grow to include it.
    if not project_in_posting_scope(flow.git_clone_config, binding.project_id):
        _stop_retrying(db, execution.id, OUT_OF_SCOPE_REASON)
        if explicit:
            raise PlatformReviewRefusedError(OUT_OF_SCOPE_REASON)
        if execution.status in TERMINAL_STATUSES:
            _record_out_of_scope(db, execution.id, binding)
        return None
    # The list at the time the run finished decides: a run closed outside it
    # stays unposted after the project is added.
    if _closed_out_of_scope(db, execution.id):
        if explicit:
            raise PlatformReviewRefusedError(OUT_OF_SCOPE_REASON)
        return None
    if execution.status not in TERMINAL_STATUSES:
        if explicit:
            raise PlatformReviewRefusedError("execution_not_finished")
        return None
    # The last streaming gateway request can still be committing its evidence.
    # Do not post, and do not record evidence_incomplete, until that window
    # has passed. Wake at the deadline; the sweep covers a lost timer.
    if (
        evidence_may_still_arrive(execution)
        and stricter_evidence(evidence_level(flow.git_clone_config), pre.evidence)
        != "any"
    ):
        if explicit:
            raise PlatformReviewRefusedError("evidence_pending")
        due = evidence_settled_at(execution)
        if due is not None:
            _schedule_settled_publication(
                account_id=account_id, execution_id=execution_id, due=due
            )
        return None
    # One read of the stored records serves the precondition, the plan and
    # the record returned to the caller. A flow with the gate off, and a
    # publish call that does not ask for evidence, does not read them.
    counts: dict[str, Any] | None = None
    if pre.evidence is not None or evidence_level(flow.git_clone_config) != "any":
        counts = stored_tool_evidence(db, execution)
    if pre.any:
        failures = check_stored_preconditions(
            pre,
            flow=flow,
            execution=execution,
            binding=binding,
            evidence=counts,
        )
        if failures:
            _audit_attempt(
                db,
                account_id=account_id,
                execution=execution,
                actor_user_id=actor_user_id,
                mode=mode,
                posted_by=NOT_POSTED,
                status="denied",
                details={"preconditions_failed": failures},
            )
            raise PreconditionFailedError(failures)
    store = crud.crud_platform_review_publication
    row = store.claim(
        db,
        account_id=binding.account_id,
        execution_id=str(execution.id),
        repository=binding.repository_slug,
        pull_request=binding.pr_number,
        head_sha=binding.head_sha,
        reclaim_states=(STATE_FAILED, STATE_SKIPPED) if explicit else (STATE_FAILED,),
        retryable_only=not explicit,
    )
    if row is None:
        return platform_review_projection(
            db,
            execution,
            flow=flow,
            evidence=counts,
            with_evidence=counts is not None,
        )
    # Manual mode never retries on its own: a failed explicit attempt waits
    # for the caller, who may want to check again before posting.
    automatic_retry = mode == MODE_AUTOMATIC
    was_posted = row.review_state == STATE_POSTED
    outcome = "success"
    details: dict[str, Any] = {}
    try:
        await _publish_claimed(
            db,
            row,
            flow=flow,
            execution=execution,
            preconditions=pre,
            evidence=counts,
        )
    except asyncio.CancelledError:
        # Cut short by the caller's time limit: hand the row to the monitor
        # instead of leaving it claimed.
        _fail(db, execution.id, "attempt_interrupted", automatic_retry)
        raise
    except PreconditionFailedError as error:
        # Nothing was written to the provider. The row records the refusal,
        # not a post, and an explicit call with corrected values may retry.
        _fail(db, execution.id, "precondition_failed", False)
        _audit_attempt(
            db,
            account_id=account_id,
            execution=execution,
            actor_user_id=actor_user_id,
            mode=mode,
            posted_by=NOT_POSTED,
            status="denied",
            details={"preconditions_failed": error.failures},
        )
        raise
    except PlatformReviewError as error:
        _fail(
            db,
            execution.id,
            error.reason,
            error.retryable and automatic_retry,
            check=error.check,
        )
        outcome, details = "failure", {"reason": error.reason}
        if error.check:
            details["readback_check"] = error.check
            logger.warning(
                "Platform review read-back %s for execution %s",
                error.check,
                execution.id,
            )
    except Exception:
        # A posting failure never reaches the execution's status or result.
        logger.error("Platform review publication failed for %s", execution.id)
        _fail(db, execution.id, "internal_error", automatic_retry)
        outcome, details = "failure", {"reason": "internal_error"}
    posted_now = False
    if not was_posted:
        current = store.get_for_execution(db, execution_id=str(execution.id))
        posted_now = current is not None and current.review_state == STATE_POSTED
    posted_by = (
        (POSTED_MANUALLY if explicit else POSTED_AUTOMATICALLY)
        if posted_now
        else NOT_POSTED
    )
    if posted_now:
        _record_posted_by(db, execution.id, posted_by, source=review_source(execution))
    _audit_attempt(
        db,
        account_id=account_id,
        execution=execution,
        actor_user_id=actor_user_id,
        mode=mode,
        posted_by=posted_by,
        status=outcome,
        details=details,
    )
    return platform_review_projection(
        db, execution, flow=flow, evidence=counts, with_evidence=counts is not None
    )


def review_source(execution: Any) -> str | None:
    """Which route admitted the run: ``tracker_event`` or ``restricted_ci``.

    Args:
        execution: Flow execution row.

    Returns:
        The source, or None when the binding is neither version.
    """
    binding = getattr(execution, "ci_review_binding", None)
    if not isinstance(binding, Mapping):
        return None
    if binding.get("version") == 2 and binding.get("source") == "tracker_event":
        return "tracker_event"
    if binding.get("version") == 1:
        return "restricted_ci"
    return None


def _record_posted_by(
    db: Session, execution_id: Any, posted_by: str, *, source: str | None = None
) -> None:
    """Record on the execution how its review reached the pull request.

    ``posted_by`` stays ``automatic`` or ``manual``. ``source`` is the
    separate route fact.
    """
    facts: dict[str, Any] = {POSTED_BY_FACT: posted_by}
    if source in {"tracker_event", "restricted_ci"}:
        facts[SOURCE_FACT] = source
    try:
        crud.crud_flow_execution.record_completion_evidence(
            db, execution_id=execution_id, facts=facts
        )
    except Exception:
        db.rollback()
        logger.warning("Could not record who posted the review of %s", execution_id)


def posted_by(execution: Any) -> str | None:
    """How the posted review of an execution got there: automatic or manual.

    Returns:
        The recorded value, or None when nothing is recorded (no review was
        posted, or it was posted before the record existed).
    """
    from preloop.services.review_completion_evidence import completion_facts

    value = completion_facts(execution).get(POSTED_BY_FACT)
    return value if value in {POSTED_AUTOMATICALLY, POSTED_MANUALLY} else None


def _audit_attempt(
    db: Session,
    *,
    account_id: Any,
    execution: Any,
    actor_user_id: Any,
    mode: str,
    posted_by: str,
    status: str,
    details: dict[str, Any],
) -> None:
    """Write one audit entry for a publication attempt. Never raises."""
    try:
        crud.crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=actor_user_id,
            action="platform_review_publish",
            resource_type="flow_execution",
            resource_id=str(execution.id),
            status=status,
            details={"mode": mode, "posted_by": posted_by, **details},
        )
    except Exception:
        db.rollback()
        logger.warning("Could not audit the review publication of %s", execution.id)


def close_out_of_scope_run(db: Session, execution_id: Any) -> None:
    """Close a run that just finished outside the posting list. Never raises.

    For paths that write a terminal status without the completion publisher
    (the stale-execution monitor, a failed local dispatch). Recording the
    skip when the run finishes keeps a later widening of the list from
    publishing it. A run inside the list, or without a list, is untouched.

    Args:
        db: Database session; the terminal status is committed.
        execution_id: The execution that just finished.
    """
    try:
        execution = crud.crud_flow_execution.get(db, execution_id, refresh=True)
        if execution is None or execution.status not in TERMINAL_STATUSES:
            return
        flow = getattr(execution, "flow", None)
        config = getattr(flow, "git_clone_config", None)
        if platform_review_mode(config) == MODE_OFF:
            return
        binding = parse_stored_review_binding(execution.ci_review_binding)
        if binding is None or project_in_posting_scope(config, binding.project_id):
            return
        _record_out_of_scope(db, execution.id, binding)
    except Exception:
        db.rollback()
        logger.error("Could not close the out-of-scope run %s", execution_id)


def _closed_out_of_scope(db: Session, execution_id: Any) -> bool:
    """Whether the execution was closed as outside the posting list."""
    row = crud.crud_platform_review_publication.get_for_execution(
        db, execution_id=execution_id
    )
    return (
        row is not None
        and row.state == STATE_SKIPPED
        and row.reason == OUT_OF_SCOPE_REASON
    )


def _record_out_of_scope(db: Session, execution_id: Any, binding: Any) -> None:
    """Close a finished run outside the posting list as a terminal skip.

    Nothing is posted. The row is not a failure and is never retried. An
    execution that already has a row keeps it.

    Args:
        db: Database session.
        execution_id: The finished execution.
        binding: Its stored review binding.
    """
    store = crud.crud_platform_review_publication
    try:
        row = store.claim(
            db,
            account_id=binding.account_id,
            execution_id=str(execution_id),
            repository=binding.repository_slug,
            pull_request=binding.pr_number,
            head_sha=binding.head_sha,
            reclaim_states=(),
        )
        if row is None:
            return
        store.record(
            db,
            row=row,
            state=STATE_SKIPPED,
            reason=OUT_OF_SCOPE_REASON,
            review_state=STATE_SKIPPED,
            review_reason=OUT_OF_SCOPE_REASON,
            status_state=STATE_SKIPPED,
            status_reason=OUT_OF_SCOPE_REASON,
            retryable=False,
            next_attempt_at=None,
        )
    except Exception:
        db.rollback()
        logger.error("Could not record the skipped publication of %s", execution_id)


def _stop_retrying(db: Session, execution_id: Any, reason: str) -> None:
    """Take a failed or abandoned row out of the automatic retry queue."""
    store = crud.crud_platform_review_publication
    try:
        row = store.get_for_execution(db, execution_id=execution_id)
        if row is None:
            return
        stale = datetime.now(timezone.utc).replace(tzinfo=None) - CLAIM_STALE_AFTER
        abandoned = (
            row.state == STATE_PUBLISHING
            and row.claimed_at is not None
            and row.claimed_at < stale
        )
        if not abandoned and not (row.state == STATE_FAILED and row.retryable):
            return
        store.record(
            db,
            row=row,
            state=STATE_FAILED,
            reason=reason[:64],
            retryable=False,
            next_attempt_at=None,
        )
    except Exception:
        db.rollback()
        logger.error("Could not close the publication retry of %s", execution_id)


def _fail(
    db: Session,
    execution_id: Any,
    reason: str,
    retryable: bool,
    *,
    check: str | None = None,
) -> None:
    store = crud.crud_platform_review_publication
    try:
        db.rollback()
        row = store.get_for_execution(db, execution_id=execution_id)
        if row is None:
            return
        again = retryable and row.attempts < MAX_AUTOMATIC_ATTEMPTS
        # A failure that does not name a comparison keeps the one already
        # recorded, so a provider error cannot forget an open read-back.
        stored_check = check[:32] if check else row.readback_check
        store.record(
            db,
            row=row,
            state=STATE_FAILED,
            reason=reason[:64],
            retryable=again,
            next_attempt_at=_backoff(row.attempts) if again else None,
            readback_check=stored_check,
        )
    except Exception:
        db.rollback()
        logger.error("Could not record the publication failure of %s", execution_id)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def platform_review_projection(
    db: Session,
    execution: Any,
    *,
    flow: Any = None,
    evidence: Mapping[str, Any] | None = None,
    with_evidence: bool = False,
) -> dict[str, Any] | None:
    """What was posted for an execution, safe for its restricted CI identity.

    Args:
        db: Database session.
        execution: Flow execution row.
        flow: The execution's flow, when the caller holds it.
        evidence: Counts already computed for this request.
        with_evidence: Attach ``tool_evidence``. Left off for the execution
            list, a status poll and the finished event, which would otherwise
            decode every stored record.

    Returns:
        None when the flow has not opted in and nothing was ever claimed;
        ``{"status": "pending", ...}`` before the first attempt; otherwise
        the recorded state. ``mode`` is the flow's current posting mode and
        ``posted_by`` says how the review reached the pull request:
        ``automatic`` (when the execution finished, or by an automatic
        retry), ``manual`` (the explicit publish call) or ``none``; null for
        a review posted before the record existed. ``tool_evidence``, when
        requested, is the count summary of the stored records. Contains no
        credential, no provider response and no evidence values.
        ``readback_check`` names the last comparison that failed
        (``review_body``, ``status_absent``, and the other read-back checks).
        It can stay set when a later attempt fails for another reason while
        that read-back is still owed, and is null once the publication is
        posted or no read-back has failed.
    """
    row = crud.crud_platform_review_publication.get_for_execution(
        db, execution_id=execution.id
    )
    if flow is None:
        flow = getattr(execution, "flow", None)
    mode = platform_review_mode(getattr(flow, "git_clone_config", None))
    how = posted_by(execution)
    if row is None and mode == MODE_OFF:
        return None
    counts: Mapping[str, Any] | None
    if not with_evidence:
        counts = None
    elif evidence is not None:
        counts = evidence
    else:
        counts = stored_tool_evidence(db, execution)
    scope = posting_project_ids(getattr(flow, "git_clone_config", None))
    in_scope: bool | None = None
    if scope is not None:
        stored = parse_stored_review_binding(
            getattr(execution, "ci_review_binding", None)
        )
        # A run without a binding has no project to compare: it is not
        # known to be outside the list, so the key stays unset.
        in_scope = (
            None
            if stored is None
            else project_in_posting_scope(flow.git_clone_config, stored.project_id)
        )
    if row is None:
        binding = getattr(execution, "ci_review_binding", None)
        # Outside the posting list nothing will be attempted: say so
        # instead of reporting a publication that is still to come.
        skipped = in_scope is False
        reason, due_at = (
            (OUT_OF_SCOPE_REASON, None) if skipped else _pending_cause(execution, flow)
        )
        payload = {
            "mode": mode,
            "posted_by": NOT_POSTED,
            "status": STATE_SKIPPED if skipped else "pending",
            "reason": reason,
            "due_at": due_at,
            "head_sha": binding.get("head_sha")
            if isinstance(binding, Mapping)
            else None,
            "review": None,
            "review_id": None,
            "review_action": None,
            "review_reason": None,
            "commit_status": None,
            "status_context": None,
            "status_reason": None,
            "posted_at": None,
            "attempts": 0,
            "retry_scheduled": False,
            "source": review_source(execution),
        }
    else:
        payload = {
            "mode": mode,
            "posted_by": how if row.review_state == STATE_POSTED else NOT_POSTED,
            "status": row.state,
            "reason": row.reason,
            "due_at": _iso(row.next_attempt_at)
            if row.retryable and row.next_attempt_at is not None
            else None,
            "head_sha": row.head_sha,
            "review": row.review_state,
            "review_id": row.review_id,
            "review_action": row.review_action,
            "review_reason": row.review_reason,
            "commit_status": row.status_value,
            "status_context": row.status_context,
            "status_reason": row.status_reason,
            "posted_at": _iso(row.posted_at),
            "attempts": row.attempts,
            "retry_scheduled": bool(row.retryable and row.next_attempt_at is not None),
            "source": review_source(execution),
        }
    if with_evidence:
        payload["tool_evidence"] = counts
    payload["readback_check"] = None if row is None else row.readback_check
    if in_scope is not None:
        # Only this run's own project: never the list or its size, which
        # would describe the flow's other projects to a CI identity.
        payload["project_in_posting_scope"] = in_scope
    return payload


async def publish_finished_execution(*, account_id: Any, execution_id: Any) -> None:
    """Completion-path entry point, on a database session of its own.

    Args:
        account_id: Account that owns the execution.
        execution_id: Execution that just reached a terminal status.
    """
    from preloop.models.db.session import get_db_session

    session = next(get_db_session())
    try:
        await publish_platform_review(
            session, account_id=account_id, execution_id=execution_id
        )
    finally:
        session.close()


async def sweep_platform_reviews(limit: int = 20) -> int:
    """Retry due publications and pick up finished runs nobody published.

    Returns:
        Number of executions attempted.
    """
    from preloop.models.db.session import get_db_session

    session = next(get_db_session())
    attempted = 0
    try:
        store = crud.crud_platform_review_publication
        targets = store.due_execution_ids(
            db=session, limit=limit, max_attempts=MAX_AUTOMATIC_ATTEMPTS
        )
        targets += store.unclaimed_execution_ids(
            db=session,
            finished_within=UNCLAIMED_LOOKBACK,
            minimum_age=EVIDENCE_SETTLE_AFTER,
            limit=limit,
        )
        session.commit()
        for account_id, execution_id in targets:
            try:
                await publish_platform_review(
                    session, account_id=account_id, execution_id=execution_id
                )
                attempted += 1
            except Exception:
                session.rollback()
                logger.error("Platform review sweep failed for %s", execution_id)
    finally:
        session.close()
    return attempted
