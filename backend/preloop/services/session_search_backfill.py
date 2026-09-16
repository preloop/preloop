"""Backfill the session search corpus from history already on disk.

Indexing on write (see :mod:`preloop.services.session_search_index`) covers
new sessions only, so on the day search ships an account can find nothing
older than the deploy. This sweeper walks an account's runtime sessions newest
first and derives chunks from the sources that already exist: the gateway
interaction corpus joined through usage rows, and tool call activity rows.
Nothing new is captured and no chunk shape changes; the same writers that run
on the request path run here against older rows.

It is bounded the way the retention purge is bounded, and for the same reason:
a busy account's history is large and a backfill must never compete with the
request path. Off by default, a row budget per pass and per account, a wall
clock budget, its own interval, and a per account watermark so a pass that
stops halfway is resumed rather than restarted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import (
    crud_gateway_usage_search_document,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_session_search_backfill_state,
    crud_session_search_document,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.session_search_document import (
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_TOOL_CALL,
)
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)
from preloop.services.session_search_index import (
    index_tool_call,
    indexing_enabled,
    write_source_chunks,
)

logger = logging.getLogger(__name__)

#: Sessions read per query while walking one account. Small on purpose: the
#: pass is bounded by rows written, not by how much it can hold in memory.
SESSION_PAGE_SIZE = 50
#: Source rows read per query inside one session.
SOURCE_PAGE_SIZE = 100

#: Backfill state values reported with the indexed through value.
BACKFILL_STATE_NOT_STARTED = "not_started"
BACKFILL_STATE_IN_PROGRESS = "in_progress"
BACKFILL_STATE_COMPLETE = "complete"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return an aware UTC datetime so stored and fresh values compare."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def backfill_enabled() -> bool:
    """Return whether the backfill sweeper may run at all."""
    return bool(getattr(settings, "session_search_backfill_enabled", False))


@dataclass
class IndexedThrough:
    """How far back session search currently reaches for one account.

    ``indexed_through`` is the oldest point in time the corpus covers without
    gaps: the backfill watermark while the walk is in progress, and the oldest
    chunk the account holds once it finished. ``None`` means the account has
    no indexed content at all.
    """

    indexed_through: Optional[datetime] = None
    complete: bool = False
    state: str = BACKFILL_STATE_NOT_STARTED

    def as_dict(self) -> Dict[str, Any]:
        """Return the value in the shape a search response carries."""
        return {
            "indexed_through": (
                self.indexed_through.isoformat() if self.indexed_through else None
            ),
            "backfill_complete": self.complete,
            "backfill_state": self.state,
        }


@dataclass
class AccountBackfillResult:
    """What one pass achieved for one account."""

    account_id: str
    sessions_scanned: int = 0
    sources_indexed: int = 0
    rows_written: int = 0
    completed: bool = False
    budget_exhausted: bool = False
    watermark: Optional[datetime] = None


@dataclass
class BackfillPassResult:
    """Totals for one pass over every account that still needs one."""

    accounts: int = 0
    sessions_scanned: int = 0
    sources_indexed: int = 0
    rows_written: int = 0
    accounts_completed: int = 0
    accounts_failed: int = 0
    budget_exhausted: bool = False
    skipped_reason: Optional[str] = None
    results: List[AccountBackfillResult] = field(default_factory=list)

    def record(self, result: AccountBackfillResult) -> None:
        """Fold one account's result into the totals."""
        self.accounts += 1
        self.sessions_scanned += result.sessions_scanned
        self.sources_indexed += result.sources_indexed
        self.rows_written += result.rows_written
        if result.completed:
            self.accounts_completed += 1
        if result.budget_exhausted:
            self.budget_exhausted = True
        self.results.append(result)

    def as_dict(self) -> Dict[str, Any]:
        """Return a log friendly summary."""
        return {
            "accounts": self.accounts,
            "sessions_scanned": self.sessions_scanned,
            "sources_indexed": self.sources_indexed,
            "rows_written": self.rows_written,
            "accounts_completed": self.accounts_completed,
            "accounts_failed": self.accounts_failed,
            "budget_exhausted": self.budget_exhausted,
            "skipped_reason": self.skipped_reason,
        }


