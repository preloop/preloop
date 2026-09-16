"""CRUD helpers for gateway interaction search corpus rows."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import String, and_, case, cast, func, or_, tuple_
from sqlalchemy.orm import Session

from ..models.api_usage import ApiUsage
from ..models.api_key import ApiKey
from ..models.flow import Flow
from ..models.flow_execution import FlowExecution
from ..models.gateway_usage_search_document import GatewayUsageSearchDocument
from ..models.runtime_session import RuntimeSession
from .base import CRUDBase


class CRUDGatewayUsageSearchDocument(CRUDBase[GatewayUsageSearchDocument]):
    """CRUD operations for `GatewayUsageSearchDocument`."""

    def get_by_api_usage_id(
        self, db: Session, *, api_usage_id: str
    ) -> Optional[GatewayUsageSearchDocument]:
        """Return the corpus row for a specific API usage record."""
        return (
            db.query(GatewayUsageSearchDocument)
            .filter(GatewayUsageSearchDocument.api_usage_id == api_usage_id)
            .first()
        )

    def upsert_for_api_usage(
        self,
        db: Session,
        *,
        api_usage: ApiUsage,
        searchable_text: str,
        meta_data: Optional[Dict[str, Any]] = None,
    ) -> GatewayUsageSearchDocument:
        """Create or update the search corpus row for one gateway interaction."""
        return self.upsert_for_api_usage_id(
            db,
            api_usage_id=str(api_usage.id),
            searchable_text=searchable_text,
            meta_data=meta_data,
        )

    def upsert_for_api_usage_id(
        self,
        db: Session,
        *,
        api_usage_id: str,
        searchable_text: str,
        meta_data: Optional[Dict[str, Any]] = None,
    ) -> GatewayUsageSearchDocument:
        """Upsert a corpus row from an id, without loading the usage row.

        The writer may be a background worker that only carries the id and a
        prepared document, so nothing here needs the ORM object or the
        payloads it was built from.
        """
        normalized_text = searchable_text.strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        existing = self.get_by_api_usage_id(db, api_usage_id=api_usage_id)

        if existing:
            existing.searchable_text = normalized_text
            existing.content_hash = content_hash
            existing.meta_data = meta_data
            db.add(existing)
            db.commit()
            db.refresh(existing)
            return existing

        db_obj = GatewayUsageSearchDocument(
            api_usage_id=api_usage_id,
            searchable_text=normalized_text,
            content_hash=content_hash,
            meta_data=meta_data,
        )
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def list_session_documents_page(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        before_timestamp: Optional[datetime] = None,
        before_api_usage_id: Optional[Any] = None,
        limit: int = 100,
    ) -> List[Tuple[ApiUsage, GatewayUsageSearchDocument]]:
        """Return one page of a session's indexed interactions, newest first.

        The join through usage rows is what makes this usable for a backfill:
        the corpus row holds the sanitised text and the usage row holds the
        account, the session and the timestamp the chunk is filed under.
        """
        stmt = (
            db.query(ApiUsage, GatewayUsageSearchDocument)
            .join(
                GatewayUsageSearchDocument,
                GatewayUsageSearchDocument.api_usage_id == ApiUsage.id,
            )
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id == runtime_session_id,
            )
        )
        if before_timestamp is not None and before_api_usage_id is not None:
            stmt = stmt.filter(
                tuple_(ApiUsage.timestamp, ApiUsage.id)
                < tuple_(before_timestamp, before_api_usage_id)
            )
        return (
            stmt.order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )

    def search_account_documents(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        query: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        model_alias: Optional[str] = None,
        flow_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        flow_execution_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        session_source_type: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return account-scoped gateway interaction search hits."""
        legacy_session_source_type = case(
            (ApiUsage.flow_execution_id.isnot(None), "flow_execution"),
            else_=None,
        )
        legacy_session_source_id = cast(ApiUsage.flow_execution_id, String)
        resolved_session_source_type = func.coalesce(
            RuntimeSession.session_source_type, legacy_session_source_type
        )
        resolved_session_source_id = func.coalesce(
            RuntimeSession.session_source_id, legacy_session_source_id
        )
        resolved_session_reference = func.coalesce(
            RuntimeSession.session_reference, FlowExecution.agent_session_reference
        )

        base_query = (
            db.query(
                GatewayUsageSearchDocument.id.label("document_id"),
                func.substring(
                    GatewayUsageSearchDocument.searchable_text, 1, 1000
                ).label("text_preview"),
                GatewayUsageSearchDocument.meta_data.label("document_meta_data"),
                ApiUsage,
                Flow.name.label("flow_name"),
                ApiKey.name.label("api_key_name"),
                RuntimeSession.id.label("resolved_runtime_session_id"),
                resolved_session_source_type.label("resolved_session_source_type"),
                resolved_session_source_id.label("resolved_session_source_id"),
                resolved_session_reference.label("resolved_session_reference"),
            )
            .join(ApiUsage, GatewayUsageSearchDocument.api_usage_id == ApiUsage.id)
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .outerjoin(ApiKey, ApiUsage.api_key_id == ApiKey.id)
            .outerjoin(FlowExecution, ApiUsage.flow_execution_id == FlowExecution.id)
            .outerjoin(RuntimeSession, ApiUsage.runtime_session_id == RuntimeSession.id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
            )
        )

        if start_date:
            base_query = base_query.filter(ApiUsage.timestamp >= start_date)
        if end_date:
            base_query = base_query.filter(ApiUsage.timestamp < end_date)

        normalized_query = " ".join(query.strip().split()) if query else None
        if normalized_query:
            base_query = base_query.filter(
                func.to_tsvector(
                    "simple", GatewayUsageSearchDocument.searchable_text
                ).op("@@")(func.websearch_to_tsquery("simple", normalized_query))
            )
        if ai_model_id:
            base_query = base_query.filter(ApiUsage.ai_model_id == ai_model_id)
        if provider_name:
            base_query = base_query.filter(ApiUsage.provider_name == provider_name)
        if model_alias:
            base_query = base_query.filter(ApiUsage.model_alias == model_alias)
        if flow_id:
            base_query = base_query.filter(ApiUsage.flow_id == flow_id)
        if runtime_principal_id:
            base_query = base_query.filter(
                ApiUsage.runtime_principal_id == runtime_principal_id
            )
        if api_key_id:
            base_query = base_query.filter(ApiUsage.api_key_id == api_key_id)
        if runtime_session_id and flow_execution_id:
            base_query = base_query.filter(
                or_(
                    ApiUsage.runtime_session_id == runtime_session_id,
                    and_(
                        ApiUsage.runtime_session_id.is_(None),
                        ApiUsage.flow_execution_id == flow_execution_id,
                    ),
                )
            )
        elif runtime_session_id:
            base_query = base_query.filter(
                ApiUsage.runtime_session_id == runtime_session_id
            )
        elif flow_execution_id:
            base_query = base_query.filter(
                ApiUsage.flow_execution_id == flow_execution_id
            )
        if session_source_type:
            base_query = base_query.filter(
                resolved_session_source_type == session_source_type
            )

        total = base_query.with_entities(
            func.count(GatewayUsageSearchDocument.id)
        ).scalar()
        rows = (
            base_query.order_by(
                ApiUsage.timestamp.desc(), GatewayUsageSearchDocument.id.desc()
            )
            .limit(limit)
            .offset(offset)
            .all()
        )
        items = []
        for row in rows:
            text_preview = row.text_preview
            usage = row.ApiUsage
            items.append(
                {
                    "api_usage_id": str(usage.id),
                    "ai_model_id": str(usage.ai_model_id)
                    if usage.ai_model_id
                    else None,
                    "timestamp": usage.timestamp,
                    "status_code": usage.status_code,
                    "outcome": "error" if usage.status_code >= 400 else "success",
                    "endpoint": usage.endpoint,
                    "method": usage.method,
                    "provider_name": usage.provider_name,
                    "model_alias": usage.model_alias,
                    "flow_id": str(usage.flow_id) if usage.flow_id else None,
                    "flow_name": row.flow_name,
                    "flow_execution_id": (
                        str(usage.flow_execution_id)
                        if usage.flow_execution_id
                        else None
                    ),
                    "runtime_session_id": (
                        str(row.resolved_runtime_session_id)
                        if row.resolved_runtime_session_id
                        else None
                    ),
                    "session_source_type": row.resolved_session_source_type,
                    "session_source_id": row.resolved_session_source_id,
                    "session_reference": row.resolved_session_reference,
                    "runtime_principal_type": usage.runtime_principal_type,
                    "runtime_principal_id": usage.runtime_principal_id,
                    "runtime_principal_name": usage.runtime_principal_name,
                    "auth_subject_type": usage.auth_subject_type,
                    "api_key_id": str(usage.api_key_id) if usage.api_key_id else None,
                    "api_key_name": row.api_key_name,
                    "estimated_cost": float(usage.estimated_cost or 0.0),
                    "prompt_tokens": int(usage.prompt_tokens or 0),
                    "completion_tokens": int(usage.completion_tokens or 0),
                    "total_tokens": int(usage.total_tokens or 0),
                    "excerpt": self._build_excerpt(
                        text_preview, query=normalized_query
                    ),
                    "meta_data": {
                        **(row.document_meta_data or {}),
                        **(usage.meta_data or {}),
                    },
                }
            )

        return {"total": int(total or 0), "items": items}

    @staticmethod
    def _build_excerpt(searchable_text: str, *, query: Optional[str]) -> str:
        """Return a short readable preview for a search hit."""
        normalized_text = " ".join((searchable_text or "").split())
        if not normalized_text:
            return ""

        if not query:
            return (
                normalized_text[:317] + "..."
                if len(normalized_text) > 320
                else normalized_text
            )

        lowered_text = normalized_text.lower()
        lowered_query = " ".join(query.strip().lower().split())
        match_index = lowered_text.find(lowered_query)
        if match_index < 0:
            return (
                normalized_text[:317] + "..."
                if len(normalized_text) > 320
                else normalized_text
            )

        start = max(match_index - 120, 0)
        end = min(match_index + len(lowered_query) + 200, len(normalized_text))
        excerpt = normalized_text[start:end]
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(normalized_text):
            excerpt = excerpt + "..."
        return excerpt
