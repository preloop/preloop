"""CRUD operations for RuntimeSessionActivity."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..models.managed_agent import ManagedAgent
from ..models.runtime_session import RuntimeSession
from ..models.runtime_session_activity import RuntimeSessionActivity
from .base import CRUDBase

MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN = 2000


class CRUDRuntimeSessionActivity(CRUDBase[RuntimeSessionActivity]):
    """CRUD helpers for normalized runtime-session activity."""

    def _touch_runtime_session_and_agent(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        activity_timestamp: datetime,
    ) -> None:
        """Update session last-activity and linked managed-agent presence."""
        runtime_session = db.get(RuntimeSession, runtime_session_id)
        if runtime_session is None:
            return

        runtime_session.last_activity_at = activity_timestamp
        db.add(runtime_session)

        if (
            not runtime_session.runtime_principal_type
            or not runtime_session.runtime_principal_id
        ):
            return

        managed_agent = (
            db.query(ManagedAgent)
            .filter(
                ManagedAgent.account_id == account_id,
                ManagedAgent.session_source_type
                == runtime_session.runtime_principal_type,
                ManagedAgent.session_source_id == runtime_session.runtime_principal_id,
            )
            .first()
        )
        if managed_agent is None or managed_agent.lifecycle_state != "active":
            return

        managed_agent.runtime_session_id = runtime_session.id
        managed_agent.last_seen_at = activity_timestamp
        db.add(managed_agent)

    def log_tool_call(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        server_name: Optional[str],
        tool_name: Optional[str],
        status: str,
        summary: Optional[str] = None,
        flow_execution_id: Optional[Any] = None,
        api_key_id: Optional[Any] = None,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
        commit: bool = True,
    ) -> RuntimeSessionActivity:
        """Persist one normalized tool-call activity item."""
        activity_timestamp = timestamp or datetime.now(timezone.utc)
        db_obj = RuntimeSessionActivity(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            flow_execution_id=flow_execution_id,
            api_key_id=api_key_id,
            activity_type="tool_call",
            server_name=server_name,
            tool_name=tool_name,
            status=status,
            summary=summary,
            metadata_=metadata,
            timestamp=activity_timestamp,
        )
        db.add(db_obj)
        self._touch_runtime_session_and_agent(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_timestamp=activity_timestamp,
        )

        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj

    def log_model_gateway_call(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        status: str,
        summary: Optional[str] = None,
        flow_execution_id: Optional[Any] = None,
        api_key_id: Optional[Any] = None,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
        commit: bool = True,
    ) -> RuntimeSessionActivity:
        """Persist one normalized model gateway activity item."""
        activity_timestamp = timestamp or datetime.now(timezone.utc)
        db_obj = RuntimeSessionActivity(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            flow_execution_id=flow_execution_id,
            api_key_id=api_key_id,
            activity_type="model_gateway_call",
            status=status,
            summary=summary,
            metadata_=metadata,
            timestamp=activity_timestamp,
        )
        db.add(db_obj)
        self._touch_runtime_session_and_agent(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_timestamp=activity_timestamp,
        )

        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj

    def log_agent_control_message(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        message: str,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
        commit: bool = True,
    ) -> RuntimeSessionActivity:
        """Persist one operator-to-agent control message."""
        activity_timestamp = timestamp or datetime.now(timezone.utc)
        summary = message[:MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN]
        db_obj = RuntimeSessionActivity(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_type="agent_control_message",
            status=status,
            summary=summary,
            metadata_=metadata,
            timestamp=activity_timestamp,
        )
        db.add(db_obj)
        self._touch_runtime_session_and_agent(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_timestamp=activity_timestamp,
        )

        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj

    def log_agent_control_result(
        self,
        db: Session,
        *,
        account_id: Any,
        command_id: str,
        fallback_runtime_session_id: Any,
        status: str,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
        commit: bool = True,
    ) -> RuntimeSessionActivity | None:
        """Persist a runtime result for a previously routed operator command."""
        original = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.activity_type == "agent_control_message",
                self.model.metadata_["command_id"].astext == command_id,
            )
            .order_by(self.model.timestamp.desc())
            .first()
        )
        runtime_session_id = (
            original.runtime_session_id
            if original is not None
            else fallback_runtime_session_id
        )

        if original is not None:
            original.status = status
            original.metadata_ = {
                **(original.metadata_ or {}),
                "result_status": status,
            }
            db.add(original)

        if not message:
            activity_timestamp = timestamp or datetime.now(timezone.utc)
            self._touch_runtime_session_and_agent(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                activity_timestamp=activity_timestamp,
            )
            if commit:
                db.commit()
            return original

        result_metadata = {
            "command_id": command_id,
            "role": "assistant",
            "direction": "agent_to_operator",
            "source": "agent_control_result",
            **(metadata or {}),
        }
        return self.log_agent_control_message(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            message=message,
            status=status,
            metadata=result_metadata,
            timestamp=timestamp,
            commit=commit,
        )

    def list_for_runtime_session(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RuntimeSessionActivity]:
        """Return recent normalized activity for one runtime session."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .order_by(self.model.timestamp.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def list_model_gateway_calls_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        tail: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
        metadata_only: bool = False,
    ) -> list[Any]:
        """Return latest-first model gateway call activities for one session."""
        metadata_column = (
            self.model.metadata_.op("-")("request")
            .op("-")("response")
            .op("-")("messages")
            .label("metadata_")
        )
        if metadata_only:
            metadata_column = func.jsonb_build_object(
                "metadata_only",
                True,
                "api_usage_id",
                self.model.metadata_["api_usage_id"].astext,
                "model_alias",
                self.model.metadata_["model_alias"].astext,
                "provider_name",
                self.model.metadata_["provider_name"].astext,
                "endpoint",
                self.model.metadata_["endpoint"].astext,
                "endpoint_kind",
                self.model.metadata_["endpoint_kind"].astext,
                "status_code",
                self.model.metadata_["status_code"].astext,
                "outcome",
                self.model.metadata_["outcome"].astext,
                "error_detail",
                self.model.metadata_["error_detail"].astext,
                "upstream_request_id",
                self.model.metadata_["upstream_request_id"].astext,
                "request_fingerprint",
                self.model.metadata_["request_fingerprint"].astext,
                "gateway_attempt",
                self.model.metadata_["gateway_attempt"].astext,
                "is_retry",
                self.model.metadata_["is_retry"].astext,
                "retry_of_api_usage_id",
                self.model.metadata_["retry_of_api_usage_id"].astext,
                "prompt_tokens",
                self.model.metadata_["prompt_tokens"].astext,
                "completion_tokens",
                self.model.metadata_["completion_tokens"].astext,
                "total_tokens",
                self.model.metadata_["total_tokens"].astext,
                "estimated_cost",
                self.model.metadata_["estimated_cost"].astext,
                "tool_name",
                self.model.metadata_["tool_name"].astext,
            ).label("metadata_")
        query = (
            db.query(
                self.model.id,
                self.model.timestamp,
                self.model.activity_type,
                metadata_column,
            )
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.activity_type == "model_gateway_call",
            )
            .order_by(self.model.timestamp.desc())
        )
        limit = min(tail, 200) if tail else min(max(limit, 1), 100)
        if metadata_only:
            limit = min(max(limit, 1), 5000)
        query = query.limit(limit).offset(max(offset, 0))
        return query.all()

    def list_full_model_gateway_call_payloads_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        limit: int = 50,
    ) -> list[Any]:
        """Return latest-first gateway call rows with complete stored metadata.

        Unlike :meth:`list_model_gateway_calls_for_session`, the returned
        ``metadata_`` retains the captured ``request``/``response`` payloads so
        callers can analyze full message and tool-schema content.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.
            limit: Maximum number of rows (capped at 100).

        Returns:
            Rows of ``(id, timestamp, metadata_)`` ordered latest-first.
        """
        return (
            db.query(self.model.id, self.model.timestamp, self.model.metadata_)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.activity_type == "model_gateway_call",
            )
            .order_by(self.model.timestamp.desc())
            .limit(min(max(limit, 1), 100))
            .all()
        )

    def list_recent_model_gateway_call_payloads_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent stored gateway metadata payloads for summary refreshes."""
        rows = (
            db.query(self.model.metadata_)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.activity_type == "model_gateway_call",
            )
            .order_by(self.model.timestamp.desc())
            .limit(min(max(limit, 1), 20))
            .all()
        )
        return [
            row.metadata_ for row in reversed(rows) if isinstance(row.metadata_, dict)
        ]

    def count_model_gateway_calls_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
    ) -> int:
        """Return the number of model gateway call activities for a session."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.activity_type == "model_gateway_call",
            )
            .count()
        )

    def get_model_gateway_call_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        activity_id: Any,
    ) -> Optional[RuntimeSessionActivity]:
        """Return a single model gateway call activity by id."""
        return (
            db.query(self.model)
            .filter(
                self.model.id == activity_id,
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .first()
        )

    def get_server_summary_for_principal(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_principal_type: str,
        runtime_principal_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Aggregate tool-call activity by server for one durable runtime principal."""
        rows = (
            db.query(
                self.model.server_name,
                func.count(self.model.id).label("call_count"),
                func.coalesce(
                    func.sum(case((self.model.status == "success", 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((self.model.status != "success", 1), else_=0)), 0
                ).label("failure_count"),
                func.max(self.model.timestamp).label("last_activity_at"),
            )
            .join(RuntimeSession, self.model.runtime_session_id == RuntimeSession.id)
            .filter(
                self.model.account_id == account_id,
                RuntimeSession.runtime_principal_type == runtime_principal_type,
                RuntimeSession.runtime_principal_id == runtime_principal_id,
                self.model.activity_type == "tool_call",
            )
            .group_by(self.model.server_name)
            .order_by(
                func.count(self.model.id).desc(), func.max(self.model.timestamp).desc()
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "server_name": row.server_name,
                "call_count": int(row.call_count or 0),
                "successful_calls": int(row.success_count or 0),
                "failed_calls": int(row.failure_count or 0),
                "last_activity_at": row.last_activity_at,
            }
            for row in rows
        ]

    def get_tool_summary_for_principal(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_principal_type: str,
        runtime_principal_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Aggregate tool-call activity by server/tool for one durable runtime principal."""
        rows = (
            db.query(
                self.model.server_name,
                self.model.tool_name,
                func.count(self.model.id).label("call_count"),
                func.coalesce(
                    func.sum(case((self.model.status == "success", 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((self.model.status != "success", 1), else_=0)), 0
                ).label("failure_count"),
                func.max(self.model.timestamp).label("last_activity_at"),
            )
            .join(RuntimeSession, self.model.runtime_session_id == RuntimeSession.id)
            .filter(
                self.model.account_id == account_id,
                RuntimeSession.runtime_principal_type == runtime_principal_type,
                RuntimeSession.runtime_principal_id == runtime_principal_id,
                self.model.activity_type == "tool_call",
            )
            .group_by(self.model.server_name, self.model.tool_name)
            .order_by(
                func.count(self.model.id).desc(), func.max(self.model.timestamp).desc()
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "server_name": row.server_name,
                "tool_name": row.tool_name,
                "call_count": int(row.call_count or 0),
                "successful_calls": int(row.success_count or 0),
                "failed_calls": int(row.failure_count or 0),
                "last_activity_at": row.last_activity_at,
            }
            for row in rows
        ]

    def list_tool_calls_for_flow_execution(
        self,
        db: Session,
        *,
        account_id: Any,
        flow_execution_id: Any,
    ) -> list[RuntimeSessionActivity]:
        """Return tool_call activities for one flow execution, oldest first."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.flow_execution_id == flow_execution_id,
                self.model.activity_type == "tool_call",
            )
            .order_by(self.model.timestamp.asc())
            .all()
        )

    def get_last_tool_call_timestamp(
        self, db: Session, api_key_id: Any
    ) -> Optional[datetime]:
        """Get the most recent timestamp of a tool call by this API key."""
        return (
            db.query(func.max(self.model.timestamp))
            .filter(
                self.model.api_key_id == api_key_id,
                self.model.activity_type == "tool_call",
            )
            .scalar()
        )

    def get_recent_tool_calls_count(
        self, db: Session, api_key_id: Any, recent_start: datetime
    ) -> int:
        """Get the count of tool calls made by this API key since recent_start."""
        return (
            db.query(func.count(self.model.id))
            .filter(
                self.model.api_key_id == api_key_id,
                self.model.activity_type == "tool_call",
                self.model.timestamp >= recent_start,
            )
            .scalar()
            or 0
        )

    def get_tool_call_count_by_flow_execution(
        self, db: Session, flow_execution_id: Any
    ) -> int:
        """Count tool calls for a specific flow execution."""
        return (
            db.query(func.count(self.model.id))
            .filter(
                self.model.flow_execution_id == flow_execution_id,
                self.model.activity_type == "tool_call",
            )
            .scalar()
            or 0
        )

    def get_recent_successful_tool_calls_by_flow_execution(
        self, db: Session, flow_execution_id: Any, limit: int = 12
    ) -> list[RuntimeSessionActivity]:
        """Return recent successful tool call activities for a flow execution."""
        return (
            db.query(self.model)
            .filter(
                self.model.flow_execution_id == flow_execution_id,
                self.model.activity_type == "tool_call",
                self.model.status == "success",
            )
            .order_by(self.model.timestamp.desc())
            .limit(limit)
            .all()
        )


crud_runtime_session_activity = CRUDRuntimeSessionActivity(RuntimeSessionActivity)
