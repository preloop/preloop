"""Live activity persistence: rollups, presence state, watermarks (#1889).

The only writer is the extension plugin's worker; the console board reads
through the bounded readers here. Two groups of functions:

* **Board readers** touch only ``activity_rollup``, ``presence_state``,
  ``activity_settings`` and ``activity_worker_watermark`` plus the small
  directory tables (``user``, ``managed_agent``, ``runtime_session``). They
  never scan ``api_usage``, ``runtime_session_activity`` or
  ``approval_request``; a test pins that.
* **Source readers** are the worker's incremental, time-bounded reads of the
  raw tables, ``WHERE timestamp > watermark AND timestamp <= until`` with a
  row limit, each on an index that leads with that timestamp.

Callers own the transaction unless a function says otherwise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, delete, func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.live_activity import LATENCY_BUCKETS

ActivityRollup = models.ActivityRollup
PresenceState = models.PresenceState
ActivityWorkerWatermark = models.ActivityWorkerWatermark
ActivitySettings = models.ActivitySettings

WORKER_LOCK_KEY = "live_activity_worker"
MAINTENANCE_LOCK_KEY = "live_activity_maintenance"
HOOK_TURN_TYPES = ("user_prompt", "turn_end", "session_end")
CONSOLE_EVENT_TYPES = ("session_start", "page_view")
REPLAY_PURPOSE = "replay_validation"
MAX_SOURCE_ROWS = 5000
MAX_BOARD_ROWS = 20000

_COUNT_COLUMNS = (
    "requests",
    "tool_calls",
    "prompts",
    "turn_ends",
    "approvals_requested",
    "approvals_decided",
    "prompt_tokens",
    "completion_tokens",
    "active_seconds",
)


def empty_histogram() -> list[int]:
    """A latency histogram with every bucket at zero."""
    return [0] * LATENCY_BUCKETS


@dataclass
class RollupDelta:
    """Additive counts for one rollup row, folded by the worker."""

    account_id: uuid.UUID
    granularity: int
    bucket_start: datetime
    subject_type: str
    subject_id: str
    requests: int = 0
    tool_calls: int = 0
    prompts: int = 0
    turn_ends: int = 0
    approvals_requested: int = 0
    approvals_decided: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    active_seconds: int = 0
    spend_usd: Optional[Decimal] = None
    reply_latency_hist: list[int] = field(default_factory=empty_histogram)
    approval_latency_hist: list[int] = field(default_factory=empty_histogram)

    def key(self) -> tuple[Any, ...]:
        """The unique key of the row this delta lands on."""
        return (
            self.account_id,
            self.granularity,
            self.subject_type,
            self.subject_id,
            self.bucket_start,
        )


@dataclass(frozen=True)
class PresenceUpdate:
    """The state the worker derived for one subject."""

    subject_type: str
    subject_id: str
    state: str
    last_signal_at: Optional[datetime] = None
    last_signal_kind: Optional[str] = None
    awaiting_reply_since: Optional[datetime] = None
    awaiting_approval_count: int = 0
    runtime_session_id: Optional[uuid.UUID] = None
    device_key: Optional[str] = None


def _hist_sum(column: str) -> Any:
    """Element-wise sum of the stored and the incoming histogram."""
    return literal_column(
        "ARRAY(SELECT COALESCE(activity_rollup.{c}[i], 0) + "
        "COALESCE(excluded.{c}[i], 0) FROM generate_series(1, {n}) AS i)"
        "::smallint[]".format(c=column, n=LATENCY_BUCKETS)
    )


class CRUDLiveActivity:
    """Rollup, presence and watermark persistence for the live activity board."""

    # --- worker lock ------------------------------------------------------

    def try_worker_lock(self, db: Session, *, key: str = WORKER_LOCK_KEY) -> bool:
        """Take a transaction-scoped advisory lock that elects one worker.

        Released when the caller's transaction ends, so a pass holds it for
        exactly as long as its writes. Maintenance batches use their own key
        (:data:`MAINTENANCE_LOCK_KEY`) so they never block passes.

        Args:
            db: Database session.
            key: Lock name.

        Returns:
            True when this connection now holds the lock.
        """
        return bool(
            db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": key},
            ).scalar()
        )

    def set_statement_timeout(self, db: Session, *, milliseconds: int) -> None:
        """Bound every statement of the current transaction."""
        db.execute(text(f"SET LOCAL statement_timeout = {int(milliseconds)}"))

    # --- watermarks -------------------------------------------------------

    def get_watermark(self, db: Session, *, source: str) -> Optional[datetime]:
        """The newest timestamp the worker has folded for ``source``."""
        return db.scalar(
            select(ActivityWorkerWatermark.watermark).where(
                ActivityWorkerWatermark.source == source
            )
        )

    def set_watermark(self, db: Session, *, source: str, watermark: datetime) -> None:
        """Advance (or create) the watermark of ``source``; never moves back."""
        stmt = insert(ActivityWorkerWatermark).values(
            id=uuid.uuid4(), source=source, watermark=watermark
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ActivityWorkerWatermark.source],
            set_={
                "watermark": func.greatest(
                    ActivityWorkerWatermark.watermark, stmt.excluded.watermark
                ),
                "updated_at": func.now(),
            },
        )
        db.execute(stmt)

    def get_install_floor(self, db: Session) -> Optional[datetime]:
        """When the worker's history starts: the oldest watermark row's creation."""
        return db.scalar(select(func.min(ActivityWorkerWatermark.created_at)))

    # --- rollups ----------------------------------------------------------

    def upsert_rollups(
        self, db: Session, *, deltas: Sequence[RollupDelta], pass_id: uuid.UUID
    ) -> int:
        """Add ``deltas`` to their rows, creating rows as needed.

        A row whose ``last_pass_id`` already equals ``pass_id`` is left alone,
        so re-applying one pass's deltas cannot double count.

        Returns:
            Number of deltas submitted.
        """
        if not deltas:
            return 0
        rows = []
        for delta in deltas:
            row: dict[str, Any] = {
                "id": uuid.uuid4(),
                "account_id": delta.account_id,
                "granularity": delta.granularity,
                "bucket_start": delta.bucket_start,
                "subject_type": delta.subject_type,
                "subject_id": delta.subject_id[:128],
                "spend_usd": delta.spend_usd,
                "reply_latency_hist": list(delta.reply_latency_hist),
                "approval_latency_hist": list(delta.approval_latency_hist),
                "last_pass_id": pass_id,
            }
            for column in _COUNT_COLUMNS:
                row[column] = getattr(delta, column)
            rows.append(row)
        stmt = insert(ActivityRollup).values(rows)
        set_: dict[str, Any] = {
            column: getattr(ActivityRollup, column) + getattr(stmt.excluded, column)
            for column in _COUNT_COLUMNS
        }
        # Keep NULL (unpriced) only while neither side carried a price.
        set_["spend_usd"] = func.coalesce(
            ActivityRollup.spend_usd + stmt.excluded.spend_usd,
            ActivityRollup.spend_usd,
            stmt.excluded.spend_usd,
        )
        set_["reply_latency_hist"] = _hist_sum("reply_latency_hist")
        set_["approval_latency_hist"] = _hist_sum("approval_latency_hist")
        set_["last_pass_id"] = stmt.excluded.last_pass_id
        set_["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            constraint="uq_activity_rollup_key",
            set_=set_,
            where=or_(
                ActivityRollup.last_pass_id.is_(None),
                ActivityRollup.last_pass_id != stmt.excluded.last_pass_id,
            ),
        )
        db.execute(stmt)
        return len(rows)

    def read_rollups(
        self,
        db: Session,
        *,
        account_id: Any,
        granularities: Iterable[int],
        since: Optional[datetime],
        subject_types: Iterable[str],
        limit: int = MAX_BOARD_ROWS,
    ) -> list[ActivityRollup]:
        """Rollup rows of an account at the given granularities from ``since``.

        Served by ``ix_activity_rollup_account_bucket``; ``since`` None reads
        the whole retention. Newest buckets first, capped at ``limit``.
        """
        query = select(ActivityRollup).where(
            ActivityRollup.account_id == account_id,
            ActivityRollup.granularity.in_(list(granularities)),
            ActivityRollup.subject_type.in_(list(subject_types)),
        )
        if since is not None:
            query = query.where(ActivityRollup.bucket_start >= since)
        query = query.order_by(ActivityRollup.bucket_start.desc()).limit(
            max(1, min(limit, MAX_BOARD_ROWS))
        )
        return list(db.scalars(query))

    def read_board_rollups(
        self,
        db: Session,
        *,
        account_id: Any,
        granularities: Iterable[int],
        since: Optional[datetime],
        limit: int = MAX_BOARD_ROWS,
    ) -> tuple[list[Any], bool]:
        """The board's rollups in one statement: account buckets plus totals.

        Account rows come back per bucket (they draw the sparkline and the
        stats); person and agent rows come back summed per subject over the
        window, histograms summed element-wise. So an account with many
        active subjects never pushes its own buckets past the row limit.

        Account rows carry ``bucket_start`` and ``granularity``; subject
        totals carry the epoch and granularity 0.

        Passing ``[60, 3600]`` is safe: :meth:`fold_minutes_into_hours`
        deletes each minute row in the statement that adds it to its hour, so
        minute and hour rows never hold the same event. Long windows read
        both because the last 48 hours are still minutes.

        Returns:
            The rows, and whether the row limit cut the result.
        """
        rows = db.execute(
            text(
                """
                WITH r AS (
                    SELECT * FROM activity_rollup
                    WHERE account_id = :account_id
                      AND granularity = ANY(:granularities)
                      AND (CAST(:since AS timestamptz) IS NULL
                           OR bucket_start >= CAST(:since AS timestamptz))
                      AND subject_type IN ('account', 'person', 'agent')
                ), s AS (
                    SELECT * FROM r WHERE subject_type <> 'account'
                ), sums AS (
                    SELECT subject_type, subject_id,
                        SUM(requests)::int AS requests,
                        SUM(tool_calls)::int AS tool_calls,
                        SUM(prompts)::int AS prompts,
                        SUM(turn_ends)::int AS turn_ends,
                        SUM(spend_usd) AS spend_usd
                    FROM s GROUP BY 1, 2
                ), rh AS (
                    SELECT s.subject_type, s.subject_id, h.i, SUM(h.v) AS v
                    FROM s, unnest(s.reply_latency_hist) WITH ORDINALITY h(v, i)
                    GROUP BY 1, 2, 3
                ), ah AS (
                    SELECT s.subject_type, s.subject_id, h.i, SUM(h.v) AS v
                    FROM s, unnest(s.approval_latency_hist) WITH ORDINALITY h(v, i)
                    GROUP BY 1, 2, 3
                ), rha AS (
                    SELECT subject_type, subject_id,
                        array_agg(v::int ORDER BY i) AS arr
                    FROM rh GROUP BY 1, 2
                ), aha AS (
                    SELECT subject_type, subject_id,
                        array_agg(v::int ORDER BY i) AS arr
                    FROM ah GROUP BY 1, 2
                )
                SELECT * FROM (
                    SELECT subject_type, subject_id, bucket_start,
                        granularity::int AS granularity, requests, tool_calls,
                        prompts, turn_ends, spend_usd,
                        reply_latency_hist::int[] AS reply_latency_hist,
                        approval_latency_hist::int[] AS approval_latency_hist,
                        0 AS rank
                    FROM r WHERE subject_type = 'account'
                    UNION ALL
                    SELECT sums.subject_type, sums.subject_id,
                        'epoch'::timestamptz, 0, sums.requests, sums.tool_calls,
                        sums.prompts, sums.turn_ends, sums.spend_usd,
                        COALESCE(rha.arr, '{}'), COALESCE(aha.arr, '{}'), 1
                    FROM sums
                    LEFT JOIN rha USING (subject_type, subject_id)
                    LEFT JOIN aha USING (subject_type, subject_id)
                ) board
                ORDER BY rank, bucket_start DESC, requests DESC
                LIMIT :limit
                """
            ),
            {
                "account_id": account_id,
                "granularities": list(granularities),
                "since": since,
                "limit": max(1, min(limit, MAX_BOARD_ROWS)) + 1,
            },
        ).all()
        cap = max(1, min(limit, MAX_BOARD_ROWS))
        return list(rows[:cap]), len(rows) > cap

    def fold_minutes_into_hours(
        self, db: Session, *, older_than: datetime, batch: int = 5000
    ) -> int:
        """Move one batch of minute rows older than ``older_than`` into hours.

        The minute rows are summed into their hour rows and deleted in the
        same statement, so a crash cannot count a minute twice.

        Returns:
            Minute rows folded in this batch (0 when nothing is left).
        """
        result = db.execute(
            text(
                """
                WITH picked AS (
                    SELECT id FROM activity_rollup
                    WHERE granularity = 60 AND bucket_start < :older_than
                    ORDER BY bucket_start
                    LIMIT :batch
                    FOR UPDATE SKIP LOCKED
                ), moved AS (
                    DELETE FROM activity_rollup r USING picked
                    WHERE r.id = picked.id
                    RETURNING r.*
                ), m AS (
                    SELECT *, date_trunc('hour', bucket_start, 'UTC') AS hour
                    FROM moved
                ), rh AS (
                    SELECT m.account_id, m.subject_type, m.subject_id, m.hour,
                        h.i, SUM(h.v) AS v
                    FROM m, unnest(m.reply_latency_hist) WITH ORDINALITY h(v, i)
                    GROUP BY 1, 2, 3, 4, 5
                ), rha AS (
                    SELECT account_id, subject_type, subject_id, hour,
                        array_agg(v::smallint ORDER BY i) AS arr
                    FROM rh GROUP BY 1, 2, 3, 4
                ), ah AS (
                    SELECT m.account_id, m.subject_type, m.subject_id, m.hour,
                        h.i, SUM(h.v) AS v
                    FROM m, unnest(m.approval_latency_hist) WITH ORDINALITY h(v, i)
                    GROUP BY 1, 2, 3, 4, 5
                ), aha AS (
                    SELECT account_id, subject_type, subject_id, hour,
                        array_agg(v::smallint ORDER BY i) AS arr
                    FROM ah GROUP BY 1, 2, 3, 4
                ), agg AS (
                    SELECT account_id, subject_type, subject_id, hour,
                        SUM(requests) AS requests, SUM(tool_calls) AS tool_calls,
                        SUM(prompts) AS prompts, SUM(turn_ends) AS turn_ends,
                        SUM(approvals_requested) AS approvals_requested,
                        SUM(approvals_decided) AS approvals_decided,
                        SUM(prompt_tokens) AS prompt_tokens,
                        SUM(completion_tokens) AS completion_tokens,
                        SUM(spend_usd) AS spend_usd,
                        SUM(active_seconds) AS active_seconds
                    FROM m GROUP BY 1, 2, 3, 4
                ), folded AS (
                    INSERT INTO activity_rollup (
                        id, account_id, granularity, bucket_start, subject_type,
                        subject_id, requests, tool_calls, prompts, turn_ends,
                        approvals_requested, approvals_decided, prompt_tokens,
                        completion_tokens, spend_usd, reply_latency_hist,
                        approval_latency_hist, active_seconds, created_at,
                        updated_at
                    )
                    SELECT gen_random_uuid(), agg.account_id, 3600, agg.hour,
                        agg.subject_type, agg.subject_id, agg.requests,
                        agg.tool_calls, agg.prompts, agg.turn_ends,
                        agg.approvals_requested, agg.approvals_decided,
                        agg.prompt_tokens, agg.completion_tokens, agg.spend_usd,
                        rha.arr, aha.arr, agg.active_seconds, now(), now()
                    FROM agg
                    JOIN rha USING (account_id, subject_type, subject_id, hour)
                    JOIN aha USING (account_id, subject_type, subject_id, hour)
                    ON CONFLICT ON CONSTRAINT uq_activity_rollup_key DO UPDATE SET
                        requests = activity_rollup.requests + excluded.requests,
                        tool_calls = activity_rollup.tool_calls + excluded.tool_calls,
                        prompts = activity_rollup.prompts + excluded.prompts,
                        turn_ends = activity_rollup.turn_ends + excluded.turn_ends,
                        approvals_requested = activity_rollup.approvals_requested
                            + excluded.approvals_requested,
                        approvals_decided = activity_rollup.approvals_decided
                            + excluded.approvals_decided,
                        prompt_tokens = activity_rollup.prompt_tokens
                            + excluded.prompt_tokens,
                        completion_tokens = activity_rollup.completion_tokens
                            + excluded.completion_tokens,
                        spend_usd = COALESCE(
                            activity_rollup.spend_usd + excluded.spend_usd,
                            activity_rollup.spend_usd, excluded.spend_usd),
                        reply_latency_hist = ARRAY(
                            SELECT COALESCE(activity_rollup.reply_latency_hist[i], 0)
                                + COALESCE(excluded.reply_latency_hist[i], 0)
                            FROM generate_series(1, :buckets) AS i)::smallint[],
                        approval_latency_hist = ARRAY(
                            SELECT COALESCE(activity_rollup.approval_latency_hist[i], 0)
                                + COALESCE(excluded.approval_latency_hist[i], 0)
                            FROM generate_series(1, :buckets) AS i)::smallint[],
                        active_seconds = activity_rollup.active_seconds
                            + excluded.active_seconds,
                        updated_at = now()
                    RETURNING 1
                )
                SELECT (SELECT COUNT(*) FROM moved), (SELECT COUNT(*) FROM folded)
                """
            ),
            {"older_than": older_than, "batch": batch, "buckets": LATENCY_BUCKETS},
        ).one()
        return int(result[0] or 0)

    def delete_rollups_before(
        self, db: Session, *, granularity: int, before: datetime, batch: int = 5000
    ) -> int:
        """Delete one batch of rollup rows older than ``before``."""
        picked = (
            select(ActivityRollup.id)
            .where(
                ActivityRollup.granularity == granularity,
                ActivityRollup.bucket_start < before,
            )
            .limit(batch)
            .scalar_subquery()
        )
        result = db.execute(delete(ActivityRollup).where(ActivityRollup.id.in_(picked)))
        return int(getattr(result, "rowcount", 0) or 0)

    # --- presence state ---------------------------------------------------

    def list_current_states(
        self,
        db: Session,
        *,
        account_id: Any,
        subject_types: Iterable[str],
        limit: int = MAX_BOARD_ROWS,
    ) -> list[PresenceState]:
        """Current (``until IS NULL``) state rows of an account."""
        return list(
            db.scalars(
                select(PresenceState)
                .where(
                    PresenceState.account_id == account_id,
                    PresenceState.subject_type.in_(list(subject_types)),
                    PresenceState.until.is_(None),
                )
                .order_by(PresenceState.last_signal_at.desc().nullslast())
                .limit(max(1, min(limit, MAX_BOARD_ROWS)))
            )
        )

    def list_all_current_states(
        self, db: Session, *, limit: int = MAX_BOARD_ROWS
    ) -> list[PresenceState]:
        """Every account's current state rows, for a worker cold start."""
        return list(
            db.scalars(
                select(PresenceState)
                .where(PresenceState.until.is_(None))
                .order_by(PresenceState.last_signal_at.desc().nullslast())
                .limit(max(1, min(limit, MAX_BOARD_ROWS)))
            )
        )

    def apply_state(
        self,
        db: Session,
        *,
        account_id: Any,
        update_: PresenceUpdate,
        now: datetime,
    ) -> tuple[PresenceState, bool]:
        """Record a derived state, closing the previous row when it changed.

        A change of state or of a flag closes the current row (``until =
        now``) and opens a new one, in the caller's transaction. A newer
        ``last_signal_at`` alone updates the current row in place.

        Returns:
            The current row and whether the state or a flag changed.
        """
        current = db.scalar(
            select(PresenceState)
            .where(
                PresenceState.account_id == account_id,
                PresenceState.subject_type == update_.subject_type,
                PresenceState.subject_id == update_.subject_id,
                PresenceState.until.is_(None),
            )
            .with_for_update()
        )
        if current is not None and (
            current.state == update_.state
            and current.awaiting_reply_since == update_.awaiting_reply_since
            and current.awaiting_approval_count == update_.awaiting_approval_count
        ):
            current.last_signal_at = update_.last_signal_at or current.last_signal_at
            current.last_signal_kind = (
                update_.last_signal_kind or current.last_signal_kind
            )
            current.runtime_session_id = (
                update_.runtime_session_id or current.runtime_session_id
            )
            current.device_key = update_.device_key or current.device_key
            db.add(current)
            return current, False
        since = now
        if current is not None:
            if current.state == update_.state:
                since = current.since
            current.until = now
            db.add(current)
            db.flush()
        row = PresenceState(
            account_id=account_id,
            subject_type=update_.subject_type,
            subject_id=update_.subject_id[:128],
            state=update_.state,
            since=since,
            last_signal_at=update_.last_signal_at,
            last_signal_kind=update_.last_signal_kind,
            awaiting_reply_since=update_.awaiting_reply_since,
            awaiting_approval_count=update_.awaiting_approval_count,
            runtime_session_id=update_.runtime_session_id,
            device_key=update_.device_key,
        )
        db.add(row)
        db.flush()
        return row, True

    def delete_closed_states_before(
        self, db: Session, *, before: datetime, batch: int = 5000
    ) -> int:
        """Delete one batch of closed state rows that ended before ``before``."""
        picked = (
            select(PresenceState.id)
            .where(PresenceState.until.is_not(None), PresenceState.until < before)
            .limit(batch)
            .scalar_subquery()
        )
        result = db.execute(delete(PresenceState).where(PresenceState.id.in_(picked)))
        return int(getattr(result, "rowcount", 0) or 0)

    # --- settings ---------------------------------------------------------

    def presence_enabled(self, db: Session, *, account_id: Any) -> bool:
        """Whether presence is on for the account; no row means on."""
        value = db.scalar(
            select(ActivitySettings.presence_enabled).where(
                ActivitySettings.account_id == account_id
            )
        )
        return True if value is None else bool(value)

    def board_settings(
        self, db: Session, *, account_id: Any
    ) -> tuple[bool, Optional[datetime]]:
        """Presence switch and history floor of the board, in one statement.

        Returns:
            Whether presence is on (no row means on) and when the worker's
            history starts (None before the first pass).
        """
        enabled = (
            select(ActivitySettings.presence_enabled)
            .where(ActivitySettings.account_id == account_id)
            .scalar_subquery()
        )
        floor = select(func.min(ActivityWorkerWatermark.created_at)).scalar_subquery()
        row = db.execute(select(enabled, floor)).one()
        return (True if row[0] is None else bool(row[0])), row[1]

    def set_presence_enabled(
        self, db: Session, *, account_id: Any, enabled: bool, user_id: Any
    ) -> None:
        """Create or update the account's presence switch."""
        stmt = insert(ActivitySettings).values(
            id=uuid.uuid4(),
            account_id=account_id,
            presence_enabled=enabled,
            updated_by_user_id=user_id,
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ActivitySettings.account_id],
                set_={
                    "presence_enabled": enabled,
                    "updated_by_user_id": user_id,
                    "updated_at": func.now(),
                },
            )
        )

    # --- directory reads for the board ------------------------------------

    def users_brief(
        self, db: Session, *, account_id: Any, user_ids: Iterable[Any]
    ) -> dict[str, dict[str, Any]]:
        """Display fields of active users of the account, by id."""
        ids = [uuid.UUID(str(i)) for i in user_ids if _is_uuid(i)]
        if not ids:
            return {}
        rows = db.execute(
            select(
                models.User.id,
                models.User.full_name,
                models.User.username,
                models.User.email,
            ).where(
                models.User.account_id == account_id,
                models.User.id.in_(ids),
                models.User.is_active.is_(True),
            )
        ).all()
        return {
            str(r.id): {
                "display_name": r.full_name or r.username or r.email,
            }
            for r in rows
        }

    def agents_brief(
        self, db: Session, *, account_id: Any, agent_ids: Iterable[Any]
    ) -> dict[str, dict[str, Any]]:
        """Display fields of the account's managed agents, by id."""
        ids = [uuid.UUID(str(i)) for i in agent_ids if _is_uuid(i)]
        if not ids:
            return {}
        agent = models.ManagedAgent
        rows = db.execute(
            select(
                agent.id,
                agent.display_name,
                agent.agent_kind,
                agent.owner_user_id,
                agent.runtime_session_id,
                agent.enrollment_hostname,
                agent.last_seen_at,
            ).where(agent.account_id == account_id, agent.id.in_(ids))
        ).all()
        return {
            str(r.id): {
                "display_name": r.display_name,
                "agent_kind": r.agent_kind,
                "owner_user_id": str(r.owner_user_id) if r.owner_user_id else None,
                "runtime_session_id": (
                    str(r.runtime_session_id) if r.runtime_session_id else None
                ),
                "enrollment_hostname": r.enrollment_hostname,
                "last_seen_at": r.last_seen_at,
            }
            for r in rows
        }

    def sessions_brief(
        self, db: Session, *, account_id: Any, session_ids: Iterable[Any]
    ) -> dict[str, dict[str, Any]]:
        """Title, working directory and lifecycle of the account's sessions."""
        ids = [uuid.UUID(str(i)) for i in session_ids if _is_uuid(i)]
        if not ids:
            return {}
        session = models.RuntimeSession
        rows = db.execute(
            select(
                session.id,
                session.title,
                session.cwd,
                session.ended_at,
                session.last_activity_at,
                session.session_source_type,
            ).where(session.account_id == account_id, session.id.in_(ids))
        ).all()
        return {
            str(r.id): {
                "title": r.title,
                "cwd": r.cwd,
                "ended_at": r.ended_at,
                "last_activity_at": r.last_activity_at,
                "session_source_type": r.session_source_type,
            }
            for r in rows
        }

    def account_has_agents(self, db: Session, *, account_id: Any) -> bool:
        """Whether the account has any managed agent at all."""
        return (
            db.scalar(
                select(models.ManagedAgent.id)
                .where(models.ManagedAgent.account_id == account_id)
                .limit(1)
            )
            is not None
        )

    # --- worker source reads (raw tables, time-bounded) -------------------

    def read_gateway_usage(
        self,
        db: Session,
        *,
        after: datetime,
        until: datetime,
        limit: int = MAX_SOURCE_ROWS,
    ) -> list[Any]:
        """Model gateway rows in ``(after, until]`` across all accounts.

        Replay-validation and imported rows are excluded. ``api_usage``
        stores naive UTC timestamps, so the bounds are compared naive.
        """
        usage = models.ApiUsage
        purpose = usage.meta_data.op("->>")(literal_column("'purpose'"))
        return list(
            db.execute(
                select(
                    usage.id,
                    usage.account_id,
                    usage.user_id,
                    usage.api_key_id,
                    usage.runtime_session_id,
                    usage.estimated_cost,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.timestamp,
                    models.ApiKey.context_data.op("->>")(
                        literal_column("'managed_agent_id'")
                    ).label("managed_agent_id"),
                )
                .outerjoin(models.ApiKey, models.ApiKey.id == usage.api_key_id)
                .where(
                    usage.timestamp > _naive(after),
                    usage.timestamp <= _naive(until),
                    usage.account_id.is_not(None),
                    usage.action_type == "model_gateway",
                    or_(
                        purpose.is_(None),
                        purpose != literal_column(f"'{REPLAY_PURPOSE}'"),
                    ),
                    or_(usage.usage_source.is_(None), usage.usage_source != "imported"),
                )
                .order_by(usage.timestamp)
                .limit(limit)
            ).all()
        )

    def read_session_activity(
        self,
        db: Session,
        *,
        after: datetime,
        until: datetime,
        activity_types: Sequence[str] = HOOK_TURN_TYPES,
        limit: int = MAX_SOURCE_ROWS,
    ) -> list[Any]:
        """Session activity of the given kinds in ``(after, until]``.

        The worker reads hook timestamps (``user_prompt``, ``turn_end``,
        ``session_end``; served by the partial ``ix_rsa_account_turn_events``)
        and tool calls (``ix_runtime_session_activity_timestamp``) as two
        sources with their own watermarks.
        """
        activity = models.RuntimeSessionActivity
        return list(
            db.execute(
                select(
                    activity.id,
                    activity.account_id,
                    activity.runtime_session_id,
                    activity.api_key_id,
                    activity.activity_type,
                    activity.metadata_,
                    activity.timestamp,
                    activity.flow_execution_id,
                    models.ApiKey.user_id.label("api_key_user_id"),
                    models.ApiKey.context_data.op("->>")(
                        literal_column("'managed_agent_id'")
                    ).label("managed_agent_id"),
                )
                .outerjoin(models.ApiKey, models.ApiKey.id == activity.api_key_id)
                .where(
                    activity.timestamp > after,
                    activity.timestamp <= until,
                    activity.activity_type.in_(list(activity_types)),
                )
                .order_by(activity.timestamp)
                .limit(limit)
            ).all()
        )

    def read_approvals(
        self,
        db: Session,
        *,
        after: datetime,
        until: datetime,
        limit: int = MAX_SOURCE_ROWS,
    ) -> list[Any]:
        """Approvals requested or resolved in ``(after, until]``.

        Each row carries ``changed_at``, the later of the two that fell in
        the range, for watermark ordering.
        """
        request = models.ApprovalRequest
        changed = func.greatest(
            func.coalesce(request.resolved_at, request.requested_at),
            request.requested_at,
        )
        return list(
            db.execute(
                select(
                    request.id,
                    request.account_id,
                    request.managed_agent_id,
                    request.runtime_session_id,
                    request.status,
                    request.requested_at,
                    request.resolved_at,
                    request.responses,
                    request.decided_by_ai,
                    request.auto_approved_reason,
                    changed.label("changed_at"),
                )
                .where(
                    or_(
                        and_(
                            request.requested_at > after,
                            request.requested_at <= until,
                        ),
                        and_(
                            request.resolved_at.is_not(None),
                            request.resolved_at > after,
                            request.resolved_at <= until,
                        ),
                    )
                )
                .order_by(changed)
                .limit(limit)
            ).all()
        )

    def read_pending_approvals(
        self, db: Session, *, account_ids: Sequence[Any]
    ) -> list[Any]:
        """Open approvals of the given accounts, for agent waiting states."""
        if not account_ids:
            return []
        request = models.ApprovalRequest
        return list(
            db.execute(
                select(
                    request.id,
                    request.account_id,
                    request.managed_agent_id,
                    request.runtime_session_id,
                    request.requested_at,
                )
                .where(
                    request.account_id.in_(list(account_ids)),
                    request.status == "pending",
                )
                .order_by(request.requested_at.desc())
                .limit(MAX_SOURCE_ROWS)
            ).all()
        )

    def read_console_events(
        self,
        db: Session,
        *,
        after: datetime,
        until: datetime,
        limit: int = MAX_SOURCE_ROWS,
    ) -> list[Any]:
        """Console connects and page views with a user in ``(after, until]``."""
        event = models.Event
        return list(
            db.execute(
                select(
                    event.id,
                    event.account_id,
                    event.user_id,
                    event.event_type,
                    event.timestamp,
                )
                .where(
                    event.timestamp > after,
                    event.timestamp <= until,
                    event.event_type.in_(CONSOLE_EVENT_TYPES),
                    event.user_id.is_not(None),
                    event.account_id.is_not(None),
                )
                .order_by(event.timestamp)
                .limit(limit)
            ).all()
        )

    def read_session_cursor_history(
        self,
        db: Session,
        *,
        runtime_session_id: Any,
        since: datetime,
        limit: int = 500,
    ) -> list[Any]:
        """One session's recent hook and tool events, to rebuild its cursor."""
        activity = models.RuntimeSessionActivity
        return list(
            db.execute(
                select(activity.activity_type, activity.metadata_, activity.timestamp)
                .where(
                    activity.runtime_session_id == runtime_session_id,
                    activity.timestamp >= since,
                    activity.activity_type.in_(("tool_call", *HOOK_TURN_TYPES)),
                )
                .order_by(activity.timestamp.desc())
                .limit(limit)
            ).all()
        )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_floor(value: datetime, seconds: int) -> datetime:
    """Align ``value`` down to a ``seconds`` bucket, in UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def recent_floor(now: datetime, *, hours: int = 24) -> datetime:
    """Where a fresh install starts reading: ``now - hours``."""
    return now - timedelta(hours=hours)


crud_live_activity = CRUDLiveActivity()
