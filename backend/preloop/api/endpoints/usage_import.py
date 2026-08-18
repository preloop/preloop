"""Usage ingest endpoints: import externally observed spend (issue #123).

Cursor's bundled models never traverse the Preloop model gateway, so their
spend is invisible to Cost analytics. These endpoints accept that spend as
normalized usage events (JSON) or as the Cursor dashboard Usage CSV export,
attribute it to a managed agent, and record it with
``usage_source='imported'`` so it is labeled apart from gateway-metered
spend and never mixes into gateway budgets.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.usage_import import (
    UsageImportCsvResponse,
    UsageImportRequest,
    UsageImportResponse,
    UsageIngestRequest,
    UsageIngestResponse,
)
from preloop.services.usage_import import (
    MAX_CSV_BYTES,
    UsageImportError,
    ingest_events,
    ingest_push_records,
    parse_cursor_usage_csv,
    resolve_target_agent,
)
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/usage", tags=["Usage Import"])

#: Chunk size for reading CSV uploads without buffering the whole body.
_CSV_READ_CHUNK_BYTES = 1024 * 1024


def _csv_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"CSV exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MiB limit",
    )


@router.post("/import", response_model=UsageImportResponse)
@require_permission("import_usage")
def import_usage_events(
    payload: UsageImportRequest,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> UsageImportResponse:
    """Ingest a batch of normalized usage events into the cost ledger.

    Events are attributed to the given managed agent (default: the account's
    managed Cursor agent from onboarding) and recorded with
    ``usage_source='imported'``. Re-sending the same events is idempotent —
    duplicates are counted in ``skipped_duplicates``, never double-billed.
    """
    get_account_or_404(db, current_user)
    try:
        agent = resolve_target_agent(
            db,
            account_id=str(current_user.account_id),
            agent_id=str(payload.agent_id) if payload.agent_id else None,
        )
        imported, skipped = ingest_events(
            db,
            account_id=str(current_user.account_id),
            user_id=str(current_user.id),
            agent=agent,
            events=payload.events,
            source=payload.source,
        )
    except UsageImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "Imported %d usage events (%d duplicates skipped) for agent %s "
        "(source=%s, account=%s)",
        imported,
        skipped,
        agent.id,
        payload.source,
        current_user.account_id,
    )
    return UsageImportResponse(
        imported=imported,
        skipped_duplicates=skipped,
        agent_id=UUID(str(agent.id)),
        agent_display_name=agent.display_name,
        source=payload.source,
    )


@router.post("/ingest", response_model=UsageIngestResponse)
@require_permission("import_usage")
def ingest_usage_records(
    payload: UsageIngestRequest,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> UsageIngestResponse:
    """Push a batch of external usage records into the cost ledger.

    The continuous-push evolution of ``POST /usage/import``: a harness
    posts sanitized spend records — or hook-shaped lifecycle events
    (``event_type``) — as they occur instead of batch CSV. Each record is
    identified by (source, external_id) per account, so replaying a batch
    — e.g. a retry after a network timeout — returns 200 with the
    replayed records flagged ``deduplicated`` and never double-counts
    spend; a replay whose payload differs from the stored record is
    additionally flagged ``conflict`` (first write wins, never a 409).
    Records carry ``conversation_id`` / ``parent_conversation_id`` so
    subagent workers billed on separate conversations can be rolled up
    under their parent thread, and ``cost_basis`` so reconciled
    billing-export records supersede hook-derived estimates in cost
    summaries. Attribution precedence: explicit ``agent_id`` (must belong
    to the account) wins; otherwise the account's single managed agent
    whose kind equals ``source`` is used, and zero or multiple candidates
    is a 422.
    """
    get_account_or_404(db, current_user)
    try:
        agent = resolve_target_agent(
            db,
            account_id=str(current_user.account_id),
            agent_id=str(payload.agent_id) if payload.agent_id else None,
            default_agent_kind=payload.source,
        )
        results = ingest_push_records(
            db,
            account_id=str(current_user.account_id),
            user_id=str(current_user.id),
            agent=agent,
            records=payload.records,
            source=payload.source,
        )
    except UsageImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    accepted = sum(1 for result in results if not result.deduplicated)
    conflicts = sum(1 for result in results if result.conflict)
    logger.info(
        "Ingested %d usage records (%d deduplicated, %d conflicts) for "
        "agent %s (source=%s, account=%s)",
        accepted,
        len(results) - accepted,
        conflicts,
        agent.id,
        payload.source,
        current_user.account_id,
    )
    return UsageIngestResponse(
        source=payload.source,
        agent_id=UUID(str(agent.id)),
        agent_display_name=agent.display_name,
        accepted=accepted,
        deduplicated=len(results) - accepted,
        conflicts=conflicts,
        results=results,
    )


@router.post("/import/csv", response_model=UsageImportCsvResponse)
@require_permission("import_usage")
async def import_usage_csv(
    file: UploadFile = File(..., description="Cursor dashboard Usage CSV export"),
    agent_id: Optional[UUID] = Form(default=None),
    source: str = Form(default="cursor"),
    column_map: Optional[str] = Form(
        default=None,
        description=(
            "Optional JSON object mapping logical fields (date, model, cost, "
            "kind, max_mode, input_with_cache_write, input_without_cache_write, "
            "cache_read, output_tokens, total_tokens) to your export's exact "
            "column headers, for CSV shapes the built-in matcher does not "
            "recognize."
        ),
    ),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> UsageImportCsvResponse:
    """Import a Cursor dashboard Usage CSV export into the cost ledger.

    Parses the export (Date / Kind / Model / Max Mode / Input (w/ Cache
    Write) / Input (w/o Cache Write) / Cache Read / Output Tokens / Total
    Tokens / Cost), normalizes each row, and ingests it through the same
    pipeline as ``POST /usage/import``. Rows whose Cost is "Included" are
    stored with tokens but no charged amount. Re-importing the same file is
    idempotent.
    """
    get_account_or_404(db, current_user)

    parsed_column_map = None
    if column_map:
        try:
            parsed_column_map = json.loads(column_map)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422, detail=f"column_map is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed_column_map, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parsed_column_map.items()
        ):
            raise HTTPException(
                status_code=422,
                detail="column_map must be a JSON object of string to string",
            )

    # First line of defense: Starlette populates UploadFile.size from the
    # multipart parsing, so an oversized upload is rejected without reading
    # the body at all. The chunked read below is the backstop for uploads
    # that arrive without a usable size, bounding memory to the cap.
    if file.size is not None and file.size > MAX_CSV_BYTES:
        raise _csv_too_large()

    chunks: list[bytes] = []
    received = 0
    while chunk := await file.read(_CSV_READ_CHUNK_BYTES):
        received += len(chunk)
        if received > MAX_CSV_BYTES:
            raise _csv_too_large()
        chunks.append(chunk)
    content = b"".join(chunks)

    normalized_source = source.strip().lower() or "cursor"
    try:
        agent = resolve_target_agent(
            db,
            account_id=str(current_user.account_id),
            agent_id=str(agent_id) if agent_id else None,
        )
        parse_result = parse_cursor_usage_csv(content, column_map=parsed_column_map)
        imported, skipped = ingest_events(
            db,
            account_id=str(current_user.account_id),
            user_id=str(current_user.id),
            agent=agent,
            events=parse_result.events,
            source=normalized_source,
        )
    except UsageImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "CSV import: %d rows parsed, %d imported, %d duplicates, %d skipped "
        "rows for agent %s (account=%s)",
        parse_result.parsed_rows,
        imported,
        skipped,
        parse_result.skipped_rows,
        agent.id,
        current_user.account_id,
    )
    return UsageImportCsvResponse(
        imported=imported,
        skipped_duplicates=skipped,
        agent_id=UUID(str(agent.id)),
        agent_display_name=agent.display_name,
        source=normalized_source,
        parsed_rows=parse_result.parsed_rows,
        skipped_rows=parse_result.skipped_rows,
        skipped_row_reasons=parse_result.skipped_row_reasons,
    )
