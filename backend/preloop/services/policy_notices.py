"""Policy notices: model I/O rules with the ``notify`` action (#959).

A notify rule never blocks, parks or buffers a model call. When one matches,
the evaluator hands a :class:`PolicyNotice` to :func:`schedule_policy_notice`,
which records a hit and (at most once per rule, user and hour) tells the
policy owners on the channels the account already uses for approvals. The
request thread only computes the excerpt; the database write and the delivery
run on the shared DB thread pool.

What is stored: account, user, target, rule id and description, the SHA-256
of the scanned text, and an excerpt of at most 280 characters around the
match with the gateway's log secret scrubbing applied. Never the full prompt
or completion. When scrubbing fails the excerpt is dropped and only the hash
and rule id are kept.

The weekly digest is rendered by the ``optimization_digest`` plugin, which is
not in this repository. It adds the "Policy notices" section by calling
:func:`build_policy_notice_digest_section` and rendering the result with
:meth:`PolicyNoticeDigestSection.render_text` or
:meth:`PolicyNoticeDigestSection.render_html`.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_policy_notice_hit
from preloop.models.crud.policy_notice_hit import PolicyNoticeRuleSummary
from preloop.models.models.policy_notice_hit import POLICY_NOTICE_EXCERPT_MAX_CHARS
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

#: At most one outbound message per rule, per user, per this window.
NOTIFY_DEBOUNCE = timedelta(hours=1)

#: Window of the Attention card and the digest section.
NOTICE_SUMMARY_WINDOW = timedelta(days=7)

#: Characters scrubbed on each side of the match before the excerpt is cut.
#: Scrubbing a bounded window keeps the cost flat on very long prompts.
_SCRUB_MARGIN = 4096

#: How far the scrub window may grow to reach whitespace, so a credential is
#: never cut in half before it is scrubbed.
_SNAP_LIMIT = 8192

#: Bound on the regex matches walked to find the last occurrence.
_MAX_REGEX_MATCHES = 1000

#: Longest pattern re-run to locate the match (same cap as the evaluator).
_MAX_PATTERN_LEN = 512

#: How much of the end of the text is scanned for the newest regex match.
#: Beyond it only the first match is looked for, which stops early.
_REGEX_TAIL_CHARS = 16 * 1024

_CONTAINS_CALL = re.compile(r"\.contains\s*\(\s*(['\"])(.+?)\1\s*\)")
_MATCHES_CALL = re.compile(r"\.matches\s*\(\s*(['\"])((?:(?!\1)[^\\]|\\.)*)\1\s*\)")
_STRING_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_WHITESPACE = re.compile(r"\s")


@dataclass(frozen=True)
class PolicyNotice:
    """One notify match, as handed from the evaluator to the recorder."""

    account_id: UUID
    user_id: Optional[UUID]
    target: str
    rule_id: str
    rule_description: Optional[str]
    text_sha256: str
    excerpt: Optional[str]
    approval_workflow: Optional[str] = None


@dataclass
class PolicyNoticeOutcome:
    """What :func:`record_policy_notice` did with one notice."""

    hit_id: UUID
    notified: bool
    delivery: Dict[str, Any] = field(default_factory=dict)


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", r"\1", literal)


def _last_literal(text: str, literal: str) -> Optional[Tuple[int, int]]:
    if not literal:
        return None
    index = text.rfind(literal)
    if index < 0:
        return None
    return index, index + len(literal)


def _last_regex(text: str, pattern: str) -> Optional[Tuple[int, int]]:
    if not pattern or len(pattern) > _MAX_PATTERN_LEN:
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    # Look for the newest match in the tail only, so the request thread does
    # not run an admin-supplied pattern to the end of a long text. A match
    # that starts before the tail (or straddles its edge) falls back to the
    # first match, which ``search`` finds without scanning further.
    offset = max(0, len(text) - _REGEX_TAIL_CHARS)
    span: Optional[Tuple[int, int]] = None
    for count, match in enumerate(compiled.finditer(text, offset)):
        span = match.span()
        if count >= _MAX_REGEX_MATCHES:
            break
    if span is not None or offset == 0:
        return span
    first = compiled.search(text)
    return first.span() if first else None


def locate_match(text: str, expression: Optional[str]) -> Optional[Tuple[int, int]]:
    """Best-effort span of what the condition matched in ``text``.

    ``contains('...')`` and ``matches('...')`` are located directly. Any
    other string literal in the expression (CEL) is tried as a substring.
    The last occurrence wins: in a conversation the newest message is at the
    end. Conditions with no locatable literal (for example ``pii.found``)
    return None and the excerpt starts at the beginning of the text.

    Args:
        text: Scanned request or response text.
        expression: The matching condition expression.

    Returns:
        ``(start, end)`` offsets, or None.
    """
    if not text or not expression:
        return None
    for match in _CONTAINS_CALL.finditer(expression):
        span = _last_literal(text, _unescape(match.group(2)))
        if span:
            return span
    for match in _MATCHES_CALL.finditer(expression):
        span = _last_regex(text, _unescape(match.group(2)))
        if span:
            return span
    for match in _STRING_LITERAL.finditer(expression):
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        span = _last_literal(text, _unescape(literal or ""))
        if span:
            return span
    return None


def _snap_left(text: str, index: int) -> int:
    """Move ``index`` left to a whitespace boundary (bounded)."""
    if index <= 0:
        return 0
    floor = max(0, index - _SNAP_LIMIT)
    segment = text[floor:index]
    for position in range(len(segment) - 1, -1, -1):
        if segment[position].isspace():
            return floor + position
    return floor


def _snap_right(text: str, index: int) -> int:
    """Move ``index`` right to a whitespace boundary (bounded)."""
    if index >= len(text):
        return len(text)
    match = _WHITESPACE.search(text, index, min(len(text), index + _SNAP_LIMIT))
    return match.start() if match else min(len(text), index + _SNAP_LIMIT)


def build_excerpt(
    text: str,
    expression: Optional[str],
    *,
    max_chars: int = POLICY_NOTICE_EXCERPT_MAX_CHARS,
    scrub: Callable[[Optional[str]], Optional[str]] = scrub_secrets,
) -> Optional[str]:
    """Secret-scrubbed excerpt of at most ``max_chars`` around the match.

    The window around the match is scrubbed first and cut second, so a
    credential is replaced before any cut could leave an unrecognisable
    fragment of it behind. Whitespace runs are collapsed to one space.

    Args:
        text: Scanned request or response text.
        expression: The matching condition expression.
        max_chars: Excerpt length cap.
        scrub: Redaction function (injectable for tests).

    Returns:
        The excerpt, or None when redaction failed or there is no text.
    """
    if not text:
        return None
    try:
        span = locate_match(text, expression) or (0, 0)
        start, end = span
        lo = _snap_left(text, max(0, start - _SCRUB_MARGIN))
        hi = _snap_right(text, min(len(text), end + _SCRUB_MARGIN))
        window = text[lo:hi]
        scrubbed = scrub(window)
        if scrubbed is None:
            return None
        matched = text[start:end]
        anchor = scrubbed.rfind(matched) if matched else -1
        if anchor < 0:
            anchor = min(start - lo, len(scrubbed))
            matched_len = 0
        else:
            matched_len = len(matched)
        center = anchor + matched_len // 2
        cut_start = max(0, center - max_chars // 2)
        cut_end = min(len(scrubbed), cut_start + max_chars)
        cut_start = max(0, cut_end - max_chars)
        excerpt = " ".join(scrubbed[cut_start:cut_end].split())
        return excerpt[:max_chars] or None
    except Exception:  # noqa: BLE001 - fall back to hash and rule id only
        logger.warning("Policy notice excerpt redaction failed", exc_info=True)
        return None


def record_policy_notice(
    db: Session,
    notice: PolicyNotice,
    *,
    now: Optional[datetime] = None,
    deliver: Optional[Callable[[Session, models.PolicyNoticeHit], Dict]] = None,
) -> PolicyNoticeOutcome:
    """Write the hit, then send the notice unless one went out this hour.

    The hit is committed before the debounce claim so it is counted even when
    delivery fails. The claim is committed before delivery so a delivery
    failure cannot turn into one message per request.

    Args:
        db: Database session.
        notice: The match to record.
        now: Injected clock for tests.
        deliver: Delivery function (defaults to the approval channels).

    Returns:
        The hit id and whether a notice was sent.
    """
    hit = crud_policy_notice_hit.record(
        db,
        account_id=notice.account_id,
        user_id=notice.user_id,
        target=notice.target,
        rule_id=notice.rule_id,
        rule_description=notice.rule_description,
        text_sha256=notice.text_sha256,
        excerpt=notice.excerpt,
        now=now,
    )
    db.commit()
    claimed = crud_policy_notice_hit.claim_notification(
        db, hit=hit, window=NOTIFY_DEBOUNCE, now=now
    )
    db.commit()
    outcome = PolicyNoticeOutcome(hit_id=hit.id, notified=claimed)
    if not claimed:
        return outcome
    if deliver is None:
        from preloop.services.policy_notice_delivery import deliver_policy_notice

        def deliver(session: Session, row: models.PolicyNoticeHit) -> Dict:
            return deliver_policy_notice(
                session, row, approval_workflow=notice.approval_workflow
            )

    try:
        outcome.delivery = deliver(db, hit)
    except Exception:  # noqa: BLE001 - a notice must never raise upstream
        logger.exception("Policy notice delivery failed for rule %s", hit.rule_id)
        outcome.delivery = {"error": "delivery_failed"}
    return outcome


def process_policy_notice(notice: PolicyNotice) -> Optional[PolicyNoticeOutcome]:
    """Record one notice in a short-lived session. Never raises."""
    from preloop.models.db.session import _safe_close_db_session, get_db_session

    db = next(get_db_session())
    try:
        return record_policy_notice(db, notice)
    except Exception:  # noqa: BLE001 - background work, log and drop
        logger.exception("Failed to record policy notice for rule %s", notice.rule_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        _safe_close_db_session(db)


def schedule_policy_notice(notice: PolicyNotice) -> None:
    """Record and deliver ``notice`` off the request thread.

    The caller's model call continues immediately; nothing here can block,
    park or fail it.
    """
    try:
        from preloop.services.db_executor import submit_off_loop

        submit_off_loop(lambda: process_policy_notice(notice))
    except Exception:  # noqa: BLE001 - scheduling must never fail the call
        logger.exception("Could not schedule policy notice for %s", notice.rule_id)


def summarize_policy_notices(
    db: Session,
    account_id: Any,
    *,
    now: Optional[datetime] = None,
    window: timedelta = NOTICE_SUMMARY_WINDOW,
) -> List[PolicyNoticeRuleSummary]:
    """Per-rule notice counts over the last ``window`` (default 7 days)."""
    moment = now or datetime.now(timezone.utc)
    return crud_policy_notice_hit.summarize_by_rule(
        db, account_id=account_id, since=moment - window
    )


@dataclass(frozen=True)
class PolicyNoticeDigestSection:
    """The "Policy notices" section of the weekly digest."""

    title: str
    window_days: int
    rows: Sequence[PolicyNoticeRuleSummary]

    @property
    def is_empty(self) -> bool:
        """True when no notify rule matched in the window."""
        return not self.rows

    def render_text(self) -> str:
        """Plain-text section: rule, count, last user, last excerpt."""
        lines = [self.title, ""]
        if self.is_empty:
            lines.append(f"No notify rule matched in the last {self.window_days} days.")
            return "\n".join(lines)
        for row in self.rows:
            noun = "hit" if row.count == 1 else "hits"
            lines.append(f"- {row.rule_id}: {row.count} {noun}")
            lines.append(f"  Last user: {row.last_username or 'unknown'}")
            lines.append(f"  Last excerpt: {row.last_excerpt or '(not available)'}")
        return "\n".join(lines)

    def render_html(self) -> str:
        """HTML table for the digest email. Every value is escaped."""
        heading = f"<h3>{html.escape(self.title)}</h3>"
        if self.is_empty:
            return (
                f"{heading}<p>No notify rule matched in the last "
                f"{self.window_days} days.</p>"
            )
        body = "".join(
            "<tr>"
            f"<td>{html.escape(row.rule_id)}</td>"
            f"<td>{row.count}</td>"
            f"<td>{html.escape(row.last_username or 'unknown')}</td>"
            f"<td>{html.escape(row.last_excerpt or '(not available)')}</td>"
            "</tr>"
            for row in self.rows
        )
        return (
            f"{heading}<table><thead><tr><th>Rule</th><th>Count</th>"
            "<th>Last user</th><th>Last excerpt</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )


def build_policy_notice_digest_section(
    db: Session,
    account_id: Any,
    *,
    now: Optional[datetime] = None,
) -> PolicyNoticeDigestSection:
    """Data for the digest's "Policy notices" section (last 7 days).

    Called by the ``optimization_digest`` plugin. Kept here so the section
    reads the table this repository owns and can be tested without it.

    Args:
        db: Database session.
        account_id: Account the digest is for.
        now: Injected clock for tests.

    Returns:
        The section, possibly empty.
    """
    return PolicyNoticeDigestSection(
        title="Policy notices",
        window_days=NOTICE_SUMMARY_WINDOW.days,
        rows=summarize_policy_notices(db, account_id, now=now),
    )
