"""CRUD operations for AuditLog model."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models.audit_log import AuditLog
from ..models.user import User
from .base import CRUDBase


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
            account_id=account_id_str,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
            timestamp=datetime.now(timezone.utc),
        )

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
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
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
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
            start_date: Filter by start date
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
        if start_date:
            query = query.filter(AuditLog.timestamp >= start_date)
        if end_date:
            query = query.filter(AuditLog.timestamp <= end_date)

        return query.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()

    def get_by_user(
        self,
        db: Session,
        *,
        user_id: UUID,
        account_id: Union[UUID, str],
        skip: int = 0,
        limit: int = 100,
        days: int = 30,
    ) -> List[AuditLog]:
        """Get audit logs for a specific user.

        Args:
            db: Database session
            user_id: The user to filter by
            account_id: The account to filter by (for isolation, UUID or str)
            skip: Number of records to skip
            limit: Maximum number of records to return
            days: Number of days to look back

        Returns:
            List of audit logs for the user
        """
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

    def get_grouped_by_correlation(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        skip: int = 0,
        limit: int = 50,
        event_type_filter: Optional[List[str]] = None,
        outcome_filter: Optional[List[str]] = None,
        tool_name_filter: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        min_cost: Optional[float] = None,
        max_cost: Optional[float] = None,
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
            tool_name_filter: Filter by tool name (substring match)
            start_date: Filter by start date
            end_date: Filter by end date

        Returns:
            Tuple of (list of group dicts, total primary event count)
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        # Step 1: Fetch primary events (tool_call + non-tool standalone events)
        # Tool calls are the primary grouping anchors.
        # Non-tool events (auth, config_change, permission_check) are standalone.
        primary_actions = [
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
        ]

        primary_query = db.query(AuditLog).filter(
            AuditLog.account_id == account_id_str,
        )

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

            if type_conditions:
                primary_query = primary_query.filter(or_(*type_conditions))
            else:
                # No valid filters → return nothing
                return [], 0
        else:
            primary_query = primary_query.filter(
                AuditLog.action.in_(primary_actions),
            )

        if start_date:
            primary_query = primary_query.filter(AuditLog.timestamp >= start_date)
        if end_date:
            primary_query = primary_query.filter(AuditLog.timestamp <= end_date)
        if tool_name_filter:
            primary_query = primary_query.filter(
                AuditLog.resource_id.ilike(f"%{tool_name_filter}%")
            )

        if outcome_filter:
            # When outcome_filter is used, we must evaluate the outcomes of groups.
            # To avoid OOM, limit the search space to the most recent 5000 primary events.
            primary_events = (
                primary_query.order_by(AuditLog.timestamp.desc()).limit(5000).all()
            )
            total = 0  # Will be recalculated after filtering
        elif min_cost is not None or max_cost is not None:
            primary_events = (
                primary_query.order_by(AuditLog.timestamp.desc()).limit(5000).all()
            )
            total = 0
        else:
            # Count total before pagination
            total = primary_query.count()

            # Fetch the page of primary events
            primary_events = (
                primary_query.order_by(AuditLog.timestamp.desc())
                .offset(skip)
                .limit(limit)
                .all()
            )

        if not primary_events:
            return [], total if not outcome_filter else 0

        # Step 2: Collect correlation_ids from primary tool_call events
        correlation_ids = set()
        for event in primary_events:
            if event.action == "tool_call" and event.details:
                cid = event.details.get("correlation_id")
                if cid:
                    correlation_ids.add(cid)

        # Step 3: Fetch sub-events correlated by correlation_id
        sub_events_map: Dict[str, List[AuditLog]] = {}
        if correlation_ids:
            # 3a: Fetch events that directly carry a correlation_id
            direct_subs = (
                db.query(AuditLog)
                .filter(
                    AuditLog.account_id == account_id_str,
                    AuditLog.details["correlation_id"].astext.in_(
                        list(correlation_ids)
                    ),
                    AuditLog.action != "tool_call",
                )
                .order_by(AuditLog.timestamp.asc())
                .all()
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
                chained_subs = (
                    db.query(AuditLog)
                    .filter(
                        AuditLog.account_id == account_id_str,
                        AuditLog.details["approval_id"].astext.in_(
                            list(approval_id_to_cid.keys())
                        ),
                        AuditLog.action != "approval_created",  # already fetched
                    )
                    .order_by(AuditLog.timestamp.asc())
                    .all()
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

            sub_list = sub_events_map.get(cid, []) if cid else []

            # Determine outcome from sub-events chain.
            # We track *all* intermediate outcomes so that filtering by
            # e.g. "require_approval" still matches groups that were later
            # approved/declined/expired.
            outcome = event.status  # default (final outcome shown in UI)
            all_outcomes: set[str] = {outcome} if outcome else set()
            if event.action == "tool_call" and sub_list:
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

                # The post-approval tool execution (async path) is the most
                # recent meaningful event in the chain — promote it to the
                # final outcome shown on the row so users can see at a glance
                # whether the actual tool call succeeded after approval.
                for sub in sub_list:
                    if sub.action == "approval_tool_executed":
                        outcome = sub.status or outcome
                        if sub.status:
                            all_outcomes.add(sub.status)

            # Apply outcome filter — match only if *all* selected outcomes
            # appear somewhere in the chain (AND semantics).
            if outcome_filter and not set(outcome_filter).issubset(all_outcomes):
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

        return groups, total

    def count_by_account(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        action: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> int:
        """Count audit logs for an account with optional filters.

        Args:
            db: Database session
            account_id: The account to filter by (UUID or str)
            action: Filter by action type
            status: Filter by status
            start_date: Filter by start date
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
        """Get aggregated stats for CLI check-ins (``cli_activity`` events)."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        base_query = db.query(AuditLog).filter(
            AuditLog.account_id == account_id_str,
            AuditLog.action == "cli_activity",
            AuditLog.timestamp >= start_date,
            AuditLog.status == "success",
        )

        checkins_total = base_query.count()
        if not checkins_total:
            return {
                "cli_checkins_total": 0,
                "cli_active_unique_ips": 0,
                "cli_active_last_24h": 0,
                "cli_last_seen_at": None,
                "top_cli_versions": [],
            }

        unique_ips = (
            base_query.with_entities(
                func.count(func.distinct(AuditLog.ip_address))
            ).scalar()
            or 0
        )
        active_last_24h = (
            base_query.filter(AuditLog.timestamp >= day_ago)
            .with_entities(func.count(func.distinct(AuditLog.ip_address)))
            .scalar()
            or 0
        )
        last_seen_at = base_query.with_entities(func.max(AuditLog.timestamp)).scalar()

        version_expression = func.coalesce(
            AuditLog.details["cli_version"].astext, "unknown"
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

        return {
            "cli_checkins_total": checkins_total,
            "cli_active_unique_ips": unique_ips,
            "cli_active_last_24h": active_last_24h,
            "cli_last_seen_at": last_seen_at,
            "top_cli_versions": [
                {"version": row.version, "count": row.count}
                for row in version_rows
                if row.version is not None
            ],
        }


# Global instance
crud_audit_log = CRUDAuditLog(AuditLog)
