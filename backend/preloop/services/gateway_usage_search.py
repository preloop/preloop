"""Helpers for building an opt-in gateway interaction search corpus."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_gateway_usage_search_document
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.gateway_usage_search_document import (
    GatewayUsageSearchDocument,
)
from preloop.utils.request_fingerprint import public_request_fingerprint

# Runs of non-whitespace, scanned lazily so a large value is never split
# into every word it contains.
_WHITESPACE_RUN = re.compile(r"\S+")


class _LineBuffer:
    """Document lines with a hard ceiling on count and total characters.

    The flattening pass used to run to the end of the payload and throw the
    excess away afterwards, so a multi-megabyte response paid for text work
    and allocations it could never use. This stops at the first line past
    the ceiling instead.
    """

    def __init__(self, *, max_lines: int, max_chars: int) -> None:
        self._lines: list[str] = []
        self._chars = 0
        self._max_lines = max_lines
        self._max_chars = max_chars
        self.truncated = False

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def full(self) -> bool:
        """Whether the document has reached either ceiling."""
        return len(self._lines) >= self._max_lines or self._chars >= self._max_chars

    def append(self, line: str, *, force: bool = False) -> None:
        """Add one line, unless the document is already full."""
        if self.full and not force:
            self.truncated = True
            return
        self._lines.append(line)
        self._chars += len(line) + 1

    def render(self) -> str:
        """Join the lines, flagging a document that lost content."""
        text = "\n".join(self._lines)
        if self.truncated:
            return text + "\ntruncated: true"
        return text


@dataclass(frozen=True)
class GatewayUsageIndexDocument:
    """A prepared corpus row: an id plus bounded text and metadata.

    Deliberately holds no payloads. Once one of these exists the request and
    response bodies it was derived from can be released, which is what lets
    the write happen off the response path.
    """

    api_usage_id: str
    searchable_text: str
    meta_data: dict[str, Any]


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
        lines = _LineBuffer(
            max_lines=self.MAX_LINE_COUNT, max_chars=self.MAX_TEXT_CHARS
        )
        for header in (
            "kind: gateway_interaction",
            f"endpoint: {usage.endpoint}",
            f"method: {usage.method}",
            f"status_code: {usage.status_code}",
            f"outcome: {self._derive_outcome(usage.status_code)}",
        ):
            lines.append(header)

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

        lines.append(f"request_line_count: {request_line_count}", force=True)
        lines.append(f"response_line_count: {response_line_count}", force=True)
        return self._truncate_document(lines.render())

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

        searchable_text = self._storage_redacted(
            usage,
            self.build_searchable_text(
                usage=usage,
                request_payload=request_payload,
                response_payload=response_payload,
            ),
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

    @staticmethod
    def _storage_redacted(usage: ApiUsage, text: str) -> str:
        """Apply the account's redact rules to the searchable text (#1123)."""
        from preloop.services.sensitive_data.storage import (
            StorageScope,
            apply_storage_redaction,
        )

        return apply_storage_redaction(
            getattr(usage, "account_id", None),
            text,
            scope=StorageScope(
                managed_agent_id=getattr(usage, "managed_agent_id", None)
            ),
        )

    def build_index_document(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> Optional[GatewayUsageIndexDocument]:
        """Prepare a corpus row for one interaction, or ``None`` when policy says no.

        The payloads are read once and never copied: the output is bounded by
        ``MAX_LINE_COUNT`` lines of at most ``MAX_VALUE_CHARS`` each, capped
        again at ``MAX_TEXT_CHARS``. Callers on the response path can release
        the payloads as soon as this returns.
        """
        if not settings.model_gateway_auto_index_interactions:
            return None
        if (
            usage.status_code >= 400
            and not settings.model_gateway_auto_index_failed_interactions
        ):
            return None

        indexable_request = self._payload_for_indexing(request_payload)
        indexable_response = self._payload_for_indexing(response_payload)
        return GatewayUsageIndexDocument(
            api_usage_id=str(usage.id),
            searchable_text=self._storage_redacted(
                usage,
                self.build_searchable_text(
                    usage=usage,
                    request_payload=indexable_request,
                    response_payload=indexable_response,
                ),
            ),
            meta_data=self.build_document_metadata(
                usage=usage,
                request_payload=indexable_request,
                response_payload=indexable_response,
            ),
        )

    def persist_index_document(
        self, document: GatewayUsageIndexDocument
    ) -> GatewayUsageSearchDocument:
        """Write one prepared corpus row."""
        if self.db is None:
            raise ValueError("GatewayUsageSearchService requires a database session")
        return crud_gateway_usage_search_document.upsert_for_api_usage_id(
            self.db,
            api_usage_id=document.api_usage_id,
            searchable_text=document.searchable_text,
            meta_data=document.meta_data,
        )

    def auto_index_interaction(
        self,
        *,
        usage: ApiUsage,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> Optional[GatewayUsageSearchDocument]:
        """Index one gateway interaction inline when the policy allows it.

        Kept for callers that are already off the response path (imports,
        backfills, tests). The gateway itself queues instead, see
        ``preloop.services.gateway_usage_index_queue``.
        """
        document = self.build_index_document(
            usage=usage,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        if document is None:
            return None
        return self.persist_index_document(document)

    @classmethod
    def payload_for_indexing(
        cls, payload: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Public entry point to the indexing policy for one payload.

        The session search corpus indexes the same gateway payloads and must
        apply the same capture policy and the same redaction, so it calls this
        rather than growing a second sanitiser.
        """
        return cls._payload_for_indexing(payload)

    @classmethod
    def _payload_for_indexing(
        cls, payload: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Decide whether a payload may be indexed, without copying it.

        An earlier version deep-copied both payloads into sanitized dicts
        before flattening them, which doubled the memory a large interaction
        cost at exactly the moment the process was holding the most. Secret
        keys are redacted during flattening instead, so the copy bought
        nothing.
        """
        if not isinstance(payload, dict) or not payload:
            return None
        if not settings.model_gateway_capture_content:
            return None
        return payload

    @classmethod
    def _append_payload(
        cls, lines: _LineBuffer, prefix: str, payload: Optional[dict[str, Any]]
    ) -> None:
        if payload is None:
            return
        cls._append_value(lines, prefix, payload)

    @classmethod
    def _append_value(cls, lines: _LineBuffer, prefix: str, value: Any) -> None:
        if lines.full or value is None:
            return

        if isinstance(value, dict):
            if value.get("redacted") is True:
                # A redacted stand-in carries no content worth indexing; the
                # length, when the redactor recorded one, is the only useful
                # part of it.
                if "length" in value:
                    lines.append(f"{prefix}: [redacted length={value['length']}]")
                return

            for key in sorted(value):
                next_prefix = f"{prefix}.{key}"
                if cls._is_secret_key(key):
                    lines.append(f"{next_prefix}: {cls.REDACTED_VALUE}")
                    if lines.full:
                        return
                    continue
                cls._append_value(lines, next_prefix, value[key])
                if lines.full:
                    return
            return

        if isinstance(value, list):
            for index, item in enumerate(value):
                cls._append_value(lines, f"{prefix}.{index}", item)
                if lines.full:
                    return
            return

        cls._append_scalar(lines, prefix, value)

    @classmethod
    def _append_scalar(cls, lines: _LineBuffer, key: str, value: Any) -> None:
        if lines.full or value is None:
            return

        normalized = cls._normalize_scalar(value)
        if not normalized:
            return
        lines.append(f"{key}: {normalized}")

    @classmethod
    def _normalize_scalar(cls, value: Any) -> str:
        """Collapse whitespace in at most ``MAX_VALUE_CHARS`` of a value.

        Reads lazily rather than splitting the whole value: a megabyte of
        response text used to become a list of every word in it before the
        first ``MAX_VALUE_CHARS`` were kept, which was the single largest
        allocation on the gateway response path.
        """
        text = value if isinstance(value, str) else str(value)
        pieces: list[str] = []
        used = 0
        truncated = False
        for match in _WHITESPACE_RUN.finditer(text):
            token = match.group(0)
            separator = 1 if pieces else 0
            remaining = cls.MAX_VALUE_CHARS - used - separator
            if remaining <= 0:
                truncated = True
                break
            if len(token) > remaining:
                pieces.append(token[:remaining])
                truncated = True
                break
            pieces.append(token)
            used += separator + len(token)
        if not pieces:
            return ""
        normalized = " ".join(pieces)
        if truncated:
            return normalized + "... [truncated]"
        return normalized

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
