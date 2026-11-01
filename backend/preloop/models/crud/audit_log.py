"""CRUD operations for AuditLog model."""

import functools
import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID, uuid4

from sqlalchemy import and_, case, false, func, or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased

from ..models.audit_log import AuditLog
from ..models.user import User
from .base import CRUDBase

logger = logging.getLogger(__name__)

# Policy decisions that can stand alone in the grouped timeline when no
# tool_call row was written for the same correlation_id.
STANDALONE_POLICY_ACTIONS = ("policy_deny", "policy_require_approval")
_GROUP_ANCHOR_ACTIONS = ("tool_call", *STANDALONE_POLICY_ACTIONS)
# Actions that always anchor their own group in the grouped timeline.
PRIMARY_ACTIONS = (
    "tool_call",
    "model_gateway_request",
    "authentication",
    "configuration_change",
    "permission_check",
    "role_assigned",
    "role_removed",
    "runtime_session_created",
    "runtime_session_updated",
    "runtime_session_ended",
)
# Only these events are children of a tool/policy chain. All other actions,
# including future event families, remain visible as standalone groups.
GROUP_CHILD_ACTIONS = (
    "policy_allow",
    "approval_created",
    "approval_approved",
    "approval_denied",
    "approval_expired",
    "approval_escalated",
    "ai_approval",
    "ai_approval_evaluated",
    "approval_notification_sent",
    "approval_tool_executed",
)
# The grouped total is counted per branch and summed, which is only right
# while no action can match both branches.
assert not set(PRIMARY_ACTIONS) & set(STANDALONE_POLICY_ACTIONS)
# Tool call statuses that mean the call ran and did not succeed.
_FAILED_TOOL_CALL_STATUSES = ("failed", "upstream_error")
# The only ``details`` keys the grouped timeline reads to build groups.
_GROUPING_DETAIL_KEYS = ("correlation_id", "decision", "approval_id", "estimated_cost")


class _GroupingRow(Protocol):
    """Fields the grouped timeline reads before it hydrates full rows."""

    id: Any
    action: str
    status: Optional[str]
    timestamp: datetime
    details: Optional[Dict[str, Any]]


def _audit_event_estimated_cost(details: Optional[Dict[str, Any]]) -> float:
    """Return the estimated spend stored on one audit event, if any."""
    if not details:
        return 0.0
    value = details.get("estimated_cost")
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _narrow_audit_rows(query: Any) -> List[_GroupingRow]:
    """Run ``query`` for the grouping fields only, not the full row.

    Grouping and outcome evaluation read four keys of ``details``. Tool call
    rows carry arguments and results in ``details``, so loading the full row
    for the 5000-event outcome window moved megabytes per request (#1592).
    The returned objects expose the attributes the grouping code reads, with
    ``details`` trimmed to those keys; :func:`_hydrate_groups` swaps in the
    full rows for the page that is returned.
    """
    rows = query.with_entities(
        AuditLog.id,
        AuditLog.action,
        AuditLog.status,
        AuditLog.timestamp,
        *(AuditLog.details[key].label(key) for key in _GROUPING_DETAIL_KEYS),
    ).all()
    projected: List[_GroupingRow] = []
    for row in rows:
        projected.append(
            SimpleNamespace(
                id=row.id,
                action=row.action,
                status=row.status,
                timestamp=row.timestamp,
                details={
                    key: getattr(row, key)
                    for key in _GROUPING_DETAIL_KEYS
                    if getattr(row, key) is not None
                },
            )
        )
    return projected


def _hydrate_groups(db: Session, groups: List[Dict[str, Any]]) -> None:
    """Replace the narrow rows of the returned groups with full rows, in place."""
    ids = {group["primary_event"].id for group in groups}
    for group in groups:
        ids.update(sub.id for sub in group["sub_events"])
    if not ids:
        return
    full = {row.id: row for row in db.query(AuditLog).filter(AuditLog.id.in_(ids))}
    for group in groups:
        primary = full.get(group["primary_event"].id, group["primary_event"])
        group["primary_event"] = primary
        group["sub_events"] = [full.get(sub.id, sub) for sub in group["sub_events"]]


_F = TypeVar("_F", bound=Callable[..., Any])