def indexed_through_for_account(db: Session, *, account_id: Any) -> IndexedThrough:
    """Return how far back search reaches for one account.

    Consumed by the search endpoint so the interface can say what the result
    set covers instead of implying it covers everything.
    """
    state = crud_session_search_backfill_state.get_for_account(
        db, account_id=account_id
    )
    earliest = _as_utc(
        crud_session_search_document.earliest_occurred_at(db, account_id=account_id)
    )
    if state is None:
        return IndexedThrough(
            indexed_through=earliest,
            complete=False,
            state=BACKFILL_STATE_NOT_STARTED,
        )
    watermark = _as_utc(state.cursor_started_at)
    if state.completed_at is not None:
        # The walk reached the end of the retained history, so the oldest
        # chunk on disk is the honest answer: there is nothing older to find.
        return IndexedThrough(
            indexed_through=earliest or watermark,
            complete=True,
            state=BACKFILL_STATE_COMPLETE,
        )
    candidates = [value for value in (watermark, earliest) if value is not None]
    return IndexedThrough(
        indexed_through=min(candidates) if candidates else None,
        complete=False,
        state=(
            BACKFILL_STATE_IN_PROGRESS
            if watermark is not None
            else BACKFILL_STATE_NOT_STARTED
        ),
    )


def _count_new_rows(before: Sequence[Any], stored: Sequence[Any]) -> int:
    """Return how many of the stored chunks did not exist before the write.

    The corpus write is idempotent on the content hash and hands back the
    rows that are now on disk, unchanged ones included. Counting only the ids
    that are new is what keeps "rows written" honest, and keeps a re-walk of
    an already indexed session from eating the pass budget twice.
    """
    known = {row.id for row in before}
    return len([row for row in stored if row.id not in known])


def _index_gateway_document(
    db: Session,
    *,
    usage: Any,
    document: Any,
) -> int:
    """Write one already indexed gateway interaction into the corpus.

    The text comes from the gateway corpus row, which was sanitised when it
    was written, so the backfill never re-reads a payload and never applies a
    second redaction pass. Writing the same text the on write path would write
    is what makes a second pass a no op.
    """
    meta_data = dict(document.meta_data or {})
    content_captured = bool(
        meta_data.get("request_payload_present")
        or meta_data.get("response_payload_present")
    )
    before = crud_session_search_document.list_for_source(
        db,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=str(usage.id),
    )
    stored = write_source_chunks(
        db,
        account_id=usage.account_id,
        runtime_session_id=usage.runtime_session_id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
        text=document.searchable_text or "",
        occurred_at=_as_utc(usage.timestamp),
        role="assistant",
        content_captured=content_captured,
        already_sanitised=True,
        meta_data=meta_data,
        model_alias=usage.model_alias,
        provider_name=usage.provider_name,
        runtime_principal_id=usage.runtime_principal_id,
        api_key_id=usage.api_key_id,
        flow_id=usage.flow_id,
        status=str(usage.status_code) if usage.status_code is not None else None,
        existing=before,
    )
    return _count_new_rows(before, stored)


def _index_tool_call_activity(db: Session, *, activity: Any) -> int:
    """Write one historical tool call into the corpus."""
    before = crud_session_search_document.list_for_source(
        db,
        source_kind=SOURCE_KIND_TOOL_CALL,
        source_id=str(activity.id),
    )
    stored = index_tool_call(
        db,
        account_id=activity.account_id,
        runtime_session_id=activity.runtime_session_id,
        source_id=activity.id,
        server_name=activity.server_name,
        tool_name=activity.tool_name,
        status=activity.status,
        summary=activity.summary,
        occurred_at=_as_utc(activity.timestamp),
        api_key_id=activity.api_key_id,
        meta_data={"activity_type": activity.activity_type},
        existing=before,
    )
    return _count_new_rows(before, stored)


