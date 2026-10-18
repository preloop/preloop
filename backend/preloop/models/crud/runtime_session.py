"""CRUD operations for RuntimeSession."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import (
    String,
    and_,
    case,
    false,
    cast,
    func,
    inspect,
    literal,
    literal_column,
    or_,
    tuple_,
    update,
)
from sqlalchemy.orm import Session, aliased

from preloop.models import models
from preloop.models.gateway_usage import GATEWAY_USAGE_PURPOSES
from .api_usage import cache_split_columns, cache_split_from_row
from .base import CRUDBase

ApiUsage = models.ApiUsage
Flow = models.Flow
RuntimeSession = models.RuntimeSession

logger = logging.getLogger(__name__)

# Column presence for this bind. Summary fields and parent_session_id landed
# in later revisions; list/detail queries must not SELECT them against a
# not-yet-migrated schema.
_runtime_session_columns_cache: dict[int, frozenset[str]] = {}
_SUMMARY_COLUMN_NAMES = frozenset(
    {"summary", "summary_updated_at", "title", "title_request_count"}
)

# Preloop-internal model-gateway calls (session summarization/optimization,
# session-title generation, and replay-validation re-executions) are logged
# tagged via ``ApiUsage.meta_data->>'purpose'``. Real agent traffic has no
# ``purpose`` (NULL meta_data or a NULL ``purpose`` key). These internal calls
# must be excluded from a session's aggregated token/cost/request metrics so the
# session reflects only the agent's own traffic, not Preloop's overhead.
# ``replay_validation`` runs additionally suppress session attribution at the
# gateway; its presence here is defense in depth so a mis-attributed replay row
# can never inflate the session it was validating.
INTERNAL_USAGE_PURPOSES = tuple(sorted(GATEWAY_USAGE_PURPOSES))

# Infix marking a runtime session row minted by the gateway's inactivity closer
# rather than by an agent-declared conversation id. A source id shaped
# ``<principal>:idle-<epoch>`` is the Nth generation of a signal-less agent's
# session; ``<principal>:<something-else>`` is keyed by a real session id the
# agent (or the client's X-Preloop-Session-Id) supplied. Keeping the two
# namespaces distinguishable is what lets provenance be reported honestly and
# stops the closer from ever matching a natively-keyed row.
IDLE_GENERATION_INFIX = ":idle-"


def _exclude_internal_usage_condition():
    """Return a NULL-safe filter that excludes Preloop-internal usage rows.

    Rows are INCLUDED (treated as agent traffic) when ``meta_data`` is NULL or
    when its ``purpose`` key is NULL. Only rows whose ``purpose`` is explicitly
    one of :data:`INTERNAL_USAGE_PURPOSES` are EXCLUDED. The JSONB accessor
    ``ApiUsage.meta_data["purpose"].astext`` matches the style used elsewhere in
    the codebase (see ``crud/api_usage.py``).

    Returns:
        A SQLAlchemy boolean expression suitable for use in a WHERE/JOIN clause.
    """
    purpose_expr = ApiUsage.meta_data["purpose"].astext
    return or_(
        purpose_expr.is_(None),
        purpose_expr.notin_(INTERNAL_USAGE_PURPOSES),
    )


def _gateway_usage_base_query(
    db: Session,
    *,
    account_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
):
    """Build the shared gateway-usage query used for latest-per-session lookups."""
    query = db.query(ApiUsage).filter(
        ApiUsage.account_id == account_id,
        ApiUsage.action_type == "model_gateway",
        ApiUsage.runtime_session_id.isnot(None),
        # Never surface a Preloop-internal call as the session's latest model.
        _exclude_internal_usage_condition(),
    )
    if start_date is not None:
        query = query.filter(ApiUsage.timestamp >= start_date)
    if end_date is not None:
        query = query.filter(ApiUsage.timestamp < end_date)
    if ai_model_id is not None:
        query = query.filter(ApiUsage.ai_model_id == ai_model_id)
    return query


def _latest_gateway_usage_for_session(
    db: Session,
    *,
    account_id: str,
    runtime_session_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
):
    """Return the latest gateway usage row for one runtime session."""
    return (
        _gateway_usage_base_query(
            db,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        .filter(ApiUsage.runtime_session_id == runtime_session_id)
        .with_entities(
            ApiUsage.model_alias.label("latest_model_alias"),
            ApiUsage.provider_name.label("latest_provider_name"),
            ApiUsage.timestamp.label("last_request_at"),
        )
        .order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
        .first()
    )


def _latest_gateway_usage_for_sessions(
    db: Session,
    *,
    account_id: str,
    runtime_session_ids: list[str],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return the latest gateway usage row for many runtime sessions."""
    if not runtime_session_ids:
        return {}

    rows = (
        _gateway_usage_base_query(
            db,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        .filter(ApiUsage.runtime_session_id.in_(runtime_session_ids))
        .with_entities(
            ApiUsage.runtime_session_id,
            ApiUsage.model_alias,
            ApiUsage.provider_name,
            ApiUsage.timestamp,
        )
        .distinct(ApiUsage.runtime_session_id)
        .order_by(
            ApiUsage.runtime_session_id.asc(),
            ApiUsage.timestamp.desc(),
            ApiUsage.id.desc(),
        )
        .all()
    )

    latest_by_session: dict[str, Any] = {}
    for row in rows:
        session_id = str(row.runtime_session_id)
        if session_id in latest_by_session:
            continue
        latest_by_session[session_id] = row
    return latest_by_session


def _is_uuid(value: Optional[str]) -> bool:
    """Return True when ``value`` parses as a UUID (a flow execution id)."""
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


class SpawnAliasConflictError(ValueError):
    """A resumed session cannot rename its write-once alias to avoid a clash."""


# Recent-usage windows the session list ranking tries before reading the
# whole usage history (#1584).
_RANKING_WINDOWS = (timedelta(days=1), timedelta(days=7))
# Pages ending past this row skip the windows: they rarely fit one.
_RANKING_WINDOW_MAX_ROWS = 200
# A window with more candidate sessions than this (or more recently active
# sessions without a recent request, which need their own usage lookup) is
# handed to the next window or the full ranking rather than materialized.
_RANKING_WINDOW_MAX_CANDIDATES = 5000
_RANKING_WINDOW_MAX_UNRESOLVED = 500


class CRUDRuntimeSession(CRUDBase[RuntimeSession]):
    """CRUD helpers for shared runtime session identities."""

    ACTIVE_WINDOW = timedelta(minutes=10)

    def lock_active_note_target(
        self, db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> Optional[RuntimeSession]:
        """Serialize delivery with session termination, using account scope."""
        return (
            db.query(self.model)
            .filter(
                self.model.id == runtime_session_id,
                self.model.account_id == account_id,
                self.model.ended_at.is_(None),
            )
            .with_for_update()
            .first()
        )

    def lock_ended_sessions_with_pending_notes(
        self, db: Session, *, limit: int = 100
    ) -> List[RuntimeSession]:
        """Lock a bounded batch of authoritatively ended note targets.

        Row locks serialize reconciliation with a harness reopening the same
        session. SKIP LOCKED lets API replicas divide work without waiting.
        Account equality prevents malformed cross-account note references
        from participating in reconciliation.
        """
        note = models.AgentControlCommand
        pending_note = (
            db.query(note.id)
            .filter(
                note.account_id == self.model.account_id,
                note.runtime_session_id == self.model.id,
                note.kind == "note",
                note.status.in_(["pending", "claimed"]),
            )
            .exists()
        )
        return (
            db.query(self.model)
            .filter(self.model.ended_at.isnot(None), pending_note)
            .order_by(self.model.ended_at, self.model.id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
            .all()
        )

    def get_by_source(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[RuntimeSession]:
        """Look up a runtime session by its source identity."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                self.model.session_source_id == session_source_id,
            )
            .first()
        )

    def get_latest_idle_generation(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[RuntimeSession]:
        """Return the newest generation of one source-keyed runtime session.

        Sources that put no session id on the wire (Gemini CLI, Hermes,
        OpenClaw's Anthropic transport) can only be bounded by an idle window.
        When one goes idle, the gateway closes the row and rolls to a fresh
        generation whose source id is ``<base>:idle-<timestamp>`` — the row
        itself is preserved, so the session's history is never rewritten. This
        returns the generation that traffic should currently land on: the
        newest of the base row and its ``:idle-`` descendants.

        The ``:idle-`` infix is what keeps this unambiguous. Rows suffixed with
        an agent's *native* conversation id (``<base>:<uuid>``) are resolved by
        exact source key and are deliberately NOT matched here.

        Args:
            db: Database session.
            account_id: Owning account.
            session_source_type: Runtime principal type (e.g. ``gemini_cli``).
            session_source_id: The BASE source id, without any generation
                suffix.

        Returns:
            The newest matching runtime session, or ``None`` when the principal
            has no session row yet.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                or_(
                    self.model.session_source_id == session_source_id,
                    self.model.session_source_id.startswith(
                        f"{session_source_id}{IDLE_GENERATION_INFIX}"
                    ),
                ),
            )
            .order_by(
                self.model.started_at.desc(),
                self.model.id.desc(),
            )
            .first()
        )

    def touch_activity(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        observed_at: datetime,
        min_update_interval: Optional[timedelta] = None,
        commit: bool = False,
    ) -> Optional[RuntimeSession]:
        """Update last activity for one runtime session."""
        db_obj = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.id == runtime_session_id,
            )
            .first()
        )
        if db_obj is None:
            return None
        if min_update_interval is not None and db_obj.last_activity_at is not None:
            last_activity_at = db_obj.last_activity_at
            normalized_observed_at = observed_at
            if last_activity_at.tzinfo is None and normalized_observed_at.tzinfo:
                normalized_observed_at = normalized_observed_at.replace(tzinfo=None)
            elif last_activity_at.tzinfo and normalized_observed_at.tzinfo is None:
                last_activity_at = last_activity_at.replace(tzinfo=None)

            elapsed = normalized_observed_at - last_activity_at
            if elapsed <= timedelta(0) or elapsed < min_update_interval:
                return db_obj
        db_obj.last_activity_at = observed_at
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def update_operator_state(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        ended_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Update operator-managed lifecycle state for one runtime session."""
        db_obj = self.get_account_session(
            db, account_id=account_id, runtime_session_id=runtime_session_id
        )
        if db_obj is None:
            return None
        if ended_at is not None:
            db_obj.ended_at = ended_at
            db_obj.last_activity_at = ended_at
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def mark_ended_by_hook(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        ended_at: datetime,
        commit: bool = True,
    ) -> tuple[Optional[RuntimeSession], bool]:
        """Stamp ``ended_at`` from a SessionEnd hook, at most once.

        One conditional ``UPDATE ... WHERE ended_at IS NULL``, so concurrent
        SessionEnd hooks cannot both stamp it: an ended session keeps its
        first ``ended_at`` and is never re-opened here.

        Args:
            db: Database session.
            account_id: Owning account.
            runtime_session_id: Session to end.
            ended_at: Server receive time of the hook.
            commit: Commit when True, flush otherwise.

        Returns:
            The session (None when absent) and whether this call ended it.
        """
        ended = db.execute(
            update(self.model)
            .where(
                self.model.account_id == account_id,
                self.model.id == runtime_session_id,
                self.model.ended_at.is_(None),
            )
            .values(ended_at=ended_at, last_activity_at=ended_at)
            .returning(self.model.id)
            .execution_options(synchronize_session=False)
        ).first()
        if commit:
            db.commit()
        else:
            db.flush()
        db_obj = self.get_account_session(
            db, account_id=str(account_id), runtime_session_id=str(runtime_session_id)
        )
        if db_obj is not None and ended is not None:
            db.refresh(db_obj)
        return db_obj, ended is not None

    def reopen_for_managed_agent(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Reopen the identity session of a resumed managed agent.

        A previous release stamped ``ended_at`` when an agent was suspended
        and never cleared it, so session-bound runtime keys kept failing after
        resume. Reopening is a no-op when no session exists or the session is
        already open.

        Args:
            db: Database session.
            account_id: Account that owns the session.
            session_source_type: Durable principal type of the agent.
            session_source_id: Durable principal id of the agent.
            commit: Commit the transaction when True.

        Returns:
            The reopened session, or None when there is nothing to reopen.
        """
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            logger.info(
                "No runtime session to reopen for %s/%s (account %s)",
                session_source_type,
                session_source_id,
                account_id,
            )
            return None
        if db_obj.ended_at is None:
            # Already open: resuming an agent that was never session-ended.
            return db_obj
        now = datetime.now(UTC)
        db_obj.ended_at = None
        # started_at is NOT NULL, so it is left alone: reopening continues the
        # original session rather than pretending it began now.
        db_obj.last_activity_at = now
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def fold_into(
        self,
        db: Session,
        *,
        account_id: Any,
        source_session_id: Any,
        target_session_id: Any,
    ) -> bool:
        """Move one session's timeline onto another and delete the source.

        Used when two rows turn out to record one conversation, for example a
        hook that registered a runner-hosted harness session before the
        server learned the harness id (#1482). Timeline rows (activities,
        approvals, operator notes and commands, usage, artifacts, agent and
        desktop links) and lineage children are re-pointed to the target;
        derived rows that cascade (search documents, optimisation results)
        are rebuilt for the target and go with the source. Both rows must
        belong to ``account_id``. Flushes; the caller commits.

        Returns:
            True when the source existed and was folded.
        """
        if source_session_id == target_session_id:
            return False
        rows = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.id.in_([source_session_id, target_session_id]),
            )
            .all()
        )
        by_id = {row.id: row for row in rows}
        source = by_id.get(source_session_id)
        target = by_id.get(target_session_id)
        if source is None or target is None:
            return False
        repoint = (
            models.RuntimeSessionActivity,
            models.ApprovalRequest,
            models.AgentControlCommand,
            models.ApiUsage,
            models.RuntimeSessionArtifact,
            models.ManagedAgent,
            models.DesktopView,
        )
        for model in repoint:
            column = getattr(model, "runtime_session_id", None)
            if column is None:
                continue
            db.query(model).filter(column == source.id).update(
                {column: target.id}, synchronize_session=False
            )
        db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.parent_session_id == source.id,
            self.model.id != target.id,
        ).update({self.model.parent_session_id: target.id}, synchronize_session=False)
        if target.parent_session_id == source.id:
            target.parent_session_id = None
        if source.last_activity_at and (
            target.last_activity_at is None
            or source.last_activity_at > target.last_activity_at
        ):
            target.last_activity_at = source.last_activity_at
        db.add(target)
        db.flush()
        db.delete(source)
        db.flush()
        return True

    def upsert_by_source(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
        session_reference: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        started_at: Optional[datetime] = None,
        last_activity_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        reopen_if_ended: bool = False,
        parent_session_id: Optional[Any] = None,
    ) -> RuntimeSession:
        """Create or update a runtime session keyed by source identity.

        ``parent_session_id`` is write-once: it is stored when the row is
        created, and on an existing row only fills a NULL. A harness reports
        lineage on the subagent's first turn, so a later request that claims a
        different parent for the same conversation is either a race or a lie,
        and either way the first answer is the one that was observed.
        """
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            is_account_first_session = (
                db.query(self.model.id)
                .filter(self.model.account_id == account_id)
                .first()
                is None
            )
            db_obj = RuntimeSession(
                account_id=account_id,
                session_source_type=session_source_type,
                session_source_id=session_source_id,
                session_reference=session_reference,
                runtime_principal_type=runtime_principal_type,
                runtime_principal_id=runtime_principal_id,
                runtime_principal_name=runtime_principal_name,
                started_at=started_at or last_activity_at,
                last_activity_at=last_activity_at,
                ended_at=ended_at,
                parent_session_id=parent_session_id,
            )
            db.add(db_obj)
            db.flush()
            if is_account_first_session:
                # Every session-recording path funnels through this creation
                # branch, so it is the single chokepoint for the account's
                # first-session signal. The notification is an inert no-op
                # unless an EE plugin registered a hook (see
                # preloop.services.session_events); it never raises. Lazy
                # import keeps the models package importable standalone.
                try:
                    from preloop.services.session_events import (
                        notify_first_session_recorded,
                    )
                except ImportError:  # pragma: no cover - models-only contexts
                    pass
                else:
                    notify_first_session_recorded(
                        db,
                        account_id=account_id,
                        occurred_at=db_obj.started_at
                        or db_obj.last_activity_at
                        or datetime.now(UTC),
                    )
            return db_obj

        if session_reference is not None:
            db_obj.session_reference = session_reference
        if runtime_principal_type is not None:
            db_obj.runtime_principal_type = runtime_principal_type
        if runtime_principal_id is not None:
            db_obj.runtime_principal_id = runtime_principal_id
        if runtime_principal_name is not None:
            db_obj.runtime_principal_name = runtime_principal_name
        if parent_session_id is not None and db_obj.parent_session_id is None:
            db_obj.parent_session_id = parent_session_id
            if db_obj.alias:
                db_obj.alias_namespace = f"parent:{parent_session_id}"
        if reopen_if_ended and db_obj.ended_at is not None and ended_at is None:
            db_obj.ended_at = None
            db_obj.started_at = started_at or last_activity_at
        elif started_at is not None and db_obj.started_at is None:
            db_obj.started_at = started_at
        if last_activity_at is not None:
            db_obj.last_activity_at = last_activity_at
        if ended_at is not None:
            db_obj.ended_at = ended_at

        db.add(db_obj)
        db.flush()
        return db_obj

    def lock_alias_account(self, db: Session, *, account_id: Any) -> None:
        """Serialize spawn alias assignment within an account until commit."""
        from preloop.models.models.account import Account

        db.query(Account.id).filter(Account.id == account_id).with_for_update().one()

    def assign_spawn_alias(
        self,
        db: Session,
        *,
        session: RuntimeSession,
        alias: Optional[str],
        principal_type: str,
        principal_id: str,
    ) -> None:
        """Fill a write-once alias, suffixing active clashes in its namespace.

        The caller holds lock_alias_account through the session commit.
        A parent groups workers across principals; otherwise the principal
        is the namespace. Ended sessions release their names.
        """
        if not session.alias and not alias:
            return
        query = db.query(self.model.alias).filter(
            self.model.account_id == session.account_id,
            self.model.ended_at.is_(None),
            self.model.id != session.id,
            self.model.alias.isnot(None),
        )
        if session.parent_session_id is not None:
            query = query.filter(
                self.model.parent_session_id == session.parent_session_id
            )
        else:
            prefix = (
                principal_id.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            query = query.filter(
                self.model.parent_session_id.is_(None),
                self.model.session_source_type == principal_type,
                self.model.session_source_id.like(prefix + ":%", escape="\\"),
            )
        taken = {row[0] for row in query.all()}
        session.alias_namespace = (
            f"parent:{session.parent_session_id}"
            if session.parent_session_id is not None
            else f"principal:{principal_type}:{principal_id}"
        )
        if session.alias:
            if session.alias in taken:
                raise SpawnAliasConflictError(
                    "This session alias is already active in its namespace; end the other run before resuming."
                )
            return
        candidate = alias
        number = 2
        while candidate in taken:
            suffix = f"-{number}"
            candidate = alias[: 64 - len(suffix)] + suffix
            number += 1
        session.alias = candidate

    def resolve_alias(
        self,
        db: Session,
        *,
        account_id: Any,
        alias: str,
        principal_type: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> list[RuntimeSession]:
        """Resolve an active alias, preferring the caller's own principal."""
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.alias == alias,
            self.model.ended_at.is_(None),
        )
        if principal_type and principal_id:
            prefix = (
                principal_id.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            owned = (
                query.filter(
                    self.model.session_source_type == principal_type,
                    self.model.session_source_id.like(prefix + ":%", escape="\\"),
                )
                .order_by(self.model.started_at.desc())
                .limit(5)
                .all()
            )
            if owned:
                return owned
        return query.order_by(self.model.started_at.desc()).limit(5).all()

    def list_for_search_backfill(
        self,
        db: Session,
        *,
        account_id: Any,
        before_started_at: Optional[datetime] = None,
        before_session_id: Optional[Any] = None,
        not_before: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[RuntimeSession]:
        """Return one page of an account's sessions, newest first.

        The page is keyed on ``(started_at, id)`` rather than an offset: the
        search backfill walks an account across passes, and an offset would
        skip or repeat sessions whenever a new one is written between two
        passes. ``not_before`` stops the walk at the retention horizon.
        """
        stmt = db.query(RuntimeSession).filter(RuntimeSession.account_id == account_id)
        if before_started_at is not None and before_session_id is not None:
            stmt = stmt.filter(
                tuple_(RuntimeSession.started_at, RuntimeSession.id)
                < tuple_(before_started_at, before_session_id)
            )
        elif before_started_at is not None:
            stmt = stmt.filter(RuntimeSession.started_at < before_started_at)
        if not_before is not None:
            stmt = stmt.filter(RuntimeSession.started_at >= not_before)
        return (
            stmt.order_by(RuntimeSession.started_at.desc(), RuntimeSession.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )

    @staticmethod
    def _model_page_is_bounded(
        *,
        principals: Optional[list[tuple[str, str]]],
        source_types: Optional[list[str]],
        alias: Optional[str],
        parent_session_id: Optional[str],
        flow_execution_id: Optional[str],
        has_artifacts: Optional[str],
        active_within: Optional[timedelta],
        min_requests: Optional[int],
        runtime_principal_type: Optional[str],
        runtime_principal_id: Optional[str],
    ) -> bool:
        """Whether a model-scoped list can stop at the page.

        The model detail page asks for one model's recent sessions and
        nothing else. Extra filters still use the full ranking.
        """
        return not any(
            (
                principals is not None,
                source_types is not None,
                alias,
                parent_session_id,
                flow_execution_id,
                has_artifacts,
                active_within is not None,
                min_requests is not None,
                runtime_principal_type,
                runtime_principal_id,
            )
        )

    def _model_session_page_ids(
        self,
        db: Session,
        *,
        account_id: str,
        ai_model_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        query: Optional[str],
        session_source_type: Optional[str],
        status: str,
        limit: int,
        offset: int,
    ) -> tuple[List[Any], bool]:
        """Session ids for one model, newest request first, one page.

        Aggregates the model's gateway rows in the window and returns one
        page of that ranking. The predicate matches
        ``ix_api_usage_account_model_ts``, so the aggregate is an index
        range on that model rather than a scan of the account's usage
        (#1688). ``LIMIT`` applies after the per-session maximum, so the
        window is what bounds the read.

        Args:
            db: Database session.
            account_id: Account that owns the sessions.
            ai_model_id: Model whose usage selects the sessions.
            start_date: Inclusive lower bound on usage time, if set.
            end_date: Exclusive upper bound on usage time, if set.
            query: Optional case-insensitive session text filter.
            session_source_type: Optional source-type filter.
            status: ``all``, ``active``, or ``ended``.
            limit: Page size.
            offset: Rows to skip.

        Returns:
            The page of session ids, and whether another page exists.
        """
        last_request = func.max(ApiUsage.timestamp)
        # The model detail page asks for one model's newest sessions and
        # nothing else. That predicate is exactly ``ix_api_usage_account_model_ts``
        # (account, model, timestamp, and the included session id), so the
        # aggregate can be index-only. Joining ``runtime_session`` here made
        # the planner walk ``ix_api_usage_timestamp`` and recheck every row
        # in the window (#1688). Status and text filters still need the
        # session row, and those pages are not the busy-model load.
        needs_session_row = bool(
            query or session_source_type or status in ("active", "ended")
        )
        stmt = db.query(
            ApiUsage.runtime_session_id, last_request.label("last_request_at")
        ).filter(
            ApiUsage.account_id == account_id,
            ApiUsage.ai_model_id == ai_model_id,
            ApiUsage.action_type == "model_gateway",
            ApiUsage.runtime_session_id.isnot(None),
        )
        if start_date is not None:
            stmt = stmt.filter(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            stmt = stmt.filter(ApiUsage.timestamp < end_date)
        if needs_session_row:
            stmt = stmt.join(
                RuntimeSession, RuntimeSession.id == ApiUsage.runtime_session_id
            ).filter(RuntimeSession.account_id == account_id)
            if status == "active":
                stmt = stmt.filter(RuntimeSession.ended_at.is_(None))
            elif status == "ended":
                stmt = stmt.filter(RuntimeSession.ended_at.isnot(None))
            if session_source_type:
                stmt = stmt.filter(
                    RuntimeSession.session_source_type == session_source_type
                )
            if query:
                normalized_query = f"%{' '.join(query.strip().split())}%"
                stmt = stmt.filter(
                    or_(
                        RuntimeSession.session_source_type.ilike(normalized_query),
                        RuntimeSession.session_source_id.ilike(normalized_query),
                        RuntimeSession.session_reference.ilike(normalized_query),
                        RuntimeSession.runtime_principal_name.ilike(normalized_query),
                    )
                )
        rows = (
            stmt.group_by(ApiUsage.runtime_session_id)
            .order_by(last_request.desc(), ApiUsage.runtime_session_id.desc())
            .limit(limit + 1)
            .offset(offset)
            .all()
        )
        has_more = len(rows) > limit
        return [row.runtime_session_id for row in rows[:limit]], has_more

    def list_account_sessions(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        query: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        session_source_type: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        min_requests: Optional[int] = None,
        status: str = "all",
        limit: int = 20,
        offset: int = 0,
        principals: Optional[list[tuple[str, str]]] = None,
        source_types: Optional[list[str]] = None,
        alias: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        flow_execution_id: Optional[str] = None,
        active_within: Optional[timedelta] = None,
        has_artifacts: Optional[str] = None,
    ) -> dict[str, Any]:
        """List runtime sessions with aggregated gateway usage.

        ``principals`` and ``source_types`` are alternatives, OR-ed together:
        a session matches when its runtime principal is one of the given
        ``(type, id)`` pairs (or a per-run ``<id>:<run>`` variant of one), or
        when its ``session_source_type`` is one of ``source_types``. Passing
        either as an empty list matches nothing, which is how a filter that
        resolved to no agent stays a filter instead of widening to the whole
        account.

        ``active_within`` keeps open sessions whose last activity (or start,
        before the first activity) is no older than the window.

        ``has_artifacts`` keeps sessions holding at least one available
        artifact: ``any`` for any kind, otherwise that artifact kind.
        """
        session_query = db.query(self.model).filter(self.model.account_id == account_id)

        if principals is not None or source_types is not None:
            alternatives = [
                self._principal_condition(principal_type, principal_id)
                for principal_type, principal_id in (principals or [])
            ]
            if source_types:
                alternatives.append(self.model.session_source_type.in_(source_types))
            session_query = session_query.filter(
                or_(*alternatives) if alternatives else false()
            )
        if alias:
            session_query = session_query.filter(self.model.alias == alias)
        if parent_session_id:
            session_query = session_query.filter(
                self.model.parent_session_id == parent_session_id
            )
        if flow_execution_id:
            # A flow execution is linked two ways: its own legacy session row,
            # and any session whose governed usage carries the execution id.
            execution_usage = db.query(ApiUsage.runtime_session_id).filter(
                ApiUsage.account_id == account_id,
                ApiUsage.flow_execution_id == flow_execution_id,
                ApiUsage.runtime_session_id.isnot(None),
            )
            session_query = session_query.filter(
                or_(
                    and_(
                        self.model.session_source_type == "flow_execution",
                        self.model.session_source_id == str(flow_execution_id),
                    ),
                    self.model.id.in_(execution_usage.distinct()),
                )
            )
        if has_artifacts:
            from preloop.models.crud import runtime_session_artifact

            holders = runtime_session_artifact.sessions_with_available_artifacts(
                db,
                account_id=account_id,
                kind=None if has_artifacts == "any" else has_artifacts,
            )
            session_query = session_query.filter(self.model.id.in_(holders.distinct()))
        if active_within is not None:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - active_within
            session_query = session_query.filter(
                self.model.ended_at.is_(None),
                func.coalesce(self.model.last_activity_at, self.model.started_at)
                >= cutoff,
            )

        if query:
            normalized_query = f"%{' '.join(query.strip().split())}%"
            session_query = session_query.filter(
                or_(
                    self.model.session_source_type.ilike(normalized_query),
                    self.model.session_source_id.ilike(normalized_query),
                    self.model.session_reference.ilike(normalized_query),
                    self.model.runtime_principal_name.ilike(normalized_query),
                )
            )
        use_model_page = bool(ai_model_id) and self._model_page_is_bounded(
            principals=principals,
            source_types=source_types,
            alias=alias,
            parent_session_id=parent_session_id,
            flow_execution_id=flow_execution_id,
            has_artifacts=has_artifacts,
            active_within=active_within,
            min_requests=min_requests,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
        )
        model_page_ids: Optional[List[Any]] = None
        has_more = False
        if use_model_page:
            model_page_ids, has_more = self._model_session_page_ids(
                db,
                account_id=account_id,
                ai_model_id=str(ai_model_id),
                start_date=start_date,
                end_date=end_date,
                query=query,
                session_source_type=session_source_type,
                status=status,
                limit=limit,
                offset=offset,
            )
            if model_page_ids:
                session_query = session_query.filter(self.model.id.in_(model_page_ids))
            else:
                session_query = session_query.filter(false())
        elif ai_model_id:
            matching_session_ids = db.query(ApiUsage.runtime_session_id).filter(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id.isnot(None),
                ApiUsage.action_type == "model_gateway",
                ApiUsage.ai_model_id == ai_model_id,
            )
            if start_date is not None:
                matching_session_ids = matching_session_ids.filter(
                    ApiUsage.timestamp >= start_date
                )
            if end_date is not None:
                matching_session_ids = matching_session_ids.filter(
                    ApiUsage.timestamp < end_date
                )
            session_query = session_query.filter(
                self.model.id.in_(matching_session_ids.distinct())
            )
        else:
            if start_date is not None:
                start_date_utc = (
                    start_date.astimezone(UTC).replace(tzinfo=None)
                    if start_date.tzinfo
                    else start_date
                )
                session_query = session_query.filter(
                    or_(
                        self.model.last_activity_at >= start_date_utc,
                        self.model.started_at >= start_date_utc,
                    )
                )
            if end_date is not None:
                end_date_utc = (
                    end_date.astimezone(UTC).replace(tzinfo=None)
                    if end_date.tzinfo
                    else end_date
                )
                session_query = session_query.filter(
                    self.model.started_at < end_date_utc
                )

        if min_requests is not None:
            # Correlated per session, so the planner probes the session's own
            # usage rows through ix_api_usage_runtime_session_id. A grouped
            # subquery over the account's whole usage table cost seconds on
            # every agent detail load once an account had a million requests.
            usage_probe = db.query(ApiUsage.id).filter(
                ApiUsage.runtime_session_id == self.model.id,
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == "model_gateway",
            )
            if min_requests <= 1:
                has_usage = usage_probe.exists()
            else:
                has_usage = (
                    usage_probe.with_entities(func.count(ApiUsage.id))
                    .correlate(self.model)
                    .scalar_subquery()
                    >= min_requests
                )
            # Keep historical empty "shell" sessions hidden, but a freshly
            # created session (e.g. a Hermes "/new") has zero requests for the
            # first few seconds before its first gateway call lands. Surface it
            # immediately when it is recent and still open, so a new session is
            # visible the moment it is created instead of only after its request
            # count catches up.
            recent_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                minutes=15
            )
            session_query = session_query.filter(
                or_(
                    has_usage,
                    and_(
                        self.model.started_at >= recent_cutoff,
                        self.model.ended_at.is_(None),
                    ),
                )
            )
        if session_source_type:
            session_query = session_query.filter(
                self.model.session_source_type == session_source_type
            )
        if runtime_principal_type:
            session_query = session_query.filter(
                self.model.runtime_principal_type == runtime_principal_type
            )
        if runtime_principal_id:
            session_query = session_query.filter(
                self._principal_id_condition(runtime_principal_id)
            )
        if status == "active":
            session_query = session_query.filter(self.model.ended_at.is_(None))
        elif status == "ended":
            session_query = session_query.filter(self.model.ended_at.isnot(None))

        if use_model_page:
            # The probe already stopped at limit + 1. Counting every session
            # that ever used the model is the read that scaled with request
            # count (#1688). The total is exact only on the last page that
            # returned rows. A page past the end must not report the offset
            # as the total.
            if model_page_ids:
                total = None if has_more else offset + len(model_page_ids)
            else:
                total = 0 if offset == 0 and not has_more else None
            page_ids = model_page_ids or []
        else:
            total = session_query.count()
            page_ids = None
        link_filters = {
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
            "ai_model_id": ai_model_id,
        }
        # Pick the page on a narrow (session, last request) ranking first,
        # then aggregate usage for those sessions only. Aggregating every
        # session's full usage before the LIMIT took 30 s on a busy model
        # detail page (#1584). The ranking itself is tried on recent usage
        # first; it falls back to the whole history only when the recent
        # window cannot decide the page.
        # Deep pages almost never fit a recent window; go straight to the
        # full ranking instead of paying for two windows first. A model
        # detail page already has its ids from the bounded probe.
        windows = (
            ()
            if use_model_page
            else _RANKING_WINDOWS
            if offset + limit <= _RANKING_WINDOW_MAX_ROWS
            else ()
        )
        for window in windows:
            page_ids = self._rank_page_from_recent_usage(
                db,
                session_query=session_query,
                link_filters=link_filters,
                window=window,
                limit=limit,
                offset=offset,
            )
            if page_ids is not None:
                break
        if page_ids is None and not use_model_page:
            page_ids = self._rank_page_from_all_usage(
                db,
                session_query=session_query,
                link_filters=link_filters,
                limit=limit,
                offset=offset,
            )
        if page_ids:
            usage_links = self._account_usage_links(
                db, session_ids=page_ids, **link_filters
            )
            session_usage = aliased(ApiUsage, usage_links)
            rows = (
                self._account_sessions_query(
                    session_query=session_query.filter(self.model.id.in_(page_ids)),
                    usage_join=usage_links.c.session_id == self.model.id,
                    usage_model=session_usage,
                    summary_columns_available=self._summary_columns_available(db),
                    parent_session_id_available=self._parent_session_id_available(db),
                )
                .order_by(
                    func.coalesce(
                        func.max(session_usage.timestamp),
                        self.model.last_activity_at,
                        self.model.started_at,
                    ).desc(),
                    self.model.id.desc(),
                )
                .all()
            )
        else:
            rows = []

        session_ids = [str(row.id) for row in rows]
        latest_usage_by_session = _latest_gateway_usage_for_sessions(
            db,
            account_id=account_id,
            runtime_session_ids=session_ids,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )

        items = []
        for row in rows:
            summary = self._row_to_summary(row)
            latest_usage = latest_usage_by_session.get(str(row.id))
            if latest_usage is not None:
                summary["latest_model_alias"] = latest_usage.model_alias
                summary["latest_provider_name"] = latest_usage.provider_name
                summary["last_request_at"] = latest_usage.timestamp
            items.append(summary)

        if not use_model_page and total is not None:
            has_more = offset + len(items) < total
        return {"total": total, "has_more": has_more, "items": items}

    def _rank_page_from_all_usage(
        self,
        db: Session,
        *,
        session_query: Any,
        link_filters: Dict[str, Any],
        limit: int,
        offset: int,
    ) -> List[Any]:
        """Rank every listed session by its last request over all usage."""
        ranking_links = self._account_usage_links(db, narrow=True, **link_filters)
        last_request = (
            db.query(
                ranking_links.c.session_id.label("session_id"),
                func.max(ranking_links.c.timestamp).label("last_request_at"),
            )
            .group_by(ranking_links.c.session_id)
            .subquery("session_last_request")
        )
        return [
            row.id
            for row in session_query.outerjoin(
                last_request, last_request.c.session_id == self.model.id
            )
            .with_entities(self.model.id)
            .order_by(
                func.coalesce(
                    last_request.c.last_request_at,
                    self.model.last_activity_at,
                    self.model.started_at,
                ).desc(),
                self.model.id.desc(),
            )
            .limit(limit)
            .offset(offset)
            .all()
        ]

    def _rank_page_from_recent_usage(
        self,
        db: Session,
        *,
        session_query: Any,
        link_filters: Dict[str, Any],
        window: timedelta,
        limit: int,
        offset: int,
    ) -> Optional[List[Any]]:
        """The same page as ``_rank_page_from_all_usage``, from recent usage.

        A session ranks by ``coalesce(last request, last activity, start)``.
        Every session whose key is at or after a cutoff ``T`` either made a
        request at or after ``T`` (its last request is in the recent usage)
        or has an activity/start time at or after ``T``. Both sets are small
        and cheap to find, and the second gets its exact last request from
        its own usage. So the ranking of all sessions with key >= ``T`` is
        exact, and if the page ends at or after ``T`` it is the full answer.
        Otherwise ``None`` asks the caller for a wider window.
        """
        needed = offset + limit
        if needed <= 0:
            return []
        cutoff = datetime.now(UTC).replace(tzinfo=None) - window
        start_date = link_filters.get("start_date")
        if start_date is not None:
            start_utc = (
                start_date.astimezone(UTC).replace(tzinfo=None)
                if start_date.tzinfo
                else start_date
            )
            if start_utc >= cutoff:
                # The filter already bounds usage at least this tightly.
                return None
        recent_links = self._account_usage_links(
            db, narrow=True, **{**link_filters, "start_date": cutoff}
        )
        recent_request = (
            db.query(
                recent_links.c.session_id.label("session_id"),
                func.max(recent_links.c.timestamp).label("last_request_at"),
            )
            .group_by(recent_links.c.session_id)
            .subquery("session_recent_request")
        )
        fallback_key = func.coalesce(self.model.last_activity_at, self.model.started_at)
        candidates = (
            session_query.outerjoin(
                recent_request, recent_request.c.session_id == self.model.id
            )
            .filter(
                or_(
                    recent_request.c.last_request_at.isnot(None),
                    fallback_key >= cutoff,
                )
            )
            .with_entities(
                self.model.id,
                recent_request.c.last_request_at,
                fallback_key.label("fallback_key"),
            )
            .limit(_RANKING_WINDOW_MAX_CANDIDATES + 1)
            .all()
        )
        keys: Dict[Any, datetime] = {}
        unresolved = []
        for row in candidates:
            if row.last_request_at is not None:
                keys[row.id] = row.last_request_at
            else:
                unresolved.append(row)
        if len(candidates) > _RANKING_WINDOW_MAX_CANDIDATES or (
            len(unresolved) > _RANKING_WINDOW_MAX_UNRESOLVED
        ):
            # Too many sessions in the window to rank them cheaply here.
            return None
        if unresolved:
            # Recently active without a recent request: the key is the last
            # request (older than the cutoff) if there is one at all.
            older_links = self._account_usage_links(
                db,
                narrow=True,
                session_ids=[row.id for row in unresolved],
                **link_filters,
            )
            older = dict(
                db.query(
                    older_links.c.session_id,
                    func.max(older_links.c.timestamp),
                )
                .group_by(older_links.c.session_id)
                .all()
            )
            for row in unresolved:
                keys[row.id] = older.get(row.id) or row.fallback_key
        ranked = sorted(
            (
                (key, session_id)
                for session_id, key in keys.items()
                if key is not None and key >= cutoff
            ),
            reverse=True,
        )
        if len(ranked) < needed:
            return None
        return [session_id for _, session_id in ranked[offset:needed]]

    def list_note_targets(
        self,
        db: Session,
        *,
        account_id: str,
        agent: models.ManagedAgent,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[models.RuntimeSession], int]:
        """Page live conversations for an account's agent, without bootstrap rows."""
        enrollment = models.ManagedAgentEnrollment
        identity = (
            db.query(enrollment.id)
            .filter(
                enrollment.account_id == account_id,
                enrollment.managed_agent_id == agent.id,
                enrollment.enrollment_type == "runtime_session_bootstrap",
                enrollment.managed_config["runtime_session_id"].astext
                == cast(self.model.id, String),
                self.model.session_source_type != "flow_execution",
                self.model.session_source_type == agent.session_source_type,
                self.model.session_source_id == agent.session_source_id,
                self.model.runtime_principal_id == agent.session_source_id,
            )
            .exists()
        )
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.ended_at.is_(None),
            or_(
                self.model.id == agent.runtime_session_id,
                self._principal_condition(
                    agent.session_source_type, agent.session_source_id
                ),
                self._principal_condition("managed_agent", str(agent.id)),
            ),
            ~identity,
        )
        total = query.count()
        sessions = (
            query.order_by(self.model.started_at.desc(), self.model.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return sessions, total

    def _principal_id_condition(self, runtime_principal_id: str) -> Any:
        """Match a principal id exactly or any per-run variant of it.

        A managed agent's principal id is the *base* id (e.g. ``custom_ABC``).
        Per-run sessions key off a derived id that appends the
        X-Preloop-Session-Id as ``<base>:<run-id>``. Match the base exactly OR
        any per-run variant so agent-scoped views surface every run, not just
        sessions whose principal id equals the base verbatim.
        """
        escaped = (
            runtime_principal_id.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return or_(
            self.model.runtime_principal_id == runtime_principal_id,
            self.model.runtime_principal_id.like(f"{escaped}:%", escape="\\"),
        )

    def _principal_condition(self, principal_type: str, principal_id: str) -> Any:
        """Match one ``(type, id)`` runtime principal, per-run variants included."""
        return and_(
            self.model.runtime_principal_type == principal_type,
            self._principal_id_condition(principal_id),
        )

    @staticmethod
    def _account_usage_links(
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        ai_model_id: Optional[str],
        session_ids: Optional[Sequence[Any]] = None,
        narrow: bool = False,
    ) -> Any:
        """Map account usage to sessions using disjoint, equality-join branches.

        The legacy flow attribution must remain supported, but combining it
        with runtime_session_id in one OR join forces repeated scans of usage
        for each session. Separate branches allow hash/index joins, including
        for the legacy UUID-to-text comparison. A row matching both paths for
        the same session is included only once.

        Args:
            db: Database session.
            account_id: Account whose gateway usage is mapped.
            start_date: Inclusive lower bound on usage time, if set.
            end_date: Exclusive upper bound on usage time, if set.
            ai_model_id: Restrict usage to one model, if set.
            session_ids: Restrict both the direct and legacy attribution
                branches to these sessions.
            narrow: Select only the timestamp column, which is all the page
                ranking reads.
        """
        # Keep the UNION narrow even if the planner materializes a branch.
        # Request/response metadata is only inspected by the exclusion filter.
        # ``narrow`` keeps only the timestamp, which is all the page ranking
        # reads.
        usage_columns = (
            [ApiUsage.timestamp.label("timestamp")]
            if narrow
            else [
                ApiUsage.id,
                ApiUsage.flow_id,
                ApiUsage.flow_execution_id,
                ApiUsage.model_alias,
                ApiUsage.provider_name,
                ApiUsage.status_code,
                ApiUsage.prompt_tokens,
                ApiUsage.completion_tokens,
                ApiUsage.total_tokens,
                ApiUsage.estimated_cost,
                ApiUsage.timestamp,
                ApiUsage.cache_read_tokens,
                ApiUsage.cache_creation_tokens,
            ]
        )
        usage = db.query(*usage_columns).filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "model_gateway",
            _exclude_internal_usage_condition(),
        )
        if start_date is not None:
            usage = usage.filter(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            usage = usage.filter(ApiUsage.timestamp < end_date)
        if ai_model_id is not None:
            usage = usage.filter(ApiUsage.ai_model_id == ai_model_id)

        direct = usage.add_columns(
            ApiUsage.runtime_session_id.label("session_id")
        ).filter(ApiUsage.runtime_session_id.isnot(None))
        if session_ids is not None:
            direct = direct.filter(ApiUsage.runtime_session_id.in_(session_ids))
        legacy = (
            usage.add_columns(RuntimeSession.id.label("session_id"))
            .join(
                RuntimeSession,
                cast(ApiUsage.flow_execution_id, String)
                == RuntimeSession.session_source_id,
            )
            .filter(
                RuntimeSession.account_id == account_id,
                RuntimeSession.session_source_type == "flow_execution",
                ApiUsage.flow_execution_id.isnot(None),
                ApiUsage.runtime_session_id.is_distinct_from(RuntimeSession.id),
            )
        )
        if session_ids is not None:
            # Resolve the page's legacy flow sessions first. Joining on the
            # text cast cannot use the flow_execution_id index, so with the
            # page known the branch reads only those executions' usage, and
            # a page without flow sessions skips it. Otherwise the planner
            # re-ran an account-wide scan of flow usage once per session row.
            legacy_execution_ids = [
                row.session_source_id
                for row in db.query(RuntimeSession.session_source_id).filter(
                    RuntimeSession.account_id == account_id,
                    RuntimeSession.session_source_type == "flow_execution",
                    RuntimeSession.id.in_(session_ids),
                )
                if _is_uuid(row.session_source_id)
            ]
            if not legacy_execution_ids:
                return direct.subquery("session_usage_links")
            legacy = legacy.filter(
                RuntimeSession.id.in_(session_ids),
                ApiUsage.flow_execution_id.in_(legacy_execution_ids),
            )
        return direct.union_all(legacy).subquery("session_usage_links")

    def _account_sessions_query(
        self,
        *,
        session_query: Any,
        usage_join: Any,
        usage_model: Any,
        summary_columns_available: bool,
        parent_session_id_available: bool,
    ) -> Any:
        """Build the runtime session aggregate query."""
        summary_column = (
            literal_column("runtime_session.summary")
            if summary_columns_available
            else literal(None)
        )
        summary_updated_at_column = (
            literal_column("runtime_session.summary_updated_at")
            if summary_columns_available
            else literal(None)
        )
        title_column = (
            literal_column("runtime_session.title")
            if summary_columns_available
            else literal(None)
        )
        title_request_count_column = (
            literal_column("runtime_session.title_request_count")
            if summary_columns_available
            else literal(None)
        )
        parent_session_id_column = (
            self.model.parent_session_id
            if parent_session_id_available
            else literal(None).label("parent_session_id")
        )
        return (
            session_query.outerjoin(usage_model, usage_join)
            .outerjoin(Flow, usage_model.flow_id == Flow.id)
            .with_entities(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.alias,
                parent_session_id_column,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column.label("summary"),
                summary_updated_at_column.label("summary_updated_at"),
                title_column.label("title"),
                title_request_count_column.label("title_request_count"),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                self.model.legal_hold,
                func.max(cast(usage_model.flow_id, String)).label("flow_id"),
                func.max(Flow.name).label("flow_name"),
                func.max(cast(usage_model.flow_execution_id, String)).label(
                    "flow_execution_id"
                ),
                func.max(usage_model.model_alias).label("latest_model_alias"),
                func.max(usage_model.provider_name).label("latest_provider_name"),
                func.count(usage_model.id).label("request_count"),
                func.coalesce(
                    func.sum(case((usage_model.status_code < 400, 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((usage_model.status_code >= 400, 1), else_=0)), 0
                ).label("error_count"),
                func.coalesce(func.sum(usage_model.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(usage_model.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(usage_model.total_tokens), 0).label(
                    "total_tokens"
                ),
                func.coalesce(func.sum(usage_model.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.max(usage_model.timestamp).label("last_request_at"),
                *cache_split_columns(usage_model),
            )
            .group_by(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.alias,
                # PostgreSQL rejects a bound NULL constant in GROUP BY.
                *([parent_session_id_column] if parent_session_id_available else []),
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                *(
                    [
                        summary_column,
                        summary_updated_at_column,
                        title_column,
                        title_request_count_column,
                    ]
                    if summary_columns_available
                    else []
                ),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                self.model.legal_hold,
            )
        )

    def update_session_title(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        title: Optional[str],
        summary: Optional[str] = None,
        title_request_count: Optional[int] = None,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Persist a generated title (and optional summary) for one session.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session to update.
            title: Short human-readable title, or ``None`` to leave unchanged.
            summary: Optional longer summary; updates ``summary_updated_at``
                when it differs from the stored one.
            title_request_count: Session request count captured when the title
                was generated, used to drive the periodic refresh watermark.
            commit: Whether to commit the change.

        Returns:
            The updated runtime session, or ``None`` when it was not found.
        """
        db_obj = self.get_account_session(
            db, account_id=account_id, runtime_session_id=runtime_session_id
        )
        if db_obj is None:
            return None
        if title is not None:
            db_obj.title = title
        if summary is not None and (
            summary != db_obj.summary or db_obj.summary_updated_at is None
        ):
            # Only a changed summary moves the timestamp. A regeneration that
            # produced the same sentence has not updated anything, and a
            # moving timestamp would rewrite the search chunk for it.
            db_obj.summary = summary
            db_obj.summary_updated_at = datetime.now(UTC)
        if title_request_count is not None:
            db_obj.title_request_count = title_request_count
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()

        # Search corpus chunk for the session's own title and summary.
        # Imported here rather than at module import time because the
        # indexing service imports the CRUD package; the writer swallows its
        # own failures, and the guard below catches anything raised before it
        # reaches them, so a broken corpus never loses a title.
        from preloop.services import session_search_index

        try:
            session_search_index.index_session_summary(
                db,
                account_id=db_obj.account_id,
                runtime_session_id=db_obj.id,
                title=db_obj.title,
                summary=db_obj.summary,
                occurred_at=db_obj.summary_updated_at or db_obj.last_activity_at,
                meta_data={"session_source_type": db_obj.session_source_type},
                commit=commit,
            )
        except Exception:  # noqa: BLE001 - a title is never lost over search
            logger.warning(
                "Session summary indexing failed for session %s",
                runtime_session_id,
                exc_info=True,
            )
        return db_obj

    def get_account_session(
        self, db: Session, *, account_id: str, runtime_session_id: str
    ) -> Optional[RuntimeSession]:
        """Return one runtime session for an account."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id, self.model.id == runtime_session_id
            )
            .first()
        )

    def count_active_sessions(self, db: Session, *, account_id: str) -> int:
        """Count active (non-ended) runtime sessions for an account."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.ended_at.is_(None),
            )
            .count()
        )

    def count_active_sessions_by_model(
        self,
        db: Session,
        *,
        account_id: str,
        ai_model_ids: Sequence[str],
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Count active runtime sessions per AI model in a single query.

        The Models page needs this number for every model on screen. Asking
        per model meant one request (and one pooled connection) per row; one
        grouped query answers the whole page.

        Filters match :meth:`list_account_sessions` with ``status="active"``
        and an ``ai_model_id``: sessions the account owns that have not ended
        and carry at least one gateway request for the model inside the
        window. Internal purposes are not excluded here either, so the count
        agrees with the per-model list the detail page shows.

        Args:
            db: Database session.
            account_id: Account whose sessions are counted.
            ai_model_ids: Models to count for. An empty sequence returns ``{}``
                without touching the database.
            start_date: Inclusive lower bound on gateway request timestamp.
            end_date: Exclusive upper bound on gateway request timestamp.

        Returns:
            Mapping of model id (as a string) to active session count. Models
            with no active sessions are absent.
        """
        model_ids = [str(model_id) for model_id in ai_model_ids]
        if not model_ids:
            return {}

        query = (
            db.query(
                ApiUsage.ai_model_id.label("ai_model_id"),
                func.count(func.distinct(ApiUsage.runtime_session_id)).label(
                    "session_count"
                ),
            )
            .join(self.model, self.model.id == ApiUsage.runtime_session_id)
            .filter(
                self.model.account_id == account_id,
                self.model.ended_at.is_(None),
                ApiUsage.runtime_session_id.isnot(None),
                ApiUsage.action_type == "model_gateway",
                ApiUsage.ai_model_id.in_(model_ids),
            )
        )
        if start_date is not None:
            query = query.filter(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            query = query.filter(ApiUsage.timestamp < end_date)

        rows = query.group_by(ApiUsage.ai_model_id).all()
        return {str(row.ai_model_id): int(row.session_count or 0) for row in rows}

    def get_latest_by_principal(
        self,
        db: Session,
        *,
        account_id: str,
        principal_type: str,
        principal_id: str,
    ) -> Optional[RuntimeSession]:
        """Return the most recent runtime session for a given principal."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == principal_type,
                self.model.session_source_id.startswith(f"{principal_id}-")
                | (self.model.session_source_id == principal_id),
            )
            .order_by(self.model.created_at.desc())
            .first()
        )

    def get_account_session_summary(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ai_model_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return one runtime session with aggregated gateway usage."""
        usage_join = self._usage_join_conditions(
            start_date=start_date, end_date=end_date, ai_model_id=ai_model_id
        )
        summary_columns_available = self._summary_columns_available(db)
        parent_session_id_available = self._parent_session_id_available(db)
        summary_column = (
            literal_column("runtime_session.summary")
            if summary_columns_available
            else literal(None)
        )
        summary_updated_at_column = (
            literal_column("runtime_session.summary_updated_at")
            if summary_columns_available
            else literal(None)
        )
        title_column = (
            literal_column("runtime_session.title")
            if summary_columns_available
            else literal(None)
        )
        title_request_count_column = (
            literal_column("runtime_session.title_request_count")
            if summary_columns_available
            else literal(None)
        )
        parent_session_id_column = (
            self.model.parent_session_id
            if parent_session_id_available
            else literal(None).label("parent_session_id")
        )
        row = (
            db.query(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.alias,
                parent_session_id_column,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column.label("summary"),
                summary_updated_at_column.label("summary_updated_at"),
                title_column.label("title"),
                title_request_count_column.label("title_request_count"),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                self.model.legal_hold,
                func.max(cast(ApiUsage.flow_id, String)).label("flow_id"),
                func.max(Flow.name).label("flow_name"),
                func.max(cast(ApiUsage.flow_execution_id, String)).label(
                    "flow_execution_id"
                ),
                func.max(ApiUsage.model_alias).label("latest_model_alias"),
                func.max(ApiUsage.provider_name).label("latest_provider_name"),
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code < 400, 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code >= 400, 1), else_=0)), 0
                ).label("error_count"),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.max(ApiUsage.timestamp).label("last_request_at"),
                *cache_split_columns(),
            )
            .outerjoin(ApiUsage, usage_join)
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .filter(
                self.model.account_id == account_id, self.model.id == runtime_session_id
            )
            .group_by(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.alias,
                # PostgreSQL rejects a bound NULL constant in GROUP BY.
                *([parent_session_id_column] if parent_session_id_available else []),
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                *(
                    [
                        summary_column,
                        summary_updated_at_column,
                        title_column,
                        title_request_count_column,
                    ]
                    if summary_columns_available
                    else []
                ),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                self.model.legal_hold,
            )
            .first()
        )
        if row is None:
            return None
        summary = self._row_to_summary(row)
        latest_usage = _latest_gateway_usage_for_session(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        if latest_usage is not None:
            summary["latest_model_alias"] = latest_usage.latest_model_alias
            summary["latest_provider_name"] = latest_usage.latest_provider_name
            summary["last_request_at"] = latest_usage.last_request_at
        return summary

    @staticmethod
    def _runtime_session_columns(db: Session) -> frozenset[str]:
        """Return the runtime_session column names for this bind, cached."""
        bind = db.get_bind()
        if bind is None:
            bind = db.bind
        if bind is None:
            return frozenset()
        cache_key = id(bind)
        cached = _runtime_session_columns_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            names = frozenset(
                column["name"]
                for column in inspect(bind).get_columns("runtime_session")
            )
        except Exception:
            names = frozenset()
        _runtime_session_columns_cache[cache_key] = names
        return names

    @staticmethod
    def _summary_columns_available(db: Session) -> bool:
        """Return whether the runtime session summary migration has been applied."""
        return _SUMMARY_COLUMN_NAMES.issubset(
            CRUDRuntimeSession._runtime_session_columns(db)
        )

    @staticmethod
    def _parent_session_id_available(db: Session) -> bool:
        """Return whether the parent_session_id migration has been applied.

        This column lands in a later revision than the summary set, so it has
        its own probe. Selecting it against a not-yet-migrated schema 500s
        every list and detail request.
        """
        return "parent_session_id" in CRUDRuntimeSession._runtime_session_columns(db)

    @staticmethod
    def _usage_join_conditions(
        *,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        ai_model_id: Optional[str] = None,
    ):
        legacy_flow_execution_match = and_(
            RuntimeSession.session_source_type == "flow_execution",
            ApiUsage.flow_execution_id.isnot(None),
            cast(ApiUsage.flow_execution_id, String)
            == RuntimeSession.session_source_id,
        )
        conditions = [
            ApiUsage.account_id == RuntimeSession.account_id,
            ApiUsage.action_type == "model_gateway",
            or_(
                ApiUsage.runtime_session_id == RuntimeSession.id,
                legacy_flow_execution_match,
            ),
            # Exclude Preloop-internal usage (session optimization + title
            # generation) from the summed metrics so a session's token/cost/
            # request totals reflect only real agent traffic. This join is the
            # shared chokepoint for both ``get_account_session_summary`` and
            # ``list_account_sessions`` (via ``_account_sessions_query``), so a
            # single filter here fixes both aggregations. NULL-safe: rows with
            # NULL meta_data or NULL purpose are agent traffic and stay counted.
            _exclude_internal_usage_condition(),
        ]
        if start_date is not None:
            conditions.append(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            conditions.append(ApiUsage.timestamp < end_date)
        if ai_model_id is not None:
            conditions.append(ApiUsage.ai_model_id == ai_model_id)
        return and_(*conditions)

    @staticmethod
    def _row_to_summary(row) -> dict[str, Any]:
        now = datetime.now(UTC)
        last_observed_at = row.last_request_at or row.last_activity_at or row.started_at
        if last_observed_at is not None and last_observed_at.tzinfo is None:
            last_observed_at = last_observed_at.replace(tzinfo=UTC)
        elif last_observed_at is not None:
            last_observed_at = last_observed_at.astimezone(UTC)

        if row.ended_at is not None:
            activity_status = "ended"
            is_active_now = False
        elif (
            last_observed_at is not None
            and (now - last_observed_at) <= CRUDRuntimeSession.ACTIVE_WINDOW
        ):
            activity_status = "active_now"
            is_active_now = True
        else:
            activity_status = "idle"
            is_active_now = False

        return {
            "account_id": str(row.account_id),
            "id": str(row.id),
            "session_source_type": row.session_source_type,
            "session_source_id": row.session_source_id,
            "session_reference": row.session_reference,
            "alias": row.alias,
            "parent_session_id": (
                str(row.parent_session_id)
                if row.parent_session_id is not None
                else None
            ),
            "runtime_principal_type": row.runtime_principal_type,
            "runtime_principal_id": row.runtime_principal_id,
            "runtime_principal_name": row.runtime_principal_name,
            "summary": row.summary,
            "summary_updated_at": row.summary_updated_at,
            "title": row.title,
            "title_request_count": (
                int(row.title_request_count)
                if row.title_request_count is not None
                else None
            ),
            "started_at": row.started_at,
            "last_activity_at": row.last_activity_at,
            "ended_at": row.ended_at,
            "flow_id": row.flow_id,
            "flow_name": row.flow_name,
            "flow_execution_id": row.flow_execution_id,
            "latest_model_alias": row.latest_model_alias,
            "latest_provider_name": row.latest_provider_name,
            "is_active_now": is_active_now,
            "activity_status": activity_status,
            "total_requests": int(row.request_count or 0),
            "successful_requests": int(row.success_count or 0),
            "failed_requests": int(row.error_count or 0),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            **cache_split_from_row(row),
            "estimated_cost": float(row.estimated_cost or 0.0),
            "last_request_at": row.last_request_at,
            "legal_hold": bool(getattr(row, "legal_hold", False)),
        }


crud_runtime_session = CRUDRuntimeSession(RuntimeSession)