def _without_jit(func: _F) -> _F:
    """Run a grouped read with Postgres JIT off for its own statements.

    The anti-join probes of the grouped timeline are estimated at millions
    of cost units, far past ``jit_above_cost``, so every count and page was
    JIT-compiled: 150-270 ms of LLVM per statement on prod, more on a fresh
    backend, for statements that then execute in 20-100 ms (#1693). The
    previous value is restored afterwards, transaction-locally, so the rest
    of the request keeps the server's setting.
    """

    @functools.wraps(func)
    def wrapper(self: Any, db: Session, *args: Any, **kwargs: Any) -> Any:
        previous = db.execute(
            text("SELECT current_setting('jit'), set_config('jit', 'off', true)")
        ).scalar()
        try:
            return func(self, db, *args, **kwargs)
        finally:
            try:
                db.execute(
                    text("SELECT set_config('jit', :previous, true)"),
                    {"previous": previous},
                )
            except Exception:
                # An aborted transaction drops the local setting with it.
                logger.debug("Could not restore jit after a grouped read")

    return cast(_F, wrapper)


def _count_rows(db: Session, filters: List[Any], limit: Optional[int]) -> int:
    """Count audit rows matching ``filters``, stopping at ``limit + 1``.

    With a limit the count runs over a ``LIMIT`` subquery, so Postgres stops
    reading after ``limit + 1`` rows instead of visiting every match.
    """
    if limit is None:
        return (
            db.query(func.count()).select_from(AuditLog).filter(*filters).scalar() or 0
        )
    capped = db.query(AuditLog.id).filter(*filters).limit(limit + 1).subquery()
    return db.query(func.count()).select_from(capped).scalar() or 0


def _is_statement_timeout(exc: OperationalError) -> bool:
    """Whether ``exc`` is Postgres cancelling a statement for its timeout.

    Read through ``postgres_sqlstate``: psycopg2 exposes the code as
    ``pgcode``, psycopg3 as ``sqlstate``.
    """
    from .audit_chain import postgres_sqlstate

    return postgres_sqlstate(exc) == "57014"


def _account_actions(db: Session, account_id: str) -> List[str]:
    """Every distinct ``action`` the account's trail holds.

    A loose index scan of ``ix_audit_log_account_action_status_ts_ip``: one
    probe per distinct action (a few dozen), not a read of the trail. The
    grouped timeline uses it to turn "any action not otherwise grouped" into
    an ``IN`` list, because the ``NOT IN`` it replaces cannot narrow the
    index by action and walked every row in range: 627k index entries and
    5.8 s for 868 matches on the founder account (#1693).
    """
    rows = db.execute(
        text(
            """
            WITH RECURSIVE present(action) AS (
                SELECT min(action) FROM audit_log WHERE account_id = :account_id
                UNION ALL
                SELECT (
                    SELECT min(action) FROM audit_log
                    WHERE account_id = :account_id AND action > present.action
                )
                FROM present WHERE present.action IS NOT NULL
            )
            SELECT action FROM present WHERE action IS NOT NULL
            """
        ),
        {"account_id": account_id},
    ).all()
    return [row[0] for row in rows]


def audit_row_id(details: Optional[Dict[str, Any]]) -> UUID:
    """Primary key for an audit row.

    A policy match records the same id on ``policy_notice_hit.audit_event_id``
    and in ``details['audit_event_id']`` before the audit service writes the
    row. Using that value as the row id makes the two point at each other
    without the plugin having to learn a new argument.

    The audit service builds that payload with ``tool_args`` nested under
    ``details['tool_args']``, then ``details.update(extra_details)``.
    ``audit_event_id`` therefore has to be a top-level key. A nested
    ``details['extra_details']`` object is ignored. Any other value, or a
    missing one, falls back to a fresh id.

    Args:
        details: Audit details payload. ``audit_event_id`` is read only from
            the top level, which is where the plugin places ``extra_details``.

    Returns:
        The id the row should be inserted with.
    """
    raw = None if not details else details.get("audit_event_id")
    if isinstance(raw, UUID):
        return raw
    if raw is not None:
        try:
            return UUID(str(raw))
        except (TypeError, ValueError):
            pass
    return uuid4()


def _audit_ip_address(value: Optional[str]) -> Optional[str]:
    """Keep only a canonical IP; client headers cannot overflow the audit column."""
    if not value:
        return None
    try:
        # Interface scope identifiers are local metadata, not an IP address.
        return str(ipaddress.ip_address(value.split("%", 1)[0].strip()))
    except ValueError:
        return None