def backfill_session(
    db: Session,
    *,
    session: RuntimeSession,
    row_budget: int,
    deadline: Optional[float] = None,
) -> Tuple[int, int, bool]:
    """Index one session's existing sources, newest source first.

    Returns ``(sources_indexed, rows_written, finished)``. The budget is
    checked between sources, never inside one, so a source is either fully
    chunked or not chunked at all. ``finished`` is False when the budget ran
    out mid session: the caller leaves the watermark where it is, and the next
    pass re-walks this session, which costs reads but writes nothing for the
    sources already stored.
    """
    sources = 0
    rows = 0
    remaining = row_budget

    cursor: Optional[Tuple[datetime, Any]] = None
    while True:
        page = crud_gateway_usage_search_document.list_session_documents_page(
            db,
            account_id=session.account_id,
            runtime_session_id=session.id,
            before_timestamp=cursor[0] if cursor else None,
            before_api_usage_id=cursor[1] if cursor else None,
            limit=SOURCE_PAGE_SIZE,
        )
        if not page:
            break
        for usage, document in page:
            if remaining <= 0 or _out_of_time(deadline):
                return sources, rows, False
            written = _index_gateway_document(db, usage=usage, document=document)
            sources += 1
            rows += written
            remaining -= written
            cursor = (usage.timestamp, usage.id)
        if len(page) < SOURCE_PAGE_SIZE:
            break

    cursor = None
    while True:
        activities = crud_runtime_session_activity.list_tool_calls_page(
            db,
            account_id=session.account_id,
            runtime_session_id=session.id,
            before_timestamp=cursor[0] if cursor else None,
            before_activity_id=cursor[1] if cursor else None,
            limit=SOURCE_PAGE_SIZE,
        )
        if not activities:
            break
        for activity in activities:
            if remaining <= 0 or _out_of_time(deadline):
                return sources, rows, False
            written = _index_tool_call_activity(db, activity=activity)
            sources += 1
            rows += written
            remaining -= written
            cursor = (activity.timestamp, activity.id)
        if len(activities) < SOURCE_PAGE_SIZE:
            break

    return sources, rows, True


