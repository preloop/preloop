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
                # These two bodies are the ones that carried 533KB of binary
                # content in the 2026-08-05 incident. Cap them here, at the
                # point they enter the activity payload, so the JSONB row stays
                # a bounded size regardless of what the upstream returned.
                "request": self._cap_activity_body(
                    self._sanitize_payload(request_payload)
                ),
                "response": self._cap_activity_body(
                    self._sanitize_payload(response_payload)
                ),
            },
        }

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
        if (
            status_code == 403
            and error_detail
            and "budget exceeded" in error_detail.lower()
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
