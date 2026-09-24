"""CRUD operations for RuntimeSessionActivity."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, case, func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.browser_step import BrowserStepIn
from preloop.utils.redaction import redact_dict
from ...utils.jsonb_sanitize import sanitize_for_jsonb
from .base import CRUDBase

ManagedAgent = models.ManagedAgent
RuntimeSession = models.RuntimeSession
RuntimeSessionActivity = models.RuntimeSessionActivity

MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN = 2000

# One governed tool call now records ``succeeded``/``refused``/``failed``.
# ``success`` is kept in the success set so rows written before the outcome
# split still aggregate as successes.
TOOL_CALL_SUCCESS_STATUSES = ("success", "succeeded")


def _tool_call_failure_count_expr(status_column: Any) -> Any:
    """Count non-success tool-call statuses, treating NULL like the legacy CASE.

    ``status != 'success'`` is not true when status is NULL, so the old
    aggregates left NULL out of both success and failure. Keep that behaviour
    while accepting both ``success`` and ``succeeded``.
    """
    return func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        status_column.isnot(None),
                        ~status_column.in_(TOOL_CALL_SUCCESS_STATUSES),
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    )


def _redact_browser_text(value: str | None) -> tuple[str | None, bool]:
    """Mask credential-shaped text, leaving ``None`` and empty values alone."""
    if not value:
        return value, False
    from preloop.services.session_search_index import redact_text

    return redact_text(value)


def _browser_step_metadata(step: BrowserStepIn) -> dict[str, Any]:
    """Build the stored metadata for one browser step.

    Field names are redacted first, then the free-text URL, target and
    reasoning are masked. ``screenshot`` stays ``None`` until capture
    exists. ``source`` and ``source_step_id`` are left intact so a retry
    still matches the idempotency key.
    """
    metadata = redact_dict(
        {
            "source": step.source,
            "source_step_id": step.source_step_id,
            "step_index": step.step_index,
            "action": step.action,
            "url": step.url,
            "target": step.target,
            "reasoning": step.reasoning,
            "extra": step.extra,
            "screenshot": None,
        }
    )
    for key in ("url", "target", "reasoning"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            metadata[key] = _redact_browser_text(value)[0]
    return metadata


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
            metadata_=sanitize_for_jsonb(metadata),
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

        self._index_tool_call_chunks(db, activity=db_obj, commit=commit)
        return db_obj

    def log_artifact_evicted(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        artifact_id: Any,
        kind: str,
        commit: bool = True,
    ) -> RuntimeSessionActivity:
        """Record that an artifact's bytes were dropped for the account budget.

        Args:
            db: Database session.
            account_id: Owning account.
            runtime_session_id: Session the artifact belonged to.
            artifact_id: Artifact whose ciphertext was cleared.
            kind: ``screenshot`` or ``recording``.
            commit: When True, commit the row. When False, only flush.

        Returns:
            The new ``artifact_evicted`` activity.
        """
        activity_timestamp = datetime.now(timezone.utc)
        db_obj = RuntimeSessionActivity(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_type="artifact_evicted",
            summary="Session artifact evicted for the account storage budget",
            metadata_=sanitize_for_jsonb(
                {
                    "artifact_id": str(artifact_id),
                    "kind": kind,
                    "reason": "account_budget",
                }
            ),
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
        else:
            db.flush()
        return db_obj

    def set_browser_step_screenshot_availability(
        self,
        db: Session,
        *,
        account_id: Any,
        activity_id: Any,
        availability: str,
        commit: bool = True,
    ) -> bool:
        """Set ``metadata.screenshot.availability`` on a browser-step activity.

        Args:
            db: Database session.
            account_id: Account the caller is allowed to update.
            activity_id: Activity the artifact illustrates.
            availability: New availability, for example ``evicted``.
            commit: When True, commit the update. When False, only flush.

        Returns:
            True when a ``browser_step`` row in this account was updated.
        """
        from sqlalchemy.orm.attributes import flag_modified

        row = (
            db.query(self.model)
            .filter(
                self.model.id == activity_id,
                self.model.account_id == account_id,
                self.model.activity_type == "browser_step",
            )
            .one_or_none()
        )
        if row is None:
            return False
        metadata = dict(row.metadata_ or {})
        screenshot = metadata.get("screenshot")
        if not isinstance(screenshot, dict):
            screenshot = {}
        metadata["screenshot"] = {**screenshot, "availability": availability}
        row.metadata_ = sanitize_for_jsonb(metadata)
        flag_modified(row, "metadata_")
        if commit:
            db.commit()
        else:
            db.flush()
        return True

    def log_browser_step(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        api_key_id: Optional[Any],
        step: BrowserStepIn,
        commit: bool = True,
    ) -> tuple[RuntimeSessionActivity, bool]:
        """Persist one browser step, or return the existing row.

        Idempotency is ``(runtime_session_id, source, source_step_id)``
        among ``browser_step`` rows. A repeat returns that row unchanged
        and does not touch the session again. A concurrent insert that
        wins the unique index is treated the same way.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Session the step is attached to.
            api_key_id: Credential that reported the step, if any.
            step: Validated ``BrowserStepIn``.
            commit: Whether to commit this row. Batch ingestion passes
                ``False`` and commits once for the batch.

        Returns:
            The stored row and whether this call created it.
        """
        existing = self._find_browser_step(
            db,
            runtime_session_id=runtime_session_id,
            source=step.source,
            source_step_id=step.source_step_id,
        )
        if existing is not None:
            return existing, False

        metadata = _browser_step_metadata(step)
        locator = metadata.get("url") or metadata.get("target") or ""
        summary = _redact_browser_text(f"{step.action} {locator}")[0]
        activity_timestamp = step.occurred_at or datetime.now(timezone.utc)
        if activity_timestamp.tzinfo is None:
            activity_timestamp = activity_timestamp.replace(tzinfo=timezone.utc)
        db_obj = RuntimeSessionActivity(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            api_key_id=api_key_id,
            activity_type="browser_step",
            server_name=step.source,
            tool_name=step.action,
            status=step.status,
            summary=summary,
            metadata_=sanitize_for_jsonb(metadata),
            timestamp=activity_timestamp,
        )
        savepoint = db.begin_nested()
        try:
            db.add(db_obj)
            # Flush the insert before touching the session. A conflicting
            # key fails here, instead of as an autoflush inside the touch
            # query, so the savepoint can roll the duplicate back.
            db.flush()
        except IntegrityError:
            savepoint.rollback()
            if db_obj in db:
                db.expunge(db_obj)
            with db.no_autoflush:
                raced = self._find_browser_step(
                    db,
                    runtime_session_id=runtime_session_id,
                    source=step.source,
                    source_step_id=step.source_step_id,
                )
            if raced is None:
                raise
            return raced, False
        else:
            if savepoint.is_active:
                savepoint.commit()
        self._touch_runtime_session_and_agent(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            activity_timestamp=activity_timestamp,
        )
        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj, True

    def _find_browser_step(
        self,
        db: Session,
        *,
        runtime_session_id: Any,
        source: str,
        source_step_id: str,
    ) -> Optional[RuntimeSessionActivity]:
        """Return the browser step already stored for this idempotency key."""
        return (
            db.query(self.model)
            .filter(
                self.model.runtime_session_id == runtime_session_id,
                self.model.activity_type == "browser_step",
                self.model.metadata_["source"].astext == source,
                self.model.metadata_["source_step_id"].astext == source_step_id,
            )
            .first()
        )

    @staticmethod
    def _index_tool_call_chunks(
        db: Session, *, activity: RuntimeSessionActivity, commit: bool
    ) -> None:
        """Write the activity into the session search corpus.

        Imported here rather than at module import time because the indexing
        service imports the CRUD package. The writer swallows its own
        failures, so a broken corpus never loses a tool call.
        """
        from preloop.services.session_search_index import index_tool_call

        if activity.id is None:
            # The row id is a column default, so an uncommitted activity only
            # has one after a flush; the chunk keys on it.
            db.flush()

        index_tool_call(
            db,
            account_id=activity.account_id,
            runtime_session_id=activity.runtime_session_id,
            source_id=activity.id,
            server_name=activity.server_name,
            tool_name=activity.tool_name,
            status=activity.status,
            summary=activity.summary,
            occurred_at=activity.timestamp,
            api_key_id=activity.api_key_id,
            meta_data={"activity_type": activity.activity_type},
            commit=commit,
        )

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
            metadata_=sanitize_for_jsonb(metadata),
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
            metadata_=sanitize_for_jsonb(metadata),
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
        if original is not None and "result_status" in (original.metadata_ or {}):
            return original
        previous_result = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.metadata_["command_id"].astext == command_id,
                self.model.metadata_["source"].astext == "agent_control_result",
            )
            .first()
        )
        if previous_result is not None:
            return previous_result
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
            **(metadata or {}),
            # Identity and deduplication markers belong to the server. Runtime
            # metadata must not disguise a result as another activity/command.
            "command_id": command_id,
            "role": "assistant",
            "direction": "agent_to_operator",
            "source": "agent_control_result",
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

    def list_tool_calls_page(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        before_timestamp: Optional[datetime] = None,
        before_activity_id: Optional[Any] = None,
        limit: int = 100,
    ) -> list[RuntimeSessionActivity]:
        """Return one page of a session's tool calls, newest first.

        Keyed on ``(timestamp, id)`` so a backfill can walk a long session
        across passes without an offset skipping rows written in between.
        """
        stmt = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.runtime_session_id == runtime_session_id,
            self.model.activity_type == "tool_call",
        )
        if before_timestamp is not None and before_activity_id is not None:
            stmt = stmt.filter(
                tuple_(self.model.timestamp, self.model.id)
                < tuple_(before_timestamp, before_activity_id)
            )
        return (
            stmt.order_by(self.model.timestamp.desc(), self.model.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )

    def list_model_gateway_calls_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        start_date: Optional[datetime] = None,
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
                self.model.timestamp >= start_date if start_date is not None else True,
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
        start_date: Optional[datetime] = None,
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
                self.model.timestamp >= start_date if start_date is not None else True,
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
        start_date: Optional[datetime] = None,
    ) -> int:
        """Return the number of model gateway call activities for a session."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.timestamp >= start_date if start_date is not None else True,
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
        start_date: Optional[datetime] = None,
        activity_id: Any,
    ) -> Optional[RuntimeSessionActivity]:
        """Return a single model gateway call activity by id."""
        return (
            db.query(self.model)
            .filter(
                self.model.id == activity_id,
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.timestamp >= start_date if start_date is not None else True,
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
                    func.sum(
                        case(
                            (self.model.status.in_(TOOL_CALL_SUCCESS_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("success_count"),
                _tool_call_failure_count_expr(self.model.status).label("failure_count"),
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
                    func.sum(
                        case(
                            (self.model.status.in_(TOOL_CALL_SUCCESS_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("success_count"),
                _tool_call_failure_count_expr(self.model.status).label("failure_count"),
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

    def get_tool_summary_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Aggregate tool-call activity by server/tool for one account."""
        query = db.query(
            self.model.server_name,
            self.model.tool_name,
            func.count(self.model.id).label("call_count"),
            func.coalesce(
                func.sum(
                    case(
                        (self.model.status.in_(TOOL_CALL_SUCCESS_STATUSES), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("success_count"),
            _tool_call_failure_count_expr(self.model.status).label("failure_count"),
            func.max(self.model.timestamp).label("last_activity_at"),
        ).filter(
            self.model.account_id == account_id,
            self.model.activity_type == "tool_call",
            self.model.tool_name.isnot(None),
        )
        if start_date is not None:
            query = query.filter(self.model.timestamp >= start_date)
        if end_date is not None:
            query = query.filter(self.model.timestamp < end_date)
        rows = (
            query.group_by(self.model.server_name, self.model.tool_name)
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

    def get_tool_invocations_by_agent_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Aggregate tool-call counts by tool and runtime principal for one account."""
        query = (
            db.query(
                self.model.tool_name,
                RuntimeSession.runtime_principal_type,
                RuntimeSession.runtime_principal_id,
                RuntimeSession.runtime_principal_name,
                func.count(self.model.id).label("call_count"),
            )
            .join(RuntimeSession, self.model.runtime_session_id == RuntimeSession.id)
            .filter(
                self.model.account_id == account_id,
                self.model.activity_type == "tool_call",
                self.model.tool_name.isnot(None),
            )
        )
        if start_date is not None:
            query = query.filter(self.model.timestamp >= start_date)
        if end_date is not None:
            query = query.filter(self.model.timestamp < end_date)
        rows = (
            query.group_by(
                self.model.tool_name,
                RuntimeSession.runtime_principal_type,
                RuntimeSession.runtime_principal_id,
                RuntimeSession.runtime_principal_name,
            )
            .order_by(func.count(self.model.id).desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "tool_name": row.tool_name,
                "runtime_principal_type": row.runtime_principal_type,
                "runtime_principal_id": row.runtime_principal_id,
                "runtime_principal_name": row.runtime_principal_name,
                "call_count": int(row.call_count or 0),
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
                self.model.status.in_(TOOL_CALL_SUCCESS_STATUSES),
            )
            .order_by(self.model.timestamp.desc())
            .limit(limit)
            .all()
        )


crud_runtime_session_activity = CRUDRuntimeSessionActivity(RuntimeSessionActivity)