def _out_of_time(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def backfill_account(
    db: Session,
    *,
    account_id: Any,
    row_budget: int,
    now: Optional[datetime] = None,
    horizon: Optional[datetime] = None,
    deadline: Optional[float] = None,
    commit: bool = True,
) -> Optional[AccountBackfillResult]:
    """Walk one account's sessions newest first, within the budget.

    Resumes from the stored watermark and advances it only past sessions that
    were indexed in full, so a pass that stops halfway is continued by the
    next one rather than started again.

    Returns ``None`` when another replica already holds this account's
    backfill row. That is not a failure: the holder is walking it.
    """
    stamp = now or _now()
    result = AccountBackfillResult(account_id=str(account_id))
    state = crud_session_search_backfill_state.lock_for_account(
        db, account_id=account_id
    )
    if state is None:
        return None
    if state.completed_at is not None:
        # Nothing to walk: a finished account costs one indexed read.
        result.completed = True
        result.watermark = _as_utc(state.cursor_started_at)
        return result

    cursor_started_at = state.cursor_started_at
    cursor_session_id = state.cursor_session_id
    remaining = max(0, int(row_budget))
    completed = False

    while True:
        if remaining <= 0 or _out_of_time(deadline):
            result.budget_exhausted = True
            break
        page = crud_runtime_session.list_for_search_backfill(
            db,
            account_id=account_id,
            before_started_at=cursor_started_at,
            before_session_id=cursor_session_id,
            not_before=horizon,
            limit=SESSION_PAGE_SIZE,
        )
        if not page:
            # The walk reached the end of the retained history for this
            # account, which is what completion means here.
            completed = True
            break
        for session in page:
            if remaining <= 0 or _out_of_time(deadline):
                result.budget_exhausted = True
                break
            sources, rows, finished = backfill_session(
                db,
                session=session,
                row_budget=remaining,
                deadline=deadline,
            )
            result.sources_indexed += sources
            result.rows_written += rows
            remaining -= rows
            if not finished:
                result.budget_exhausted = True
                break
            result.sessions_scanned += 1
            cursor_started_at = session.started_at
            cursor_session_id = session.id
        if result.budget_exhausted:
            break

    state = crud_session_search_backfill_state.record_pass(
        db,
        account_id=account_id,
        cursor_started_at=cursor_started_at,
        cursor_session_id=cursor_session_id,
        sessions_scanned=result.sessions_scanned,
        rows_written=result.rows_written,
        completed=completed,
        now=stamp,
        commit=commit,
    )
    result.completed = state.completed_at is not None
    result.watermark = _as_utc(state.cursor_started_at)
    return result


def run_session_search_backfill(
    db: Session,
    *,
    now: Optional[datetime] = None,
    account_ids: Optional[Sequence[Any]] = None,
    ignore_enabled: bool = False,
) -> BackfillPassResult:
    """One bounded pass over the accounts that still need a backfill.

    Returns totals rather than raising: the caller is a background loop, and
    one account that cannot be walked must not cost every other account its
    pass.
    """
    stamp = now or _now()
    summary = BackfillPassResult()
    if not (ignore_enabled or backfill_enabled()):
        summary.skipped_reason = "disabled"
        return summary
    if not indexing_enabled():
        # The corpus itself is switched off, so there is nothing to write to.
        summary.skipped_reason = "corpus_disabled"
        return summary

    row_budget = max(1, int(settings.session_search_backfill_max_rows_per_pass))
    account_budget = max(1, int(settings.session_search_backfill_max_rows_per_account))
    deadline = time.monotonic() + max(
        1, int(settings.session_search_backfill_max_seconds)
    )
    max_age_days = max(0, int(settings.session_search_backfill_max_age_days))
    horizon = stamp - timedelta(days=max_age_days) if max_age_days else None

    remaining = row_budget
    for account_id in crud_session_search_backfill_state.pending_account_ids(
        db, account_ids=account_ids
    ):
        if remaining <= 0 or _out_of_time(deadline):
            summary.budget_exhausted = True
            break
        try:
            result = backfill_account(
                db,
                account_id=account_id,
                row_budget=min(remaining, account_budget),
                now=stamp,
                horizon=horizon,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 - one account must not stop the pass
            db.rollback()
            summary.accounts_failed += 1
            logger.error(
                "Session search backfill failed for account %s",
                account_id,
                exc_info=True,
            )
            try:
                crud_session_search_backfill_state.record_error(
                    db,
                    account_id=account_id,
                    error=f"{type(exc).__name__}: {exc}",
                    now=stamp,
                    commit=True,
                )
            except Exception:
                db.rollback()
                logger.error(
                    "Session search backfill could not record the failure for "
                    "account %s",
                    account_id,
                    exc_info=True,
                )
            continue
        if result is None:
            continue
        summary.record(result)
        remaining -= result.rows_written
    if summary.rows_written or summary.accounts_failed:
        logger.info("Session search backfill pass: %s", summary.as_dict())
    return summary


class SessionSearchBackfillSweeper:
    """Periodic asyncio backfill pass, modeled on the retention purge sweeper.

    Started from the app lifespan on the API role only, and only when the
    backfill is enabled. The database work is synchronous CRUD, so each pass
    runs in a thread and the event loop stays responsive.
    """

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        self.check_interval = int(
            check_interval_seconds
            if check_interval_seconds is not None
            else settings.session_search_backfill_interval_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        """Whether a sweep loop is live."""
        return self._running

    async def start(self) -> None:
        """Start the backfill background task, if it is allowed to run."""
        if self._running:
            logger.warning("Session search backfill sweeper is already running")
            return
        if not backfill_enabled():
            # Off by default: reaching back through every account's history is
            # a decision an operator makes, not something an upgrade starts.
            logger.info(
                "Session search backfill sweeper not started (disabled by setting)."
            )
            return
        if not background_passes_allowed():
            # See preloop.services.service_roles: a pod that relays model
            # responses does not also walk every account's history.
            logger.info(
                "Session search backfill sweeper not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            "Session search backfill sweeper started (check_interval=%ss)",
            self.check_interval,
        )

    async def stop(self) -> None:
        """Stop the backfill background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the sweep loop task.
                pass

    async def _sweep_loop(self) -> None:
        """Wait one interval first, then backfill on every tick.

        Sleeping first keeps a crash loop from turning into a walk of every
        account's history on every restart.
        """
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._sweep_once)
            except Exception:
                logger.error("Error in session search backfill pass", exc_info=True)

    @staticmethod
    def _sweep_once() -> None:
        """One synchronous pass with its own session."""
        db = next(get_db_session())
        try:
            run_session_search_backfill(db)
        finally:
            db.close()


_sweeper_instance: Optional[SessionSearchBackfillSweeper] = None


def get_session_search_backfill_sweeper() -> SessionSearchBackfillSweeper:
    """Get or create the global session search backfill sweeper."""
    global _sweeper_instance
    if _sweeper_instance is None:
        _sweeper_instance = SessionSearchBackfillSweeper()
    return _sweeper_instance


def reset_session_search_backfill_sweeper() -> None:
    """Drop the global sweeper, so a test can build one with fresh settings."""
    global _sweeper_instance
    _sweeper_instance = None
