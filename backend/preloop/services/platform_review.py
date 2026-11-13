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

import hashlib
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import crud, models
from preloop.models.models.platform_review_publication import (
    STATE_FAILED,
    STATE_POSTED,
    STATE_SKIPPED,
)
from preloop.schemas.ci_execution import CiReviewBinding
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
#: Pages (of 100) read when listing the files or reviews of a pull request.
MAX_PAGES = 10
#: Automatic attempts before a failed publication waits for an explicit call.
MAX_AUTOMATIC_ATTEMPTS = 6
_API = "https://api.github.com"
_ZWSP = "\u200b"
_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

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

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


# --- configuration ---------------------------------------------------------


def platform_review_block(config: Any) -> dict[str, Any] | None:
    """The enabled opt-in block of a flow's ``git_clone_config``, or None."""
    block = config.get(CONFIG_KEY) if isinstance(config, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    return dict(block)


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
    unknown = set(block) - {"enabled", "action", "verdict_actions", "status"}
    if unknown:
        raise ValueError("platform_review has unsupported keys")
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


def validate_platform_review_flow(flow: Any) -> None:
    """Require a validated result with a closed, fully mapped verdict.

    Args:
        flow: Flow row or namespace.

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


def platform_review_capabilities(config: Any) -> list[str] | None:
    """The provider writes a flow's opt-in can cause, for identity snapshots.

    A CI identity records this list when it is provisioned. An identity whose
    stored list differs from the flow's current one stops authenticating, so
    turning the opt-in on, or widening it from comments to approvals or to a
    commit status, never silently upgrades an existing identity.

    Args:
        config: The flow's ``git_clone_config``.

    Returns:
        Sorted capability names, or None when the opt-in is off.
    """
    block = platform_review_block(config)
    if block is None:
        return None
    actions = {"comment", *(block.get("verdict_actions") or {}).values()}
    capabilities = [f"review:{action}" for action in actions]
    if _status_block(block) is not None:
        capabilities.append("status")
    return sorted(capabilities)


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


def plain_text(value: Any, limit: int, *, single_line: bool = False) -> str:
    """Render untrusted text so GitHub shows it as literal characters.

    The rule, in order:

    1. Anything that is not a string becomes the empty string.
    2. Control, format, surrogate, private-use and unassigned characters are
       dropped (this removes bidirectional overrides and zero-width
       characters); line breaks are kept, or turned into spaces for a
       single-line field.
    3. The text is cut to ``limit`` characters.
    4. ``&``, ``<`` and ``>`` become HTML entities, so no tag, comment or
       character reference survives.
    5. Every other ASCII punctuation character is backslash-escaped, so no
       Markdown construct (link, image, emphasis, heading, code, table,
       list, quote) and no line-leading ``/command`` survives.
    6. A zero-width space is inserted after ``@`` and ``#``, between a URL
       scheme's ``:`` and ``//``, inside ``www.`` and inside ``GH-<digit>``,
       so no mention, issue or pull request reference, closing keyword
       target or bare-URL autolink is recognised.

    Args:
        value: Model-written value.
        limit: Maximum number of source characters kept.
        single_line: Replace line breaks with spaces.

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
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    out: list[str] = []
    for index, char in enumerate(text):
        if char in _ENTITIES:
            out.append(_ENTITIES[char])
        elif char in _PUNCTUATION:
            out.append("\\" + char)
            if char in "@#" or (char == ":" and text[index + 1 : index + 3] == "//"):
                out.append(_ZWSP)
        else:
            out.append(char)
    escaped = "".join(out)
    escaped = re.sub(r"(?i)(www)(\\\.)", rf"\1{_ZWSP}\2", escaped)
    return re.sub(r"(?i)\b(GH\\-)(?=\d)", rf"\1{_ZWSP}", escaped)


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
    path: str | None
    raw_path: str | None
    line: int | None


def _first_text(source: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(source.get(name), str) and source[name].strip():
            return source[name]
    return None


def structured_findings(result: Mapping[str, Any]) -> tuple[list[Finding], int]:
    """Read ``findings`` from a validated result.

    Args:
        result: The stored, validated result document.

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
    findings: list[Finding] = []
    for item in entries[:MAX_FINDINGS]:
        raw_path = _first_text(item, ("path", "file"))
        line = item.get("line")
        findings.append(
            Finding(
                severity=plain_text(item.get("severity"), 32, single_line=True),
                title=plain_text(
                    item.get("title"), MAX_FINDING_TITLE_CHARS, single_line=True
                ),
                body=plain_text(
                    _first_text(item, ("body", "description", "message", "detail")),
                    MAX_FINDING_BODY_CHARS,
                ),
                path=plain_text(raw_path, 300, single_line=True) or None,
                raw_path=raw_path if isinstance(raw_path, str) else None,
                line=line if type(line) is int and 0 < line < 10**7 else None,
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
    severity = f"\\[{finding.severity}\\] " if finding.severity else ""
    title = finding.title or "Finding"
    where = ""
    if location and finding.path:
        where = f" ({finding.path}"
        where += f":{finding.line})" if finding.line else ")"
    return f"**{index}\\. {severity}{title}**{where}"


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
) -> tuple[str, list[dict[str, Any]]]:
    """Build the review body and inline comments from a validated result.

    Only ``verdict``, ``summary``, ``findings`` and ``checked`` are read. The
    target, the commit, the review action and every link come from the
    caller, never from the result.

    Args:
        result: The stored, validated result document.
        verdict: The verdict, already checked against the schema's enum.
        execution_id: Execution the review belongs to.
        head_sha: Reviewed commit.
        nonce: Random value of the publication row.
        inline: Diff lines per file; a finding becomes an inline comment only
            when its path and line are in it.

    Returns:
        ``(body, comments)`` for GitHub's "create a review".
    """
    findings, omitted = structured_findings(result)
    comments: list[dict[str, Any]] = []
    parts = [
        review_marker(execution_id, head_sha, nonce),
        f"### Preloop review: {plain_text(verdict, 64, single_line=True)}",
        f"Reviewed commit `{head_sha}`. This review covers that commit only.",
    ]
    summary = plain_text(result.get("summary"), MAX_SUMMARY_CHARS)
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
        block = _finding_heading(index, finding, location=True)
        if finding.body:
            block += "\n\n" + _quote(finding.body)
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
            text = _finding_heading(index, finding, location=False)
            if finding.body:
                text += "\n\n" + _quote(finding.body)
            comments.append(
                {
                    "path": finding.raw_path,
                    "line": finding.line,
                    "side": "RIGHT",
                    "body": text,
                }
            )
    hidden = total - rendered
    if hidden > 0:
        parts.append(
            f"{hidden} further finding(s) are not shown here; the complete "
            "result is on the execution."
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


def plan_publication(flow: Any, execution: Any) -> PublicationPlan:
    """Decide the writes from the flow configuration and the stored outcome.

    A review needs all of: a SUCCEEDED execution, a stored result that
    passed the schema gate at capture, the same result passing the flow's
    current schema now, and a verdict the schema admits. Anything else
    yields no review and, when the flow asks for it, the neutral
    ``incomplete_state`` status.

    Args:
        flow: The execution's flow.
        execution: A terminal execution.

    Returns:
        The plan. ``review_action`` and ``status_value`` may both be None.
    """
    block = platform_review_block(getattr(flow, "git_clone_config", None)) or {}
    status = _status_block(block)
    context = (status or {}).get("context", DEFAULT_STATUS_CONTEXT)
    result = getattr(execution, "result", None)
    stored = getattr(execution, "result_schema_validation", None)
    schema = getattr(flow, "result_schema", None)
    reason: str | None = None
    verdict: str | None = None
    if getattr(execution, "status", None) != "SUCCEEDED":
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
        else:
            verdict = candidate
    if verdict is None:
        incomplete = (status or {}).get("incomplete_state")
        return PublicationPlan(
            review_action=None,
            status_value=incomplete,
            status_context=context if incomplete else None,
            status_description=(
                "Review did not complete; no verdict for this commit"
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
            raise PlatformReviewError("provider_denied", retryable=code == 403)
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


def _verify_review(
    review: Any, *, review_id: str, head_sha: str, event: str, digest: str
) -> None:
    """Fail unless the provider holds exactly the review that was sent.

    Checks the actor (an App bot), the target commit, the submitted state and
    the content (digest of the body).
    """
    if (
        not isinstance(review, Mapping)
        or str(review.get("id")) != review_id
        or review.get("commit_id") != head_sha
        or review.get("state") != _SUBMITTED_STATES[event]
        or not isinstance(review.get("body"), str)
        or _digest(review["body"]) != digest
        or not _is_bot(review)
    ):
        raise PlatformReviewError("readback_mismatch")


async def _post_review(
    github: _GitHub,
    row: models.PlatformReviewPublication,
    *,
    binding: CiReviewBinding,
    execution: Any,
    plan: PublicationPlan,
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
                    "_verify": None,
                }
    body, comments = render_review(
        execution.result,
        verdict=plan.verdict,
        execution_id=execution.id,
        head_sha=binding.head_sha,
        nonce=row.nonce,
        inline=diff_lines(await github.files()),
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
        raise PlatformReviewError("readback_mismatch")
    return {
        "review_state": STATE_POSTED,
        "review_action": plan.review_action,
        "review_id": review_id,
        "_verify": {"event": event, "digest": _digest(body)},
    }


async def _post_status(
    github: _GitHub, *, execution: Any, plan: PublicationPlan
) -> dict[str, Any]:
    """Create the commit status on the reviewed commit and read it back."""
    payload = {
        "state": plan.status_value,
        "context": plan.status_context,
        "description": (plan.status_description or "")[:140],
        "target_url": execution_url(execution.id),
    }
    created = await github.create_status(payload)
    status_id = str(created.get("id")) if isinstance(created, Mapping) else ""
    if not status_id.isdecimal():
        raise PlatformReviewError("readback_mismatch")
    values = {
        "status_state": STATE_POSTED,
        "status_value": plan.status_value,
        "status_context": plan.status_context,
        "status_id": status_id,
    }
    found = [
        entry
        for entry in await github.statuses()
        if isinstance(entry, Mapping) and str(entry.get("id")) == status_id
    ]
    if (
        len(found) != 1
        or found[0].get("state") != plan.status_value
        or found[0].get("context") != plan.status_context
        or found[0].get("target_url") != payload["target_url"]
    ):
        raise _RecordedError("readback_mismatch", values)
    return values


class _RecordedError(PlatformReviewError):
    """A failure after the provider accepted a write that must be recorded."""

    def __init__(self, reason: str, values: dict[str, Any]) -> None:
        super().__init__(reason)
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


async def _publish_claimed(
    db: Session,
    row: models.PlatformReviewPublication,
    *,
    flow: models.Flow,
    execution: models.FlowExecution,
) -> None:
    """Run one attempt for a claimed row and record its outcome."""
    from preloop.services.read_only_review import validate_read_only_flow

    record = crud.crud_platform_review_publication.record
    try:
        binding = crud.crud_ci_execution.authorize_dispatch(db, execution=execution)
    except PermissionError:
        raise PlatformReviewError("admission_revoked") from None
    if (
        binding is None
        or binding.tracker_type != "github"
        or str(flow.id) != str(binding.flow_id)
        or str(flow.account_id) != str(binding.account_id)
        or (row.repository, row.pull_request, row.head_sha)
        != (binding.repository_slug.lower(), str(binding.pr_number), binding.head_sha)
    ):
        raise PlatformReviewError("binding_changed")
    try:
        validate_read_only_flow(flow)
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
    plan = plan_publication(flow, execution)
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
    failure: PlatformReviewError | None = None
    if want_review or want_status:
        url = f"https://github.com/{binding.repository_slug}.git"
        try:
            lease, has_status = await _mint(
                tracker, url, review=want_review, status=want_status
            )
        except LeaseRefusedError as error:
            if error.status_code == 422 and not want_review:
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
                if want_review:
                    try:
                        values = await _post_review(
                            github, row, binding=binding, execution=execution, plan=plan
                        )
                        posted = "_verify" in values
                        verify = values.pop("_verify", None)
                        row = record(db, row=row, **values)
                        if posted:
                            _record_receipt(db, row, binding=binding)
                        if verify is not None:
                            _verify_review(
                                await github.review(str(row.review_id)),
                                review_id=str(row.review_id),
                                head_sha=binding.head_sha,
                                event=verify["event"],
                                digest=verify["digest"],
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
    record(
        db,
        row=row,
        state=STATE_POSTED if wrote else STATE_SKIPPED,
        reason=row.review_reason
        or (row.status_reason if row.status_reason != "not_configured" else None),
        retryable=False,
        next_attempt_at=None,
        posted_at=datetime.now(timezone.utc).replace(tzinfo=None) if wrote else None,
    )


def _record_receipt(
    db: Session, row: models.PlatformReviewPublication, *, binding: CiReviewBinding
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
) -> dict[str, Any] | None:
    """Publish, or publish again, the stored result of a finished execution.

    No model runs. The call is idempotent: a write the publication row
    already records is never repeated, and a concurrent caller gets the
    current state instead of a second attempt.

    Args:
        db: Database session.
        account_id: Account that owns the execution.
        execution_id: Execution whose stored result is published.
        explicit: A person or the execution's own CI identity asked for it.
            Explicit calls also retry attempts that ended ``skipped`` or
            failed for a reason automatic retries do not repeat.

    Returns:
        The public projection of the publication, or None when the flow has
        not opted in and the call is not explicit.

    Raises:
        LookupError: The execution does not exist in the account.
        PlatformReviewRefusedError: An explicit call for an execution that cannot
            be published at all (opt-in off, not finished, no binding).
    """
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
    if platform_review_block(flow.git_clone_config) is None:
        if explicit:
            raise PlatformReviewRefusedError("platform_review_disabled")
        return None
    try:
        binding = CiReviewBinding.model_validate(execution.ci_review_binding)
    except ValidationError:
        if explicit:
            raise PlatformReviewRefusedError("no_review_binding") from None
        return None
    if execution.status not in TERMINAL_STATUSES:
        if explicit:
            raise PlatformReviewRefusedError("execution_not_finished")
        return None
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
        return platform_review_projection(db, execution, flow=flow)
    try:
        await _publish_claimed(db, row, flow=flow, execution=execution)
    except PlatformReviewError as error:
        _fail(db, execution.id, error.reason, error.retryable)
    except Exception:
        # A posting failure never reaches the execution's status or result.
        logger.exception("Platform review publication failed for %s", execution.id)
        _fail(db, execution.id, "internal_error", True)
    return platform_review_projection(db, execution, flow=flow)


def _fail(db: Session, execution_id: Any, reason: str, retryable: bool) -> None:
    store = crud.crud_platform_review_publication
    try:
        db.rollback()
        row = store.get_for_execution(db, execution_id=execution_id)
        if row is None:
            return
        again = retryable and row.attempts < MAX_AUTOMATIC_ATTEMPTS
        store.record(
            db,
            row=row,
            state=STATE_FAILED,
            reason=reason[:64],
            retryable=again,
            next_attempt_at=_backoff(row.attempts) if again else None,
        )
    except Exception:
        db.rollback()
        logger.exception("Could not record the publication failure of %s", execution_id)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def platform_review_projection(
    db: Session, execution: Any, *, flow: Any = None
) -> dict[str, Any] | None:
    """What was posted for an execution, safe for its restricted CI identity.

    Args:
        db: Database session.
        execution: Flow execution row.
        flow: The execution's flow, when the caller holds it.

    Returns:
        None when the flow has not opted in and nothing was ever claimed;
        ``{"status": "pending", ...}`` before the first attempt; otherwise
        the recorded state. Contains no credential and no provider response.
    """
    row = crud.crud_platform_review_publication.get_for_execution(
        db, execution_id=execution.id
    )
    if row is None:
        if flow is None:
            flow = getattr(execution, "flow", None)
        if platform_review_block(getattr(flow, "git_clone_config", None)) is None:
            return None
        binding = getattr(execution, "ci_review_binding", None)
        return {
            "status": "pending",
            "reason": None,
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
        }
    return {
        "status": row.state,
        "reason": row.reason,
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
    }


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
        targets = store.due_execution_ids(db=session, limit=limit)
        targets += store.unclaimed_execution_ids(
            db=session, finished_within=timedelta(hours=24), limit=limit
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
                logger.exception("Platform review sweep failed for %s", execution_id)
    finally:
        session.close()
    return attempted