class CRUDAuditLog(CRUDBase[AuditLog]):
    """CRUD operations for audit logging."""

    def log_action(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        user_id: Optional[UUID] = None,
        action: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        status: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> AuditLog:
        """Log a security-sensitive action.

        Args:
            db: Database session
            account_id: The account this action belongs to (UUID or str)
            user_id: The user who performed the action (None for system actions)
            action: The action performed (e.g., 'permission_check', 'role_assigned')
            resource_type: The type of resource affected (e.g., 'issue', 'user', 'team')
            resource_id: The ID of the specific resource affected
            status: The result ('success', 'denied', 'failure')
            ip_address: The IP address of the request
            user_agent: The user agent string
            details: Additional context as JSON

        Returns:
            Created audit log record
        """
        # Convert UUID to string if needed
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        db_obj = AuditLog(
            id=audit_row_id(details),
            account_id=account_id_str,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            ip_address=_audit_ip_address(ip_address),
            user_agent=user_agent,
            details=details,
            timestamp=datetime.now(timezone.utc),
        )

        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def get_by_account(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        skip: int = 0,
        limit: int = 100,
        action: Optional[str] = None,
        status: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        consent_ref: Optional[str] = None,
    ) -> List[AuditLog]:
        """Get audit logs for an account with optional filters.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            skip: Number of records to skip
            limit: Maximum number of records to return
            action: Filter by action type
            status: Filter by status
            resource_type: Filter by resource type
            resource_id: Filter by the affected resource id
            start_date: Filter by start date
            consent_ref: Exact delegated grant consent reference
            end_date: Filter by end date

        Returns:
            List of audit logs matching the criteria
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        query = db.query(AuditLog).filter(AuditLog.account_id == account_id_str)

        if action:
            query = query.filter(AuditLog.action == action)
        if status:
            query = query.filter(AuditLog.status == status)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            query = query.filter(AuditLog.resource_id == resource_id)
        if consent_ref is not None:
            query = query.filter(
                AuditLog.details.op("->")("grant").op("->>")("consent_ref")
                == consent_ref
            )
        if start_date:
            query = query.filter(AuditLog.timestamp >= start_date)
        if end_date:
            query = query.filter(AuditLog.timestamp <= end_date)

        return query.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()

    def get_approval_correlation_id(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
    ) -> Optional[str]:
        """Return the correlation id of the call that created an approval.

        The ``approval_created`` row carries the correlation id of the tool
        call that paused for approval, the same id as that call's
        ``tool_call`` row. A replay after approval reuses it.

        Args:
            db: Database session
            account_id: The approval's account
            approval_id: The approval request id

        Returns:
            The correlation id, or ``None`` when no such row exists.
        """
        row = (
            db.query(AuditLog.details["correlation_id"].astext)
            .filter(
                AuditLog.account_id == str(account_id),
                AuditLog.action == "approval_created",
                AuditLog.details["approval_id"].astext == str(approval_id),
                AuditLog.details["correlation_id"].astext.isnot(None),
            )
            .order_by(AuditLog.timestamp.asc())
            .first()
        )
        return row[0] if row else None

    def get_by_user(
        self,
        db: Session,
        *,
        user_id: UUID,
        account_id: Union[UUID, str],
        skip: int = 0,
        limit: int = 100,
        days: int = 30,
        start_date: Optional[datetime] = None,
    ) -> List[AuditLog]:
        """Get audit logs for a specific user.

        Args:
            db: Database session
            user_id: The user to filter by
            account_id: The account to filter by (for isolation, UUID or str)
            skip: Number of records to skip
            limit: Maximum number of records to return
            days: Number of days to look back
            start_date: Explicit cutoff shared with a corresponding count query

        Returns:
            List of audit logs for the user
        """
        if start_date is None:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        return (
            db.query(AuditLog)
            .filter(
                AuditLog.user_id == user_id,
                AuditLog.account_id == account_id_str,
                AuditLog.timestamp >= start_date,
            )
            .order_by(AuditLog.timestamp.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_permission_denials(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        days: int = 7,
        limit: int = 100,
    ) -> List[AuditLog]:
        """Get recent permission denial events.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            days: Number of days to look back
            limit: Maximum number of records to return

        Returns:
            List of permission denial audit logs
        """
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        return (
            db.query(AuditLog)
            .filter(
                AuditLog.account_id == account_id_str,
                AuditLog.action == "permission_check",
                AuditLog.status == "denied",
                AuditLog.timestamp >= start_date,
            )
            .order_by(AuditLog.timestamp.desc())
            .limit(limit)
            .all()
        )

    def get_action_stats(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        days: int = 30,
    ) -> List[Dict[str, Any]]:
        """Get statistics for audit actions.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            days: Number of days to look back

        Returns:
            List of action statistics
        """
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        result = (
            db.query(
                AuditLog.action,
                AuditLog.status,
                func.count().label("count"),
            )
            .filter(
                AuditLog.account_id == account_id_str,
                AuditLog.timestamp >= start_date,
            )
            .group_by(AuditLog.action, AuditLog.status)
            .order_by(func.count().desc())
            .all()
        )

        return [
            {
                "action": row.action,
                "status": row.status,
                "count": row.count,
            }
            for row in result
        ]

    def get_user_activity(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        days: int = 30,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get user activity statistics.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            days: Number of days to look back
            limit: Maximum number of users to return

        Returns:
            List of user activity statistics
        """
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        result = (
            db.query(
                User.username,
                User.email,
                func.count().label("action_count"),
            )
            .join(AuditLog, AuditLog.user_id == User.id)
            .filter(
                AuditLog.account_id == account_id_str,
                AuditLog.timestamp >= start_date,
            )
            .group_by(User.username, User.email)
            .order_by(func.count().desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "username": row.username,
                "email": row.email,
                "action_count": row.action_count,
            }
            for row in result
        ]

    @_without_jit
    def get_grouped_by_correlation(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        skip: int = 0,
        limit: int = 50,
        event_type_filter: Optional[List[str]] = None,
        outcome_filter: Optional[List[str]] = None,
        outcome_match: str = "all",
        tool_name_filter: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        consent_ref: Optional[str] = None,
        min_cost: Optional[float] = None,
        max_cost: Optional[float] = None,
        count_limit: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Get audit logs grouped by correlation_id for the unified timeline.

        Returns primary events (tool_call + standalone) with their correlated
        sub-events (policy_*, approval_*). Events without a correlation_id are
        treated as standalone groups.

        Args:
            db: Database session
            account_id: Account to filter by
            skip: Pagination offset (over groups, not raw rows)
            limit: Max groups to return
            event_type_filter: Filter by event type(s). Plain values like
                ``"tool_call"`` match ``AuditLog.action``. Prefixed values like
                ``"config:mcp_server"`` match ``configuration_change`` events
                whose ``details->config_type`` equals the suffix.
            outcome_filter: Filter by outcome(s) ('allow', 'deny', 'require_approval', etc.)
            outcome_match: ``"all"`` (default) keeps a group only when every
                outcome in ``outcome_filter`` appears in its chain; ``"any"``
                keeps it when at least one does ("failed or denied").
            tool_name_filter: Filter by tool name (substring match)
            start_date: Filter by start date
            consent_ref: Exact delegated grant consent reference
            end_date: Filter by end date
            count_limit: Stop counting the total past this many groups. The
                exact count reads every matching row of the trail, which is
                most of the cost on a long one (#1592). A returned total
                greater than ``count_limit`` means "more than count_limit";
                it is at most ``count_limit + 1``. Totals computed from the
                outcome or cost window are exact already and not capped.

        Returns:
            Tuple of (list of group dicts, total primary event count)
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        if count_limit is not None and count_limit < 0:
            raise ValueError(f"count_limit must not be negative, not {count_limit}")
        if outcome_match not in ("all", "any"):
            raise ValueError(
                f"outcome_match must be 'all' or 'any', not {outcome_match!r}"
            )

        # Step 1: Fetch primary events (tool_call + non-tool standalone events)
        # Tool calls are the primary grouping anchors.
        # Non-tool events (auth, config_change, permission_check) are standalone.

        # Filters every count and page shares, whatever the event type.
        scope_filters: List[Any] = [AuditLog.account_id == account_id_str]
        if consent_ref is not None:
            scope_filters.append(
                AuditLog.details.op("->")("grant").op("->>")("consent_ref")
                == consent_ref
            )
        if start_date:
            scope_filters.append(AuditLog.timestamp >= start_date)
        if end_date:
            scope_filters.append(AuditLog.timestamp <= end_date)
        if tool_name_filter:
            scope_filters.append(AuditLog.resource_id.ilike(f"%{tool_name_filter}%"))

        # The total is counted per part: one count per OR branch, so the
        # plain action branch stays an index-only count.
        count_conditions: List[Any]

        # Apply event type filter
        if event_type_filter:
            # Separate plain action filters from config:* sub-type filters
            plain_actions: List[str] = []
            config_subtypes: List[str] = []
            for et in event_type_filter:
                if et.startswith("config:"):
                    config_subtypes.append(et.split(":", 1)[1])
                else:
                    plain_actions.append(et)

            # Build OR conditions: action IN plain_actions
            #                      OR (action='configuration_change' AND details->config_type IN subtypes)
            type_conditions = []
            if plain_actions:
                type_conditions.append(AuditLog.action.in_(plain_actions))
            if config_subtypes:
                type_conditions.append(
                    (AuditLog.action == "configuration_change")
                    & (AuditLog.details["config_type"].astext.in_(config_subtypes))
                )

            if not type_conditions:
                # No valid filters → return nothing
                return [], 0
            anchor_condition = or_(*type_conditions)
            count_conditions = [anchor_condition]
        else:
            # Policy decisions are normally sub-events of a tool_call. A
            # deny (or a require_approval that never executed) writes no
            # tool_call row, so such decisions anchor their own group.
            #
            # ``OFFSET 0`` keeps this a per-row probe of
            # ``ix_audit_log_account_correlation``. Without it Postgres may
            # hash every tool_call of the account instead, which reads (and
            # detoasts) each one's full ``details`` with its tool result:
            # on an account with a long history that hash alone took most
            # of the request (#1592).
            tool_call_row = aliased(AuditLog)
            has_tool_call = (
                db.query(tool_call_row.id)
                .filter(
                    tool_call_row.account_id == account_id_str,
                    tool_call_row.action == "tool_call",
                    tool_call_row.details["correlation_id"].astext
                    == AuditLog.details["correlation_id"].astext,
                )
                .offset(0)
                .exists()
            )
            standalone_condition = and_(
                AuditLog.action.in_(STANDALONE_POLICY_ACTIONS),
                ~has_tool_call,
            )
            # Keep unknown/core event families visible without duplicating the
            # known children that the grouping pass attaches below.
            grouped_actions = {
                *PRIMARY_ACTIONS,
                *STANDALONE_POLICY_ACTIONS,
                *GROUP_CHILD_ACTIONS,
            }
            other_actions = sorted(
                action
                for action in _account_actions(db, account_id_str)
                if action not in grouped_actions
            )
            other_condition = (
                AuditLog.action.in_(other_actions) if other_actions else false()
            )
            parent = aliased(AuditLog)
            has_direct_parent = (
                db.query(parent.id)
                .filter(
                    parent.account_id == account_id_str,
                    parent.action.in_(_GROUP_ANCHOR_ACTIONS),
                    parent.details["correlation_id"].astext
                    == AuditLog.details["correlation_id"].astext,
                )
                .offset(0)
                .exists()
            )
            # Nested, not a join: "an approval_created row for this
            # approval_id, which itself has an anchor". As one join the
            # planner drove it from a sequential scan of every
            # approval_created row in the table, once per candidate row; the
            # count then outlived the 30 s statement timeout on prod (#1693).
            # Nested, each probe is its own partial-index lookup
            # (ix_audit_log_account_approval, then the correlation index).
            created = aliased(AuditLog)
            approval_parent = aliased(AuditLog)
            created_has_parent = (
                db.query(approval_parent.id)
                .filter(
                    approval_parent.account_id == account_id_str,
                    approval_parent.action.in_(_GROUP_ANCHOR_ACTIONS),
                    approval_parent.details["correlation_id"].astext
                    == created.details["correlation_id"].astext,
                )
                .offset(0)
                .exists()
            )
            has_approval_parent = (
                db.query(created.id)
                .filter(
                    created.account_id == account_id_str,
                    created.action == "approval_created",
                    created.details["approval_id"].astext
                    == AuditLog.details["approval_id"].astext,
                    created_has_parent,
                )
                .offset(0)
                .exists()
            )
            orphan_condition = and_(
                AuditLog.action.in_(GROUP_CHILD_ACTIONS),
                ~has_direct_parent,
                ~has_approval_parent,
            )
            count_conditions = [
                AuditLog.action.in_(PRIMARY_ACTIONS),
                standalone_condition,
                other_condition,
                orphan_condition,
            ]
            anchor_condition = or_(*count_conditions)

        primary_query = db.query(AuditLog).filter(*scope_filters, anchor_condition)

        if outcome_filter:
            # When outcome_filter is used, we must evaluate the outcomes of groups.
            # To avoid OOM, limit the search space to the most recent 5000 primary events.
            primary_events = _narrow_audit_rows(
                primary_query.order_by(AuditLog.timestamp.desc()).limit(5000)
            )
            total = 0  # Will be recalculated after filtering
        elif min_cost is not None or max_cost is not None:
            primary_events = _narrow_audit_rows(
                primary_query.order_by(AuditLog.timestamp.desc()).limit(5000)
            )
            total = 0
        else:
            # Count total before pagination. The branches are disjoint
            # (different actions), so their counts add up.
            counted: Optional[int]
            try:
                # A savepoint, so a count the database cancels leaves the
                # transaction usable for the page below.
                with db.begin_nested():
                    counted = 0
                    for condition in count_conditions:
                        counted += _count_rows(
                            db, [*scope_filters, condition], count_limit
                        )
                        # Past the cap the total is reported as cap + 1
                        # whatever the other branches hold, so they are not
                        # counted. The plain-action branch goes first and is
                        # an index-only count; the anti-join branches after
                        # it were the cold tail of the Audit page (#1693).
                        if count_limit is not None and counted > count_limit:
                            break
            except OperationalError as exc:
                if not _is_statement_timeout(exc):
                    raise
                # The total is a label; the page is the content. A count
                # past the statement timeout used to 500 the whole Audit
                # page (#1693), so it degrades to a bound instead.
                logger.warning(
                    "Grouped audit total timed out for account %s; "
                    "reporting a lower bound",
                    account_id_str,
                )
                counted = None
            total = counted or 0
            if count_limit is not None:
                total = min(total, count_limit + 1)

            # Fetch the page of primary events
            primary_events = _narrow_audit_rows(
                primary_query.order_by(AuditLog.timestamp.desc())
                .offset(skip)
                .limit(limit)
            )
            if counted is None:
                # Past the cap means "more than the cap", which a count that
                # ran out of time on a large trail is. Without a cap: the
                # groups the page proves, a lower bound.
                total = (
                    count_limit + 1
                    if count_limit is not None
                    else skip + len(primary_events)
                )

        if not primary_events:
            return [], total if not outcome_filter else 0

        # Step 2: Collect correlation_ids from primary tool_call events
        correlation_ids = set()
        for event in primary_events:
            if event.action in _GROUP_ANCHOR_ACTIONS and event.details:
                cid = event.details.get("correlation_id")
                if cid:
                    correlation_ids.add(cid)

        # Step 3: Fetch sub-events correlated by correlation_id
        sub_events_map: Dict[str, List[_GroupingRow]] = {}
        if correlation_ids:
            # 3a: Fetch events that directly carry a correlation_id
            # Only the correlation ids in SQL; action and order are applied
            # here. A correlation id holds a handful of rows, but the planner
            # estimates thousands per id, so given an action list or an
            # ORDER BY timestamp it walked ix_audit_log_timestamp or the
            # action index across the account instead of probing
            # ix_audit_log_account_correlation: 18 s and then the statement
            # timeout for the Overview's exceptions on prod (#1693).
            sub_actions = {*STANDALONE_POLICY_ACTIONS, *GROUP_CHILD_ACTIONS}
            direct_subs = sorted(
                (
                    sub
                    for sub in _narrow_audit_rows(
                        db.query(AuditLog).filter(
                            AuditLog.account_id == account_id_str,
                            AuditLog.details["correlation_id"].astext.in_(
                                list(correlation_ids)
                            ),
                        )
                    )
                    if sub.action in sub_actions
                ),
                key=lambda sub: sub.timestamp,
            )

            for sub in direct_subs:
                cid = sub.details.get("correlation_id") if sub.details else None
                if cid:
                    sub_events_map.setdefault(cid, []).append(sub)

            # 3b: Chain approval lifecycle events via approval_id.
            # The "approval_created" sub-event has both correlation_id and
            # approval_id. Subsequent events (approved, denied, expired, etc.)
            # share the same approval_id but lack correlation_id.
            # Collect approval_ids from the direct subs, then fetch any
            # additional approval events that match.
            approval_id_to_cid: Dict[str, str] = {}
            seen_sub_ids = {sub.id for sub in direct_subs}
            for sub in direct_subs:
                if sub.action == "approval_created" and sub.details:
                    aid = sub.details.get("approval_id")
                    cid = sub.details.get("correlation_id")
                    if aid and cid:
                        approval_id_to_cid[aid] = cid

            if approval_id_to_cid:
                # The same shape as the direct sub-events, for the same
                # reason: only the approval-id probe in SQL.
                chained_subs = sorted(
                    (
                        sub
                        for sub in _narrow_audit_rows(
                            db.query(AuditLog).filter(
                                AuditLog.account_id == account_id_str,
                                AuditLog.details["approval_id"].astext.in_(
                                    list(approval_id_to_cid.keys())
                                ),
                            )
                        )
                        if sub.action in GROUP_CHILD_ACTIONS
                        # approval_created was fetched with the direct subs.
                        and sub.action != "approval_created"
                    ),
                    key=lambda sub: sub.timestamp,
                )

                for sub in chained_subs:
                    if sub.id in seen_sub_ids:
                        continue
                    aid = sub.details.get("approval_id") if sub.details else None
                    if aid and aid in approval_id_to_cid:
                        cid = approval_id_to_cid[aid]
                        sub_events_map.setdefault(cid, []).append(sub)

            # Sort each group's sub-events by timestamp
            for cid in sub_events_map:
                sub_events_map[cid].sort(key=lambda s: s.timestamp)

        # Step 4: Build groups
        groups = []
        for event in primary_events:
            cid = None
            if event.details:
                cid = event.details.get("correlation_id")

            sub_list = (
                sub_events_map.get(cid, [])
                if cid and event.action in _GROUP_ANCHOR_ACTIONS
                else []
            )
            # A standalone policy decision must not list itself as a sub-event.
            sub_list = [sub for sub in sub_list if sub.id != event.id]

            # Determine outcome from sub-events chain.
            # We track *all* intermediate outcomes so that filtering by
            # e.g. "require_approval" still matches groups that were later
            # approved/declined/expired.
            outcome = event.status  # default (final outcome shown in UI)
            if event.action in STANDALONE_POLICY_ACTIONS and event.details:
                # Same source of truth as a correlated decision below.
                outcome = event.details.get("decision") or outcome
            all_outcomes: set[str] = {outcome} if outcome else set()
            if event.action in _GROUP_ANCHOR_ACTIONS and sub_list:
                # Look for the policy decision
                for sub in sub_list:
                    if sub.action.startswith("policy_"):
                        decision = sub.details.get("decision") if sub.details else None
                        if decision:
                            outcome = decision
                            all_outcomes.add(decision)
                # If approval was required, check for final approval status
                for sub in sub_list:
                    if sub.action in ("approval_approved",):
                        outcome = "approved"
                        all_outcomes.add("approved")
                    elif sub.action in ("approval_denied",):
                        outcome = "declined"
                        all_outcomes.add("declined")
                    elif sub.action in ("approval_expired",):
                        outcome = "expired"
                        all_outcomes.add("expired")

                # A tool call that ran and then failed shows that failure, not
                # the "allow" that let it run: the decision is still in the
                # chain (and matched by an "allow" filter), but the row has to
                # say what happened. Otherwise a failure reads "allow".
                if event.action == "tool_call" and event.status in (
                    _FAILED_TOOL_CALL_STATUSES
                ):
                    outcome = event.status

                # The post-approval tool execution (async path) is the most
                # recent meaningful event in the chain — promote it to the
                # final outcome shown on the row so users can see at a glance
                # whether the actual tool call succeeded after approval.
                for sub in sub_list:
                    if sub.action == "approval_tool_executed":
                        outcome = sub.status or outcome
                        if sub.status:
                            all_outcomes.add(sub.status)

            # Apply outcome filter: by default every selected outcome must
            # appear somewhere in the chain (AND); with outcome_match="any"
            # one is enough (OR).
            if outcome_filter:
                wanted = set(outcome_filter)
                matched = (
                    bool(wanted & all_outcomes)
                    if outcome_match == "any"
                    else wanted.issubset(all_outcomes)
                )
                if not matched:
                    continue

            event_cost = _audit_event_estimated_cost(event.details)
            if min_cost is not None and event_cost < min_cost:
                continue
            if max_cost is not None and event_cost > max_cost:
                continue

            groups.append(
                {
                    "correlation_id": cid,
                    "primary_event": event,
                    "sub_events": sub_list,
                    "outcome": outcome,
                }
            )

        if outcome_filter or min_cost is not None or max_cost is not None:
            total = len(groups)
            groups = groups[skip : skip + limit]

        _hydrate_groups(db, groups)
        return groups, total

    def count_by_account(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        action: Optional[str] = None,
        status: Optional[str] = None,
        resource_type: Optional[str] = None,
        user_id: Optional[UUID] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        consent_ref: Optional[str] = None,
    ) -> int:
        """Count audit logs for an account with optional filters.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            action: Filter by action type
            status: Filter by status
            resource_type: Filter by resource type
            user_id: Filter by actor user
            start_date: Filter by start date
            consent_ref: Exact delegated grant consent reference
            end_date: Filter by end date

        Returns:
            Count of matching audit logs
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        query = db.query(func.count(AuditLog.id)).filter(
            AuditLog.account_id == account_id_str
        )

        if action:
            query = query.filter(AuditLog.action == action)
        if status:
            query = query.filter(AuditLog.status == status)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if user_id is not None:
            query = query.filter(AuditLog.user_id == user_id)
        if consent_ref is not None:
            query = query.filter(
                AuditLog.details.op("->")("grant").op("->>")("consent_ref")
                == consent_ref
            )
        if start_date:
            query = query.filter(AuditLog.timestamp >= start_date)
        if end_date:
            query = query.filter(AuditLog.timestamp <= end_date)

        return query.scalar()

    def get_installer_download_stats(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get aggregated stats for public installer downloads."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        base_query = db.query(AuditLog).filter(
            AuditLog.account_id == account_id_str,
            AuditLog.action == "installer_download",
            AuditLog.timestamp >= start_date,
            AuditLog.status == "success",
        )

        total_downloads = base_query.count()
        downloads_last_24h = (
            base_query.filter(AuditLog.timestamp >= day_ago).count()
            if total_downloads
            else 0
        )
        unique_ips = (
            base_query.with_entities(
                func.count(func.distinct(AuditLog.ip_address))
            ).scalar()
            or 0
        )
        pinned_downloads = (
            base_query.filter(
                AuditLog.details["requested_version"].astext.isnot(None)
            ).count()
            if total_downloads
            else 0
        )
        last_download_at = (
            base_query.with_entities(func.max(AuditLog.timestamp)).scalar()
            if total_downloads
            else None
        )

        resource_rows = (
            base_query.with_entities(
                AuditLog.resource_id.label("resource_id"),
                func.count().label("count"),
            )
            .group_by(AuditLog.resource_id)
            .all()
        )
        resource_counts = {
            (row.resource_id or "unknown"): row.count for row in resource_rows
        }

        version_expression = func.coalesce(
            AuditLog.details["requested_version"].astext, "latest"
        )
        version_rows = (
            base_query.with_entities(
                version_expression.label("version"),
                func.count().label("count"),
            )
            .group_by(version_expression)
            .order_by(func.count().desc(), version_expression.asc())
            .limit(5)
            .all()
        )
        version_counts = {
            row.version: row.count for row in version_rows if row.version is not None
        }

        return {
            "audit_enabled": True,
            "days": days,
            "total_downloads": total_downloads,
            "downloads_last_24h": downloads_last_24h,
            "unique_ips": unique_ips,
            "cli_downloads": resource_counts.get("cli", 0),
            "oss_downloads": resource_counts.get("oss", 0),
            "pinned_downloads": pinned_downloads,
            "latest_version_downloads": version_counts.get("latest", 0),
            "last_download_at": last_download_at,
            "top_versions": [
                {"version": row.version, "count": row.count}
                for row in version_rows
                if row.version is not None
            ],
        }

    def get_cli_activity_stats(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get aggregated stats for CLI check-ins (``cli_activity`` events).

        Relies on ``ix_audit_log_account_action_status_ts_ip`` for the
        ``COUNT(DISTINCT ip_address)`` aggregations as the audit log grows.

        Uses one scalar aggregation query plus one grouped versions query
        instead of separate round-trips per metric.
        """
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        base_filters = (
            AuditLog.account_id == account_id_str,
            AuditLog.action == "cli_activity",
            AuditLog.timestamp >= start_date,
            AuditLog.status == "success",
        )

        # COUNT(DISTINCT CASE WHEN ts >= day_ago THEN ip END) ignores NULLs
        # from rows outside the 24h window, matching a filtered distinct count.
        aggregates = (
            db.query(
                func.count().label("checkins_total"),
                func.count(func.distinct(AuditLog.ip_address)).label("unique_ips"),
                func.count(
                    func.distinct(
                        case((AuditLog.timestamp >= day_ago, AuditLog.ip_address))
                    )
                ).label("active_last_24h"),
                func.max(AuditLog.timestamp).label("last_seen_at"),
            )
            .filter(*base_filters)
            .one()
        )

        checkins_total = aggregates.checkins_total or 0
        if not checkins_total:
            return {
                "cli_checkins_total": 0,
                "cli_active_unique_ips": 0,
                "cli_active_last_24h": 0,
                "cli_last_seen_at": None,
                "top_cli_versions": [],
            }

        version_expression = func.coalesce(
            AuditLog.details["cli_version"].astext, "unknown"
        )
        version_rows = (
            db.query(
                version_expression.label("version"),
                func.count().label("count"),
            )
            .filter(*base_filters)
            .group_by(version_expression)
            .order_by(func.count().desc(), version_expression.asc())
            .limit(5)
            .all()
        )

        return {
            "cli_checkins_total": checkins_total,
            "cli_active_unique_ips": aggregates.unique_ips or 0,
            "cli_active_last_24h": aggregates.active_last_24h or 0,
            "cli_last_seen_at": aggregates.last_seen_at,
            "top_cli_versions": [
                {"version": row.version, "count": row.count}
                for row in version_rows
                if row.version is not None
            ],
        }


# Global instance
crud_audit_log = CRUDAuditLog(AuditLog)
