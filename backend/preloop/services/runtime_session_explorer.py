"""Product-facing runtime session explorer service."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException
import litellm
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.models.ai_model import AIModel
from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_gateway_usage_search_document,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_runtime_session_optimization_result,
)
from preloop.models.models.account import Account
from preloop.services.litellm_routing import to_litellm_model
from preloop.services.model_credentials import (
    build_aux_kwargs,
    call_with_default_model_fallback,
    check_reasoning_model_empty_content,
)
from preloop.schemas.gateway_usage import (
    AccountGatewayUsageSearchResponse,
    AccountRuntimeSessionDetailResponse,
    AccountRuntimeSessionListResponse,
    GatewayTokenUsage,
    RuntimeSessionActivityItem,
    RuntimeSessionActivityListResponse,
    RuntimeSessionInteractionSummary,
    RuntimeSessionSummaryInsight,
    RuntimeSessionSummary,
)
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.utils.request_fingerprint import public_request_fingerprint

logger = logging.getLogger(__name__)

# Bound the per-request cost of list-path title generation: at most this many
# sessions are titled per list call (most-recent first); the rest get titled on
# subsequent loads.
MAX_TITLES_PER_LIST_REQUEST = 8

# Refresh a session's title once its request count has grown by at least this
# many requests since the title was last generated.
TITLE_REFRESH_REQUEST_DELTA = 5

# Per-attempt provider I/O bound for interaction summaries. Matches the
# approval-summary attempt budget so a cancelled call does not sit on
# litellm's 600s default while run_db_off_loop drains the worker.
INTERACTION_SUMMARY_ATTEMPT_TIMEOUT_SECONDS = 5.0


_TRANSCRIPT_ROLE_TITLES = {
    "user": "User message",
    "assistant": "Assistant message",
    "tool": "Tool result",
    "tool_use": "Tool call",
}


def _default_activity_title(activity: Any) -> str:
    """Human title for an activity row that names no tool.

    Transcript messages (ingested from harness hooks that opted into
    transcript storage) are titled by role so a session timeline reads as
    a conversation; operator messages and tool calls keep their labels.
    """
    activity_type = getattr(activity, "activity_type", None)
    if activity_type == "agent_control_message":
        return "Operator message"
    if activity_type == "transcript_message":
        metadata = getattr(activity, "metadata_", None) or {}
        role = str(metadata.get("role") or "").lower()
        return _TRANSCRIPT_ROLE_TITLES.get(role, "Transcript message")
    return "Tool call"


class RuntimeSessionExplorerService:
    """Build runtime session explorer responses."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_account_sessions(
        self,
        *,
        account: Account,
        query: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        session_source_type: Optional[str] = None,
        status: str = "all",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> AccountRuntimeSessionListResponse:
        start_date, end_date = self._normalize_period(start_date, end_date)
        results = crud_runtime_session.list_account_sessions(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            query=query,
            ai_model_id=ai_model_id,
            session_source_type=session_source_type,
            status=status,
            limit=limit,
            offset=offset,
        )
        raw_items = results["items"]
        self.schedule_missing_session_titles(
            account_id=str(account.id),
            rows=raw_items,
            background_tasks=background_tasks,
        )
        items = [self._summary_row_to_schema(item) for item in raw_items]
        self._attach_optimization_badges(account=account, items=items)
        return AccountRuntimeSessionListResponse(
            period_start=start_date,
            period_end=end_date,
            query=query,
            session_source_type=session_source_type,
            status=status,
            total=results["total"],
            limit=limit,
            offset=offset,
            items=items,
        )

    def _attach_optimization_badges(
        self, *, account: Account, items: list[RuntimeSessionSummary]
    ) -> None:
        """Attach cached waste-score badges to session summaries in place.

        Reads previously generated optimization results (no model calls) so
        the list view can cheaply surface which sessions are worth
        optimizing.

        Args:
            account: Owning account.
            items: Session summaries for the current page.
        """
        if not items:
            return
        try:
            cached_rows = crud_runtime_session_optimization_result.list_for_sessions(
                self.db,
                account_id=account.id,
                runtime_session_ids=[item.id for item in items],
            )
        except SQLAlchemyError:
            logger.debug(
                "Optimization badge lookup failed; returning plain list",
                exc_info=True,
            )
            return
        latest_by_session: dict[str, dict] = {}
        for row in cached_rows:
            session_id = str(row.runtime_session_id)
            if session_id not in latest_by_session and isinstance(row.response, dict):
                latest_by_session[session_id] = row.response
        for item in items:
            response = latest_by_session.get(item.id)
            if not response:
                continue
            waste_score = response.get("waste_score")
            if isinstance(waste_score, (int, float)):
                item.optimization_waste_score = int(waste_score)
            savings_tokens = response.get("potential_savings_tokens")
            if isinstance(savings_tokens, (int, float)):
                item.optimization_potential_savings_tokens = int(savings_tokens)
            savings_usd = response.get("potential_savings_usd")
            if isinstance(savings_usd, (int, float)):
                item.optimization_potential_savings_usd = float(savings_usd)

    def schedule_missing_session_titles(
        self,
        *,
        account_id: str,
        rows: list[dict],
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> None:
        """Schedule best-effort title generation off the request path.

        Selects the most-recent sessions on the current page that are untitled,
        or whose request count has grown by at least
        :data:`TITLE_REFRESH_REQUEST_DELTA` since the title was last generated,
        bounded to :data:`MAX_TITLES_PER_LIST_REQUEST` sessions; the remainder
        are picked up on subsequent loads. The actual (LLM-backed) generation is
        registered as a FastAPI background task so it runs *after* the HTTP
        response is sent and never blocks it. The generated titles therefore
        appear on a later load, not in the response that scheduled them.

        When no ``background_tasks`` is provided (e.g. a non-request caller),
        this is a no-op so the hot path never makes inline LLM calls.

        Args:
            account_id: Owning account id.
            rows: Raw summary rows for the current page (most-recent first).
            background_tasks: FastAPI background-task registry from the request.
        """
        if not rows or background_tasks is None:
            return
        session_ids: list[str] = []
        for row in rows:
            if len(session_ids) >= MAX_TITLES_PER_LIST_REQUEST:
                break
            if self._session_needs_title(row):
                session_ids.append(str(row["id"]))
        if not session_ids:
            return
        background_tasks.add_task(
            self._run_title_generation,
            account_id=account_id,
            session_ids=session_ids,
        )

    @staticmethod
    def _run_title_generation(*, account_id: str, session_ids: list[str]) -> None:
        """Generate titles for the given sessions in a fresh DB session.

        Runs as a FastAPI background task *after* the request DB session has
        been closed, so it must NOT reuse the request's session. It opens its
        own session from the app session factory and always closes it. Each
        title is generated through the EE billing plugin via the plugin service
        registry (so the OSS path carries no hard EE import); the per-session
        daily cap and best-effort guards live inside that service. Any failure
        is swallowed so a background title call never surfaces to the user.

        Args:
            account_id: Owning account id.
            session_ids: Bounded list of runtime session ids to title.
        """
        if not session_ids:
            return
        try:
            from preloop.plugins.base import get_plugin_manager

            generate_title = get_plugin_manager().get_service("session_title_service")
        except Exception:
            logger.debug(
                "Session title service unavailable; skipping background titles",
                exc_info=True,
            )
            return
        if generate_title is None:
            return

        from preloop.models.db.session import get_session_factory

        db = get_session_factory()()
        try:
            for session_id in session_ids:
                try:
                    generate_title(
                        db,
                        account_id=account_id,
                        runtime_session_id=session_id,
                    )
                except Exception:
                    logger.info(
                        "Background session title generation raised; "
                        "leaving session untitled",
                        exc_info=True,
                    )
        finally:
            try:
                db.close()
            except Exception:
                logger.debug(
                    "Closing background title DB session failed", exc_info=True
                )

    @staticmethod
    def _session_needs_title(row: dict) -> bool:
        """Return whether a session row should (re)generate its title."""
        if not row.get("title"):
            return True
        current_requests = int(row.get("total_requests") or 0)
        last_titled_at = int(row.get("title_request_count") or 0)
        return current_requests - last_titled_at >= TITLE_REFRESH_REQUEST_DELTA

    def get_account_session_detail(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> AccountRuntimeSessionDetailResponse:
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
            start_date=start_date,
            end_date=end_date,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")

        flow_execution_id = summary_row.get("flow_execution_id")
        usage_by_model = crud_api_usage.get_gateway_usage_by_model(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
            flow_execution_id=flow_execution_id,
            start_date=start_date,
            end_date=end_date,
            limit=20,
        )

        return AccountRuntimeSessionDetailResponse(
            period_start=start_date,
            period_end=end_date,
            session=self._summary_row_to_schema(summary_row),
            usage_by_model=[
                ModelGatewayUsageService._model_row_to_schema(row)
                for row in usage_by_model
            ],
        )

    def get_account_session_interactions(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        interaction_query: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        interaction_limit: int = 50,
        interaction_offset: int = 0,
    ) -> AccountGatewayUsageSearchResponse:
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
            start_date=start_date,
            end_date=end_date,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")

        flow_execution_id = summary_row.get("flow_execution_id")
        interactions = crud_gateway_usage_search_document.search_account_documents(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            runtime_session_id=runtime_session_id,
            flow_execution_id=flow_execution_id,
            query=interaction_query,
            limit=interaction_limit,
            offset=interaction_offset,
        )
        interaction_items = [
            ModelGatewayUsageService._search_row_to_schema(item)
            for item in interactions["items"]
        ]

        return AccountGatewayUsageSearchResponse(
            period_start=start_date,
            period_end=end_date,
            query=interaction_query,
            total=interactions["total"],
            limit=interaction_limit,
            offset=interaction_offset,
            items=interaction_items,
        )

    def get_account_session_activity_timeline(
        self,
        *,
        account: Account,
        runtime_session_id: str,
    ) -> RuntimeSessionActivityListResponse:
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")

        # The timeline builder also expects interactions in the current structure
        # In a highly optimized world, we might fetch just the metadata rather than
        # the full SearchDocument. For now, limit the interactions we merge into timeline.
        start_date, end_date = self._normalize_period(None, None)
        flow_execution_id = summary_row.get("flow_execution_id")
        interactions = crud_gateway_usage_search_document.search_account_documents(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            runtime_session_id=runtime_session_id,
            flow_execution_id=flow_execution_id,
            limit=100,
            offset=0,
        )
        interaction_items = [
            ModelGatewayUsageService._search_row_to_schema(item)
            for item in interactions["items"]
        ]

        items = self._build_activity_timeline(
            summary_row=summary_row,
            interactions=interaction_items,
        )
        return RuntimeSessionActivityListResponse(items=items)

    def get_account_session_summary_insight(
        self,
        *,
        account: Account,
        runtime_session_id: str,
    ) -> RuntimeSessionSummaryInsight:
        """Return a lightweight summary with default-fast-model metadata.

        This endpoint is intentionally zero-spend for now: it identifies the
        configured default model so the UI can show readiness, while deriving a
        deterministic summary from captured usage until persisted model-backed
        summaries are enabled.
        """
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")
        summary = self._summary_row_to_schema(summary_row)
        fast_model = crud_ai_model.get_default_active_model(
            self.db, account_id=str(account.id)
        )
        risk_level = "high" if summary.failed_requests else "low"
        if summary.estimated_cost > 1 and risk_level == "low":
            risk_level = "medium"
        highlights = [
            f"{summary.total_requests} model request"
            f"{'' if summary.total_requests == 1 else 's'}",
            f"{summary.token_usage.total_tokens} total tokens",
            f"${summary.estimated_cost:.4f} estimated spend",
        ]
        if summary.latest_model_alias:
            highlights.append(f"Latest model: {summary.latest_model_alias}")
        return RuntimeSessionSummaryInsight(
            title=f"{summary.session_reference or summary.session_source_type} session",
            description=(
                "Captured failures are present; inspect failed gateway events "
                "and related audit rows before optimizing."
                if summary.failed_requests
                else "No captured gateway failures were found. Expand requests "
                "to inspect prompts, context, tools, and payload details."
            ),
            risk_level=risk_level,
            highlights=highlights,
            next_action=(
                "Review failed request details."
                if summary.failed_requests
                else (
                    "Inspect prompt context for trimming opportunities."
                    if summary.token_usage.prompt_tokens > 100_000
                    else None
                )
            ),
            generated_by="local",
            fast_model_name=fast_model.name if fast_model else None,
            estimated_summary_cost=0.0,
        )

    async def summarize_account_runtime_session_interaction(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        activity_id: str,
    ) -> RuntimeSessionInteractionSummary:
        """Summarize one gateway interaction on demand.

        This is intentionally per-interaction and opt-in so browsing the replay
        does not create hidden model spend.
        """
        session = crud_runtime_session.get_account_session(
            self.db, account_id=str(account.id), runtime_session_id=runtime_session_id
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")

        activity = crud_runtime_session_activity.get_model_gateway_call_for_session(
            self.db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
            activity_id=activity_id,
        )
        if activity is None:
            raise HTTPException(
                status_code=404, detail="Runtime session activity not found"
            )

        payload = activity.metadata_ or {}
        fallback = self._local_interaction_summary(
            event_id=str(activity.id),
            payload=payload,
        )
        model = crud_ai_model.get_default_active_model(
            self.db, account_id=str(account.id)
        )
        if model is None:
            return fallback

        def _call(model_to_use: AIModel, creds: dict[str, Any]) -> dict[str, Any]:
            return self._call_interaction_summary_model(
                model_to_use, creds, payload=payload
            )

        try:
            generated = await call_with_default_model_fallback(
                db=self.db,
                account_id=str(account.id),
                primary_model=model,
                caller=_call,
                operation_name="session_interaction_summary",
            )
            if generated is None:
                return fallback.model_copy(update={"model_name": model.name})
        except Exception:
            logger.info(
                "Falling back to local interaction summary",
                exc_info=True,
            )
            return fallback.model_copy(update={"model_name": model.name})

        return RuntimeSessionInteractionSummary(
            event_id=str(activity.id),
            title=str(generated.get("title") or fallback.title)[:160],
            summary=str(generated.get("summary") or fallback.summary)[:1000],
            key_points=[
                str(point)[:240]
                for point in generated.get("key_points", [])
                if isinstance(point, str) and point.strip()
            ][:5]
            or fallback.key_points,
            risk_level=str(generated.get("risk_level") or fallback.risk_level),
            next_action=(
                str(generated.get("next_action"))[:240]
                if generated.get("next_action")
                else fallback.next_action
            ),
            generated_by="model",
            model_name=model.name,
            estimated_summary_cost=0.0,
        )

    @staticmethod
    def _local_interaction_summary(
        *,
        event_id: str,
        payload: dict[str, Any],
    ) -> RuntimeSessionInteractionSummary:
        messages = RuntimeSessionExplorerService._extract_request_messages(payload)
        user_message = next(
            (
                message["text"]
                for message in reversed(messages)
                if message["role"] == "user" and message["text"]
            ),
            None,
        )
        model = payload.get("model_alias") or payload.get("requested_model")
        endpoint = payload.get("endpoint_kind") or payload.get("endpoint") or "request"
        status_code = payload.get("status_code")
        outcome = payload.get("outcome") or "unknown"
        title = f"{model or 'Model'} {outcome}"
        key_points = [
            f"{payload.get('total_tokens') or 0} tokens",
            f"{payload.get('prompt_tokens') or 0} prompt tokens",
            f"{payload.get('completion_tokens') or 0} completion tokens",
            f"{payload.get('method') or 'POST'} {endpoint}",
        ]
        if user_message:
            key_points.insert(0, f"User request: {user_message[:180]}")
        risk_level = (
            "high" if outcome == "error" or int(status_code or 0) >= 400 else "low"
        )
        return RuntimeSessionInteractionSummary(
            event_id=event_id,
            title=title,
            summary=(
                f"{model or 'The configured model'} handled {endpoint}. "
                f"{'Latest user request: ' + user_message[:500] if user_message else 'No user request was captured in the preview.'}"
            ),
            key_points=key_points,
            risk_level=risk_level,
            next_action="Inspect raw payload." if risk_level == "high" else None,
            generated_by="local",
        )

    @staticmethod
    def extract_request_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Return normalized request messages from a gateway payload."""
        return RuntimeSessionExplorerService._extract_request_messages(payload)

    @staticmethod
    def _extract_request_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        request = payload.get("request")
        raw_messages = (
            request.get("messages")
            if isinstance(request, dict) and isinstance(request.get("messages"), list)
            else []
        )
        messages: list[dict[str, str]] = []
        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            text = RuntimeSessionExplorerService._message_content_to_text(
                message.get("content")
            )
            if text:
                messages.append(
                    {
                        "role": str(message.get("role") or "message"),
                        "text": text,
                    }
                )
        if messages:
            return messages

        preview = payload.get("conversation_preview")
        preview_messages = (
            preview.get("messages")
            if isinstance(preview, dict) and isinstance(preview.get("messages"), list)
            else []
        )
        for message in preview_messages:
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            if text:
                messages.append(
                    {
                        "role": str(
                            message.get("role") or message.get("source") or "message"
                        ),
                        "text": text,
                    }
                )
        return messages

    @staticmethod
    def _message_content_to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
            return "\n".join(parts).strip()
        return str(value).strip()

    @staticmethod
    def _to_litellm_model(model: AIModel) -> str:
        return to_litellm_model(model)

    def _call_interaction_summary_model(
        self,
        model: AIModel,
        creds_kwargs: dict[str, Any],
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        messages = self._extract_request_messages(payload)
        compact_messages = [
            {
                "role": message["role"],
                "text": message["text"][:2500],
            }
            for message in messages[-8:]
        ]
        context = {
            "model": payload.get("model_alias") or payload.get("requested_model"),
            "endpoint": payload.get("endpoint_kind") or payload.get("endpoint"),
            "outcome": payload.get("outcome"),
            "status_code": payload.get("status_code"),
            "finish_reason": payload.get("finish_reason"),
            "tokens": {
                "prompt": payload.get("prompt_tokens"),
                "completion": payload.get("completion_tokens"),
                "total": payload.get("total_tokens"),
            },
            "messages": compact_messages,
        }
        call_site_kwargs: dict[str, Any] = {
            "model": self._to_litellm_model(model),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Summarize one captured agent model interaction for a "
                        "developer inspecting a replay. Return only compact JSON "
                        "with keys: title, summary, key_points, risk_level, "
                        "next_action. Focus on the user's intent, model/tool "
                        "behavior, notable outputs, failures, and optimization "
                        "signals. Do not quote huge prompts or personality files."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
            ],
            "temperature": 0.1,
            # Budget for a reasoning model's hidden reasoning pass plus the JSON
            # summary; a tight cap is fully consumed by reasoning and returns
            # empty content (no summary) on reasoning-model defaults.
            "max_tokens": 2048,
        }
        kwargs = build_aux_kwargs(
            model, creds_kwargs, call_site_kwargs=call_site_kwargs
        )

        # Cancellation drains a worker that still owns the caller's Session.
        # Bound provider I/O as well as any coroutine deadline.
        kwargs["timeout"] = INTERACTION_SUMMARY_ATTEMPT_TIMEOUT_SECONDS
        kwargs["num_retries"] = 0

        response = litellm.completion(**kwargs)
        check_reasoning_model_empty_content(response)
        raw = response.choices[0].message.content or "{}"
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Interaction summary model returned non-object JSON")
        return parsed

    @staticmethod
    def _normalize_period(
        start_date: Optional[datetime], end_date: Optional[datetime]
    ) -> tuple[datetime, datetime]:
        if end_date is None:
            end_date = datetime.now(timezone.utc)
        elif end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        if start_date is None:
            start_date = end_date - timedelta(days=30)
        elif start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)

        return start_date, end_date

    @staticmethod
    def _summary_row_to_schema(row: dict) -> RuntimeSessionSummary:
        return RuntimeSessionSummary(
            id=row["id"],
            session_source_type=row["session_source_type"],
            session_source_id=row["session_source_id"],
            session_reference=row["session_reference"],
            runtime_principal_type=row["runtime_principal_type"],
            runtime_principal_id=row["runtime_principal_id"],
            runtime_principal_name=row["runtime_principal_name"],
            title=row.get("title"),
            summary=row.get("summary"),
            summary_updated_at=row.get("summary_updated_at"),
            started_at=row["started_at"],
            last_activity_at=row["last_activity_at"],
            ended_at=row["ended_at"],
            flow_id=row["flow_id"],
            flow_name=row["flow_name"],
            flow_execution_id=row["flow_execution_id"],
            latest_model_alias=row["latest_model_alias"],
            latest_provider_name=row["latest_provider_name"],
            is_active_now=row.get("is_active_now", False),
            activity_status=row.get("activity_status", "idle"),
            total_requests=row["total_requests"],
            successful_requests=row["successful_requests"],
            failed_requests=row["failed_requests"],
            token_usage=GatewayTokenUsage.from_row(row),
            estimated_cost=row["estimated_cost"],
            last_request_at=row["last_request_at"],
        )

    def _build_activity_timeline(
        self,
        *,
        summary_row: dict,
        interactions,
    ) -> list[RuntimeSessionActivityItem]:
        items: list[RuntimeSessionActivityItem] = []
        interaction_api_usage_ids = {
            str(interaction.api_usage_id)
            for interaction in interactions
            if getattr(interaction, "api_usage_id", None)
        }

        started_at = summary_row.get("started_at")
        if started_at is not None:
            items.append(
                RuntimeSessionActivityItem(
                    activity_type="session_started",
                    timestamp=self._normalize_timestamp(started_at),
                    title="Session started",
                    summary=summary_row.get("session_reference")
                    or summary_row.get("runtime_principal_name")
                    or summary_row.get("session_source_id"),
                    status="info",
                )
            )

        for interaction in interactions:
            meta_data = interaction.meta_data or {}
            gateway_attempt = self._parse_optional_int(meta_data.get("gateway_attempt"))
            items.append(
                RuntimeSessionActivityItem(
                    activity_type="model_interaction",
                    timestamp=self._normalize_timestamp(interaction.timestamp),
                    title=interaction.model_alias or "Model interaction",
                    summary=f"{interaction.method} {interaction.endpoint}",
                    status=interaction.outcome,
                    api_usage_id=interaction.api_usage_id,
                    auth_subject_type=interaction.auth_subject_type,
                    api_key_id=interaction.api_key_id,
                    api_key_name=interaction.api_key_name,
                    estimated_cost=interaction.estimated_cost,
                    total_tokens=interaction.token_usage.total_tokens,
                    request_fingerprint=public_request_fingerprint(
                        meta_data.get("request_fingerprint")
                    ),
                    gateway_attempt=gateway_attempt,
                    is_retry=self._parse_bool(meta_data.get("is_retry")),
                    retry_of_api_usage_id=meta_data.get("retry_of_api_usage_id"),
                )
            )

        activity_rows = crud_runtime_session_activity.list_for_runtime_session(
            self.db,
            account_id=summary_row["account_id"],
            runtime_session_id=summary_row["id"],
            limit=100,
        )
        if activity_rows:
            items.extend(
                RuntimeSessionActivityItem(
                    activity_type=activity.activity_type,
                    timestamp=self._normalize_timestamp(activity.timestamp),
                    title=activity.tool_name or _default_activity_title(activity),
                    summary=activity.summary or activity.server_name,
                    status=activity.status,
                    tool_name=activity.tool_name,
                    server_name=activity.server_name,
                    api_key_id=str(activity.api_key_id)
                    if activity.api_key_id
                    else None,
                    metadata=activity.metadata_ or {},
                    api_usage_id=(activity.metadata_ or {}).get("api_usage_id"),
                    estimated_cost=self._parse_optional_float(
                        (activity.metadata_ or {}).get("estimated_cost")
                    ),
                    total_tokens=self._parse_optional_int(
                        (activity.metadata_ or {}).get("total_tokens")
                    ),
                    request_fingerprint=public_request_fingerprint(
                        (activity.metadata_ or {}).get("request_fingerprint")
                    ),
                    gateway_attempt=self._parse_optional_int(
                        (activity.metadata_ or {}).get("gateway_attempt")
                    ),
                    is_retry=self._parse_bool(
                        (activity.metadata_ or {}).get("is_retry")
                    ),
                    retry_of_api_usage_id=(activity.metadata_ or {}).get(
                        "retry_of_api_usage_id"
                    ),
                )
                for activity in activity_rows
                if not (
                    activity.activity_type == "model_gateway_call"
                    and str((activity.metadata_ or {}).get("api_usage_id"))
                    in interaction_api_usage_ids
                )
            )

        flow_execution_id = summary_row.get("flow_execution_id")
        if flow_execution_id and not activity_rows:
            from preloop.models.crud.flow_execution import CRUDFlowExecution

            crud_flow_execution = CRUDFlowExecution()
            execution = crud_flow_execution.get(
                self.db,
                id=UUID(flow_execution_id),
                account_id=str(summary_row["account_id"]),
            )
            if execution and isinstance(execution.mcp_usage_logs, list):
                for log in execution.mcp_usage_logs:
                    timestamp = self._parse_timestamp(log.get("timestamp"))
                    if timestamp is None:
                        continue
                    items.append(
                        RuntimeSessionActivityItem(
                            activity_type="tool_call",
                            timestamp=self._normalize_timestamp(timestamp),
                            title=log.get("tool_name") or "Tool call",
                            summary=log.get("result_summary")
                            or log.get("error")
                            or log.get("server_name"),
                            status=log.get("status"),
                            tool_name=log.get("tool_name"),
                            server_name=log.get("server_name"),
                        )
                    )

        ended_at = summary_row.get("ended_at")
        if ended_at is not None:
            items.append(
                RuntimeSessionActivityItem(
                    activity_type="session_ended",
                    timestamp=self._normalize_timestamp(ended_at),
                    title="Session ended",
                    summary=summary_row.get("session_reference")
                    or summary_row.get("runtime_principal_name")
                    or summary_row.get("session_source_id"),
                    status="completed",
                )
            )

        return sorted(items, key=lambda item: item.timestamp, reverse=True)

    @staticmethod
    def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _normalize_timestamp(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_optional_int(value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_optional_float(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
        return bool(value)
