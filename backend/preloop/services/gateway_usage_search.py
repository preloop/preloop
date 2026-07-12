"""Helpers for building an opt-in gateway interaction search corpus."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_gateway_usage_search_document
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.gateway_usage_search_document import (
    GatewayUsageSearchDocument,
)
from preloop.utils.request_fingerprint import public_request_fingerprint


class GatewayUsageSearchService:
    """Build and persist normalized search documents for gateway interactions."""

    MAX_VALUE_CHARS = 2000
    MAX_LINE_COUNT = 256
    MAX_TEXT_CHARS = 16000
    REDACTED_VALUE = "[redacted]"
    _CONTENT_FIELD_NAMES = {
        "content",
        "input",
        "instructions",
        "output_text",
        "system",
        "text",
    }

    def __init__(self, db: Optional[Session] = None) -> None:
        self.db = db

    def build_searchable_text(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> str:
        """Build a normalized plain-text document for one gateway interaction."""
        meta_data = usage.meta_data or {}
        lines: list[str] = [
            "kind: gateway_interaction",
            f"endpoint: {usage.endpoint}",
            f"method: {usage.method}",
            f"status_code: {usage.status_code}",
            f"outcome: {self._derive_outcome(usage.status_code)}",
        ]

        for key, value in (
            ("provider_name", usage.provider_name),
            ("model_alias", usage.model_alias),
            ("requested_model", meta_data.get("requested_model")),
            ("gateway_provider", meta_data.get("gateway_provider")),
            ("endpoint_kind", meta_data.get("endpoint_kind")),
            ("finish_reason", meta_data.get("finish_reason")),
            ("runtime_principal_type", usage.runtime_principal_type),
            ("runtime_principal_name", usage.runtime_principal_name),
            ("error_detail", meta_data.get("error_detail")),
        ):
            self._append_scalar(lines, key, value)

        request_count_before = len(lines)
        self._append_payload(lines, "request", request_payload)
        request_line_count = len(lines) - request_count_before

        response_count_before = len(lines)
        self._append_payload(lines, "response", response_payload)
        response_line_count = len(lines) - response_count_before

        lines.append(f"request_line_count: {request_line_count}")
        lines.append(f"response_line_count: {response_line_count}")
        return self._truncate_document("\n".join(lines))

    def build_document_metadata(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build compact metadata describing the corpus source."""
        usage_meta = usage.meta_data or {}
        return {
            "source": "gateway_interaction",
            "endpoint": usage.endpoint,
            "method": usage.method,
            "status_code": usage.status_code,
            "provider_name": usage.provider_name,
            "model_alias": usage.model_alias,
            "request_fingerprint": public_request_fingerprint(
                usage_meta.get("request_fingerprint")
            ),
            "gateway_attempt": usage_meta.get("gateway_attempt"),
            "is_retry": usage_meta.get("is_retry"),
            "retry_of_api_usage_id": usage_meta.get("retry_of_api_usage_id"),
            "error_detail": usage_meta.get("error_detail"),
            "upstream_request_id": usage.upstream_request_id,
            "request_payload_present": request_payload is not None,
            "response_payload_present": response_payload is not None,
        }

    def index_interaction(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> GatewayUsageSearchDocument:
        """Create or update the corpus row for a gateway interaction."""
        if self.db is None:
            raise ValueError("GatewayUsageSearchService requires a database session")

        searchable_text = self.build_searchable_text(
            usage=usage,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        meta_data = self.build_document_metadata(
            usage=usage,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        return crud_gateway_usage_search_document.upsert_for_api_usage(
            self.db,
            api_usage=usage,
            searchable_text=searchable_text,
            meta_data=meta_data,
        )

    def auto_index_interaction(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> Optional[GatewayUsageSearchDocument]:
        """Index one gateway interaction when the explicit policy allows it."""
        if not settings.model_gateway_auto_index_interactions:
            return None
        if (
            usage.status_code >= 400
            and not settings.model_gateway_auto_index_failed_interactions
        ):
            return None

        return self.index_interaction(
            usage=usage,
            request_payload=self._prepare_payload_for_indexing(request_payload),
            response_payload=self._prepare_payload_for_indexing(response_payload),
        )

    @classmethod
    def _prepare_payload_for_indexing(
        cls, payload: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        if not settings.model_gateway_capture_content:
            return None
        sanitized_payload = cls._sanitize_payload_for_indexing(payload)
        if not isinstance(sanitized_payload, dict) or not sanitized_payload:
            return None
        return sanitized_payload

    @classmethod
    def _sanitize_payload_for_indexing(
        cls, value: Any, *, inside_content_field: bool = False
    ) -> Any:
        if value is None:
            return None

        if isinstance(value, dict):
            if value.get("redacted") is True:
                return None

            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if cls._is_secret_key(key):
                    sanitized[key] = cls.REDACTED_VALUE
                    continue

                sanitized_item = cls._sanitize_payload_for_indexing(
                    item,
                    inside_content_field=inside_content_field
                    or key.lower() in cls._CONTENT_FIELD_NAMES,
                )
                if sanitized_item is not None:
                    sanitized[key] = sanitized_item
            return sanitized or None

        if isinstance(value, list):
            sanitized_items = [
                sanitized_item
                for item in value
                if (
                    sanitized_item := cls._sanitize_payload_for_indexing(
                        item, inside_content_field=inside_content_field
                    )
                )
                is not None
            ]
            return sanitized_items or None

        if inside_content_field and not settings.model_gateway_capture_content:
            return None

        normalized = cls._normalize_scalar(value)
        if not normalized:
            return None
        return normalized

    @classmethod
    def _append_payload(
        cls, lines: list[str], prefix: str, payload: Optional[dict[str, Any]]
    ) -> None:
        if payload is None:
            return
        cls._append_value(lines, prefix, payload)

    @classmethod
    def _append_value(cls, lines: list[str], prefix: str, value: Any) -> None:
        if len(lines) >= cls.MAX_LINE_COUNT or value is None:
            return

        if isinstance(value, dict):
            if value.get("redacted") is True and "length" in value:
                lines.append(f"{prefix}: [redacted length={value['length']}]")
                return

            for key in sorted(value):
                next_prefix = f"{prefix}.{key}"
                if cls._is_secret_key(key):
                    lines.append(f"{next_prefix}: {cls.REDACTED_VALUE}")
                    if len(lines) >= cls.MAX_LINE_COUNT:
                        return
                    continue
                cls._append_value(lines, next_prefix, value[key])
                if len(lines) >= cls.MAX_LINE_COUNT:
                    return
            return

        if isinstance(value, list):
            for index, item in enumerate(value):
                cls._append_value(lines, f"{prefix}.{index}", item)
                if len(lines) >= cls.MAX_LINE_COUNT:
                    return
            return

        cls._append_scalar(lines, prefix, value)

    @classmethod
    def _append_scalar(cls, lines: list[str], key: str, value: Any) -> None:
        if len(lines) >= cls.MAX_LINE_COUNT or value is None:
            return

        normalized = cls._normalize_scalar(value)
        if not normalized:
            return
        lines.append(f"{key}: {normalized}")

    @classmethod
    def _normalize_scalar(cls, value: Any) -> str:
        text = " ".join(str(value).split())
        if not text:
            return ""
        if len(text) > cls.MAX_VALUE_CHARS:
            return text[: cls.MAX_VALUE_CHARS] + "... [truncated]"
        return text

    @classmethod
    def _truncate_document(cls, value: str) -> str:
        if len(value) <= cls.MAX_TEXT_CHARS:
            return value
        return value[: cls.MAX_TEXT_CHARS] + "\ntruncated: true"

    @staticmethod
    def _derive_outcome(status_code: int) -> str:
        if status_code >= 400:
            return "error"
        return "success"

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        lowered = key.lower()
        return any(token in lowered for token in ("api_key", "authorization", "token"))
