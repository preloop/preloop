"""Emission helpers for normalized model gateway runtime events."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.models.api_key import ApiKey
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session_activity import RuntimeSessionActivity
from preloop.models.crud.runtime_session_activity import CRUDRuntimeSessionActivity
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_BUDGET_HEALTH,
    ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
    build_account_event,
    encode_realtime_event_for_nats,
    emit_account_event,
)
from preloop.services.cache_accounting import reported_cache_miss_tokens
from preloop.services.gateway_tool_activity import normalize_tool_activity
from preloop.services.model_allowlist import is_model_not_allowed_detail
from preloop.sync.services.event_bus import get_nats_client
from preloop.utils.jsonb_sanitize import sanitize_for_jsonb
from preloop.utils.request_fingerprint import public_request_fingerprint

logger = logging.getLogger(__name__)
_REDACTED_TEXT = "***REDACTED***"
_SENSITIVE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        _REDACTED_TEXT,
    ),
    (
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
        rf"\1{_REDACTED_TEXT}",
    ),
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}\b"),
        rf"\1 {_REDACTED_TEXT}",
    ),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;]+)"
        ),
        rf"\1{_REDACTED_TEXT}",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
        _REDACTED_TEXT,
    ),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}\b"),
        _REDACTED_TEXT,
    ),
)

crud_flow_execution = CRUDFlowExecution()
crud_runtime_session_activity = CRUDRuntimeSessionActivity(RuntimeSessionActivity)
_TEXT_CONTENT_TYPES = {"input_text", "output_text", "text"}


class ModelGatewayEventEmitter:
    """Emit one normalized runtime event per completed gateway request."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def emit_for_usage(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict],
        response_payload: Optional[dict],
    ) -> None:
        """Persist and publish a normalized model-call event when possible."""
        event = self._build_event(
            usage=usage,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        execution_id = str(usage.flow_execution_id) if usage.flow_execution_id else None
        if execution_id:
            crud_flow_execution.append_log(
                self.db,
                execution_id,
                event,
                commit=True,
            )

        runtime_session_id = (
            str(usage.runtime_session_id) if usage.runtime_session_id else None
        )
        if (
            not runtime_session_id
            and not execution_id
            and usage.runtime_principal_type
            and usage.runtime_principal_id
        ):
            latest_session = crud_runtime_session.get_latest_by_principal(
                self.db,
                account_id=str(usage.account_id),
                principal_type=usage.runtime_principal_type,
                principal_id=usage.runtime_principal_id,
            )
            if latest_session:
                runtime_session_id = str(latest_session.id)

        if runtime_session_id and not execution_id:
            crud_runtime_session_activity.log_model_gateway_call(
                self.db,
                account_id=str(usage.account_id),
                runtime_session_id=runtime_session_id,
                status=event["payload"].get("outcome", "success"),
                summary=None,
                flow_execution_id=execution_id,
                api_key_id=str(usage.api_key_id) if usage.api_key_id else None,
                metadata=event["payload"],
                timestamp=usage.timestamp,
                commit=True,
            )

        if execution_id and usage.account_id:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._publish_to_nats(event))
            else:
                from preloop.tools.utils import run_async

                try:
                    run_async(self._publish_to_nats(event))
                except Exception:  # noqa: BLE001 - sync NATS publish is best-effort
                    logger.debug(
                        "Failed to publish gateway activity event to NATS "
                        "(sync fallback path)",
                        exc_info=True,
                    )

        if usage.account_id:
            emit_account_event(
                build_account_event(
                    account_id=str(usage.account_id),
                    topic=ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
                    event_type=event["type"],
                    payload=event["payload"],
                    runtime_session_id=event.get("runtime_session_id"),
                    execution_id=event.get("execution_id"),
                    flow_id=event.get("flow_id"),
                )
            )

            budget_payload = (event.get("payload") or {}).get("budget") or {}
            if budget_payload:
                emit_account_event(
                    build_account_event(
                        account_id=str(usage.account_id),
                        topic=ACCOUNT_TOPIC_BUDGET_HEALTH,
                        event_type="budget_health_updated",
                        payload={
                            "api_usage_id": str(usage.id),
                            "ai_model_id": str(usage.ai_model_id)
                            if usage.ai_model_id
                            else None,
                            "model_alias": usage.model_alias,
                            "provider_name": usage.provider_name,
                            "estimated_cost": usage.estimated_cost,
                            "status_code": usage.status_code,
                            "budget": budget_payload,
                        },
                        runtime_session_id=event.get("runtime_session_id"),
                        execution_id=event.get("execution_id"),
                        flow_id=event.get("flow_id"),
                    )
                )
                self._emit_budget_webhooks(usage, budget_payload)

    def _emit_budget_webhooks(self, usage: ApiUsage, budget: dict) -> None:
        """Queue budget.threshold / budget.exceeded from the gateway snapshot.

        The snapshot is recomputed on every model call, so the raw flags fire
        constantly once a limit is near. Deduplication is left to the outbox:
        the natural key names the account, scope, limit and billing month, so
        a month of calls over the same limit collapses to one delivery.

        Never raises: billing telemetry must not fail a model call.
        """
        try:
            from preloop.services.event_webhooks.emitters import emit_budget_event

            hard = bool(budget.get("hard_limit_exceeded"))
            soft = bool(budget.get("soft_limit_exceeded"))
            if not hard and not soft:
                return

            reason = budget.get("enforcement_reason") or ""
            scope = "flow" if reason.startswith("flow_") else "account"
            prefix = "flow" if scope == "flow" else "account"
            limit = budget.get(
                f"{prefix}_limit_usd" if hard else f"{prefix}_soft_limit_usd"
            )
            if limit is None:
                return
            spend = budget.get(f"{prefix}_current_spend_usd")
            hard_limit = budget.get(f"{prefix}_limit_usd")
            threshold_percent = None
            if not hard and hard_limit:
                threshold_percent = int(round(float(limit) / float(hard_limit) * 100))

            result = emit_budget_event(
                self.db,
                account_id=usage.account_id,
                exceeded=hard,
                scope=scope,
                scope_id=usage.flow_execution_id if scope == "flow" else None,
                period=datetime.now(timezone.utc).strftime("%Y-%m"),
                limit_amount=hard_limit if hard else limit,
                spent_amount=spend,
                threshold_percent=threshold_percent,
            )
            # Gateway callers can roll this session back (or skip the
            # activity-touch commit when there is no runtime session). The
            # outbox row must not ride that rollback.
            if result.delivery_ids:
                self.db.commit()
        except Exception:  # noqa: BLE001 - a webhook must not fail a model call
            logger.debug("Failed to queue budget webhook event", exc_info=True)

    async def _publish_to_nats(self, event: dict) -> None:
        execution_id = event.get("execution_id")
        if not execution_id:
            return
        nats_client = await get_nats_client()
        if not nats_client or not nats_client.is_connected:
            return

        payload_bytes = encode_realtime_event_for_nats(
            event,
            context=f"execution {execution_id}",
        )
        if payload_bytes is None:
            return

        await nats_client.publish(
            f"flow-updates.{execution_id}",
            payload_bytes,
        )

    def _build_event(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict],
        response_payload: Optional[dict],
    ) -> dict[str, Any]:
        meta_data = usage.meta_data or {}
        error_detail = meta_data.get("error_detail")
        conversation_preview = self._build_conversation_preview(
            request_payload=request_payload,
            response_payload=response_payload,
        )
        api_key = self.db.get(ApiKey, usage.api_key_id) if usage.api_key_id else None
        managed_agent_id = self._resolve_managed_agent_id(usage=usage, api_key=api_key)
        # Sanitize once, then derive structure from the sanitized bodies: tool
        # arguments and results are content, so this module must never be the
        # path that recovers what the capture policy withheld upstream of it.
        sanitized_request = self._cap_activity_body(
            self._sanitize_payload(request_payload)
        )
        sanitized_response = self._cap_activity_body(
            self._sanitize_payload(response_payload)
        )
        return {
            "topic": "flow_executions",
            "execution_id": str(usage.flow_execution_id)
            if usage.flow_execution_id
            else None,
            "runtime_session_id": str(usage.runtime_session_id)
            if usage.runtime_session_id
            else None,
            "flow_id": str(usage.flow_id) if usage.flow_id else None,
            "account_id": str(usage.account_id) if usage.account_id else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "model_gateway_call",
            "payload": {
                "api_usage_id": str(usage.id),
                "request_id": meta_data.get("request_id"),
                "tools": self._extract_structured_tools(
                    request_payload, response_payload
                ),
                "tools_metadata_truncated": self._tool_metadata_truncated,
                "endpoint": usage.endpoint,
                "endpoint_kind": meta_data.get("endpoint_kind"),
                "method": usage.method,
                "status_code": usage.status_code,
                "outcome": self._derive_outcome(usage.status_code, error_detail),
                "duration_ms": int((usage.duration or 0) * 1000),
                "user_id": str(usage.user_id) if usage.user_id else None,
                "auth_subject_type": usage.auth_subject_type,
                "api_key_id": str(usage.api_key_id) if usage.api_key_id else None,
                "api_key_name": api_key.name if api_key is not None else None,
                "ai_model_id": str(usage.ai_model_id) if usage.ai_model_id else None,
                "model_alias": usage.model_alias,
                "provider_name": usage.provider_name,
                "gateway_provider": meta_data.get("gateway_provider"),
                "requested_model": meta_data.get("requested_model"),
                "upstream_request_id": usage.upstream_request_id,
                "request_fingerprint": public_request_fingerprint(
                    meta_data.get("request_fingerprint")
                ),
                "gateway_attempt": meta_data.get("gateway_attempt"),
                "is_retry": meta_data.get("is_retry"),
                "retry_of_api_usage_id": meta_data.get("retry_of_api_usage_id"),
                # Shared with the `model_gateway_request_started` event this
                # call announced. Present on rows recorded after the id was
                # threaded through the gateway; absent on older rows, which is
                # what makes "never fabricate completion" possible: a start
                # without a matching id here stays unmatched rather than
                # closing some other request that happened to finish next.
                "gateway_request_id": meta_data.get("gateway_request_id"),
                # Retries the gateway itself made against the provider inside
                # this single request (mid-stream disconnect, 5xx, 429).
                # 0 = the call succeeded first time. Lets the console show
                # "recovered after N retries" instead of only a longer
                # duration, and makes provider flakiness countable.
                "retried": int(meta_data.get("upstream_retries") or 0),
                "finish_reason": meta_data.get("finish_reason"),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost": usage.estimated_cost,
                # Prefer authoritative ApiUsage cache columns; fall back to the
                # raw usage_details snapshot for older rows or nested OpenAI
                # shapes still useful in prompt_tokens_details.
                "prompt_tokens_details": (meta_data.get("usage_details") or {}).get(
                    "prompt_tokens_details"
                ),
                "cache_read_input_tokens": (
                    usage.cache_read_tokens
                    if usage.cache_read_tokens is not None
                    else (meta_data.get("usage_details") or {}).get(
                        "cache_read_input_tokens"
                    )
                ),
                "cache_creation_input_tokens": (
                    usage.cache_creation_tokens
                    if usage.cache_creation_tokens is not None
                    else (meta_data.get("usage_details") or {}).get(
                        "cache_creation_input_tokens"
                    )
                ),
                # A cache-MISS count the provider reported outright (today only
                # DeepSeek's prompt_cache_miss_tokens, which litellm drops on the
                # floor). None means "no provider miss count" — consumers must
                # not read that as zero misses.
                "cache_miss_input_tokens_reported": reported_cache_miss_tokens(
                    meta_data
                ),
                "runtime_session_id": str(usage.runtime_session_id)
                if usage.runtime_session_id
                else None,
                "runtime_principal_type": usage.runtime_principal_type,
                "runtime_principal_id": usage.runtime_principal_id,
                "runtime_principal_name": usage.runtime_principal_name,
                "managed_agent_id": managed_agent_id,
                "runtime_principal": {
                    "type": usage.runtime_principal_type,
                    "id": usage.runtime_principal_id,
                    "name": usage.runtime_principal_name,
                },
                "budget": meta_data.get("budget"),
                "error_detail": error_detail,
                "capture_policy": self._build_capture_policy(conversation_preview),
                "conversation_preview": conversation_preview,
                # Named tool calls and results for this exchange, recovered from
                # the structured wire fields rather than left inside raw text.
                # A session whose gateway history is the only record of what an
                # agent did can still render `terminal · completed · 1.2s`.
                # None when neither body carried tool structure.
                "tool_activity": normalize_tool_activity(
                    request_payload=sanitized_request,
                    response_payload=sanitized_response,
                    capture_content=settings.model_gateway_capture_content,
                    redact_text=self._redact_for_tool_activity,
                ),
                # These two bodies are the ones that carried 533KB of binary
                # content in the 2026-08-05 incident. Cap them here, at the
                # point they enter the activity payload, so the JSONB row stays
                # a bounded size regardless of what the upstream returned.
                "request": sanitized_request,
                "response": sanitized_response,
            },
        }

    @staticmethod
    def _redact_for_tool_activity(value: str) -> str:
        """Secret redaction for tool arguments and results.

        ``_sanitize_payload`` only rewrites the message-content keys, so a
        bearer token nested inside a tool argument would otherwise reach the
        activity row untouched. Reusing the emitter's patterns here keeps one
        definition of "sensitive" for the whole event.
        """
        redacted, _ = ModelGatewayEventEmitter._redact_sensitive_text(value)
        return redacted

    def _resolve_managed_agent_id(
        self, *, usage: ApiUsage, api_key: Optional[ApiKey]
    ) -> Optional[str]:
        context_data = (
            api_key.context_data
            if api_key and isinstance(api_key.context_data, dict)
            else {}
        )
        managed_agent_id = (
            context_data.get("managed_agent_id") if context_data else None
        )
        if managed_agent_id:
            return str(managed_agent_id)
        if not usage.runtime_principal_type or not usage.runtime_principal_id:
            return None
        from preloop.models.crud.managed_agent import crud_managed_agent

        managed_agent = crud_managed_agent.get_by_source(
            self.db,
            account_id=str(usage.account_id),
            session_source_type=usage.runtime_principal_type,
            session_source_id=usage.runtime_principal_id,
        )
        return str(managed_agent.id) if managed_agent is not None else None

    @staticmethod
    def _derive_outcome(status_code: int, error_detail: Optional[str]) -> str:
        # Allowlist denials reuse budget_denied: it is the only denial outcome
        # the transcript, replay, and audit surfaces know how to render.
        if (
            status_code == 403
            and error_detail
            and (
                "budget exceeded" in error_detail.lower()
                or is_model_not_allowed_detail(error_detail)
            )
        ):
            return "budget_denied"
        if status_code >= 400:
            return "error"
        return "success"

    @staticmethod
    def _cap_activity_body(value: Any) -> Any:
        """Bound one request/response body before it is stored as JSONB.

        Separate from the preview cap on purpose. Previews feed the transcript
        UI and are tuned for readability; these bodies are archival and are
        never rendered in full, so they get the tighter activity cap.
        """
        return sanitize_for_jsonb(
            value,
            max_string_chars=settings.model_gateway_activity_max_body_chars,
        )

    def _sanitize_payload(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                lowered = key.lower()
                if any(word in lowered for word in ("api_key", "authorization")) or (
                    "token" in lowered and "tokens" not in lowered
                ):
                    sanitized[key] = "***REDACTED***"
                    continue
                if lowered in {
                    "content",
                    "input",
                    "instructions",
                    "output_text",
                    "text",
                    "system",
                }:
                    if isinstance(item, list):
                        sanitized[key] = [self._sanitize_payload(i) for i in item]
                        continue
                    if not settings.model_gateway_capture_content:
                        sanitized[key] = "***REDACTED***"
                        continue
                    # Retain full payload raw length, only do regex sensitive redaction
                    redacted_text, _ = self._redact_sensitive_text(str(item))
                    sanitized[key] = redacted_text
                    continue
                sanitized[key] = self._sanitize_payload(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_payload(item) for item in value]
        if isinstance(value, str):
            return self._truncate_text(value)
        return value

    def _sanitize_text(self, value: Any) -> Any:
        # Note: This is now only used in fallback scenarios.
        if isinstance(value, list):
            return [self._sanitize_payload(item) for item in value]
        text = str(value)
        sanitized_text, _ = self._sanitize_text_with_meta(text)
        return sanitized_text

    def _build_capture_policy(
        self, conversation_preview: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = conversation_preview.get("metadata", {})
        return {
            "content_capture_enabled": settings.model_gateway_capture_content,
            "max_preview_chars": settings.model_gateway_max_preview_chars,
            "sensitive_fields_redacted": True,
            "content_redacted": bool(metadata.get("has_redacted_content")),
            "content_truncated": bool(metadata.get("has_truncated_content")),
            "conversation_preview_available": bool(
                conversation_preview.get("messages")
            ),
        }

    def _extract_structured_tools(
        self, request: Optional[dict], response: Optional[dict]
    ) -> list[dict[str, Any]]:
        """Retain bounded tool identities and policy-sanitized captured content.

        Calls are requests, not evidence that an executor started or succeeded.
        Stable provider call ids link accumulated history and parallel results.
        """
        tools: list[dict[str, Any]] = []
        remaining_bytes = 64 * 1024
        self._tool_metadata_truncated = False

        def add(kind: str, item: dict, source: str) -> None:
            nonlocal remaining_bytes
            if len(tools) >= 256 or remaining_bytes < 1024:
                self._tool_metadata_truncated = True
                return
            call_id = (
                item.get("call_id")
                or item.get("tool_call_id")
                or item.get("tool_use_id")
                or item.get("id")
            )
            function = item.get("function")
            if not isinstance(function, dict):
                function = item
            name = function.get("name") if isinstance(function, dict) else None
            value = (
                function.get("arguments", item.get("input"))
                if kind == "call"
                else item.get("output", item.get("content"))
            )
            import json

            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
            original_value = value
            capped_value = self._cap_activity_body(value)
            value = self._sanitize_payload(capped_value)
            key_redacted = value != original_value and "***REDACTED***" in json.dumps(
                value, default=str
            )
            payload_truncated = capped_value != original_value or (
                value != capped_value and not key_redacted
            )
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, default=str)
            text, meta = self._sanitize_text_with_meta(value)
            entry = {
                "kind": kind,
                "call_id": call_id
                if isinstance(call_id, str) and 0 < len(call_id) <= 256
                else None,
                "name": str(name)[:256] if name else None,
                "source": source,
                "text": text,
                "redacted": meta["redacted"] or key_redacted,
                "truncated": meta["truncated"] or payload_truncated,
                "is_error": item.get("is_error") is True,
            }
            if isinstance(text, str):
                encoded = text.encode("utf-8")
                limit = min(8192, remaining_bytes - 1024)
                if len(encoded) > limit:
                    entry["text"] = encoded[:limit].decode("utf-8", errors="ignore")
                    entry["truncated"] = True
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
            if size <= remaining_bytes:
                tools.append(entry)
                remaining_bytes -= size

        def scan(items: Any, source: str, depth: int = 0) -> None:
            if depth > 12:
                self._tool_metadata_truncated = True
                return
            if not isinstance(items, list):
                return
            for item in items[-256:]:
                if len(tools) >= 256 or remaining_bytes < 1024:
                    self._tool_metadata_truncated = True
                    break
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type in ("function_call", "tool_use"):
                    add("call", item, source)
                elif (
                    item_type in ("function_call_output", "tool_result")
                    or item.get("role") == "tool"
                ):
                    add("result", item, source)
                raw_calls = item.get("tool_calls")
                for call in (raw_calls if isinstance(raw_calls, list) else [])[:128]:
                    if isinstance(call, dict):
                        add("call", call, source)
                scan(item.get("content"), source, depth + 1)

        if isinstance(response, dict):
            scan(response.get("output"), "response")
            scan(response.get("content"), "response")
            raw_choices = response.get("choices")
            for choice in (raw_choices if isinstance(raw_choices, list) else [])[:128]:
                if isinstance(choice, dict):
                    scan([choice.get("message")], "response")
        if isinstance(request, dict):
            scan(request.get("messages"), "request")
            scan(request.get("input"), "request")
        return tools[:256]

    def _build_conversation_preview(
        self,
        *,
        request_payload: Optional[dict],
        response_payload: Optional[dict],
    ) -> dict[str, Any]:
        request_messages = self._extract_request_preview_messages(request_payload)
        response_messages = self._extract_response_preview_messages(response_payload)
        messages = [*request_messages, *response_messages]
        return {
            "messages": messages,
            "metadata": {
                "message_count": len(messages),
                "request_message_count": len(request_messages),
                "response_message_count": len(response_messages),
                "has_redacted_content": any(
                    bool(message.get("redacted")) for message in messages
                ),
                "has_truncated_content": any(
                    bool(message.get("truncated")) for message in messages
                ),
            },
        }

    def _extract_request_preview_messages(
        self, payload: Optional[dict]
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []

        messages: list[dict[str, Any]] = []
        if payload.get("instructions") is not None:
            preview_message = self._build_preview_message(
                source="request",
                role="system",
                content=payload.get("instructions"),
            )
            if preview_message:
                messages.append(preview_message)

        if payload.get("system") is not None:
            preview_message = self._build_preview_message(
                source="request",
                role="system",
                content=payload.get("system"),
            )
            if preview_message:
                messages.append(preview_message)

        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            preview_message = self._build_preview_message(
                source="request",
                role="user",
                content=raw_input,
            )
            if preview_message:
                messages.append(preview_message)
        elif isinstance(raw_input, list):
            messages.extend(
                self._extract_preview_messages_from_items(raw_input, "request")
            )

        raw_messages = payload.get("messages")
        if isinstance(raw_messages, list):
            messages.extend(
                self._extract_preview_messages_from_items(raw_messages, "request")
            )

        return messages

    def _extract_response_preview_messages(
        self, payload: Optional[dict]
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []

        messages: list[dict[str, Any]] = []
        raw_output = payload.get("output")
        if isinstance(raw_output, list):
            messages.extend(
                self._extract_preview_messages_from_items(raw_output, "response")
            )

        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                preview_message = self._build_preview_message(
                    source="response",
                    role=str(message.get("role") or "assistant"),
                    content=message.get("content"),
                )
                if preview_message:
                    messages.append(preview_message)

        if isinstance(payload.get("content"), list):
            preview_message = self._build_preview_message(
                source="response",
                role=str(payload.get("role") or "assistant"),
                content=payload.get("content"),
            )
            if preview_message:
                messages.append(preview_message)

        if not messages and payload.get("output_text") is not None:
            preview_message = self._build_preview_message(
                source="response",
                role="assistant",
                content=payload.get("output_text"),
            )
            if preview_message:
                messages.append(preview_message)

        return messages

    def _extract_preview_messages_from_items(
        self, items: list[Any], source: str
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            preview_message = self._build_preview_message(
                source=source,
                role=str(
                    item.get("role")
                    or ("assistant" if source == "response" else "user")
                ),
                content=item.get("content", item),
            )
            if preview_message:
                call_ids = []
                direct_id = item.get("tool_call_id") or item.get("call_id")
                if direct_id:
                    if isinstance(direct_id, str) and len(direct_id) <= 256:
                        call_ids.append(direct_id)
                content = item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id")
                        ):
                            identity = block["tool_use_id"]
                            if isinstance(identity, str) and len(identity) <= 256:
                                call_ids.append(identity)
                if call_ids:
                    preview_message["tool_call_ids"] = call_ids[:128]
                messages.append(preview_message)
        return messages

    def _build_preview_message(
        self, *, source: str, role: str, content: Any
    ) -> Optional[dict[str, Any]]:
        text = self._content_to_preview_text(content).strip()
        if not text:
            return None

        sanitized_text, metadata = self._sanitize_text_with_meta(text)
        return {
            "source": source,
            "role": role,
            "text": sanitized_text if isinstance(sanitized_text, str) else None,
            "redacted": metadata["redacted"],
            "truncated": metadata["truncated"],
            "original_length": metadata["length"],
        }

    def _content_to_preview_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments = [
                self._content_to_preview_text(item)
                for item in content
                if self._content_to_preview_text(item)
            ]
            return "\n".join(fragments)
        if isinstance(content, dict):
            content_type = str(content.get("type") or "")
            if content_type in _TEXT_CONTENT_TYPES and content.get("text") is not None:
                return str(content.get("text"))
            if content.get("content") is not None:
                return self._content_to_preview_text(content.get("content"))
            if content.get("text") is not None:
                return str(content.get("text"))
            return ""
        return str(content)

    def _sanitize_text_with_meta(self, text: str) -> tuple[Any, dict[str, Any]]:
        if settings.model_gateway_capture_content:
            redacted_text, redacted = self._redact_sensitive_text(text)
            truncated_text, truncated = self._truncate_text_with_meta(redacted_text)
            return truncated_text, {
                "redacted": redacted,
                "truncated": truncated,
                "length": len(text),
            }
        return {"redacted": True, "length": len(text)}, {
            "redacted": True,
            "truncated": False,
            "length": len(text),
        }

    @staticmethod
    def _truncate_text(value: str) -> str:
        return ModelGatewayEventEmitter._truncate_text_with_meta(value)[0]

    @staticmethod
    def _redact_sensitive_text(value: str) -> tuple[str, bool]:
        redacted = value
        changed = False
        for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
            updated = pattern.sub(replacement, redacted)
            if updated != redacted:
                changed = True
                redacted = updated
        return redacted, changed

    @staticmethod
    def _truncate_text_with_meta(value: str) -> tuple[str, bool]:
        if len(value) <= settings.model_gateway_max_preview_chars:
            return value, False
        return value[
            : settings.model_gateway_max_preview_chars
        ] + "... [truncated]", True
