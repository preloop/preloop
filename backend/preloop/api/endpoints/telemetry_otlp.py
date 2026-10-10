"""OTLP/HTTP receiver for Claude client telemetry (issue #1412).

``POST /api/v1/telemetry/otlp/v1/{metrics,logs,traces}``, so an exporter
whose base URL is ``<preloop>/api/v1/telemetry/otlp`` reaches the standard
per-signal paths. Auth is a Preloop API key carrying ``telemetry:ingest``,
sent as ``Authorization: Bearer`` or ``x-api-key``.

Responses follow OTLP/HTTP: 200 with an empty ``Export*ServiceResponse`` in
the request's encoding (``partial_success`` with ``rejected_*`` counts when
records were dropped), 400 undecodable, 413 too large, 415 other content
types, 429 with ``Retry-After`` on the per-key rate limit.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import TELEMETRY_INGEST_SCOPE
from preloop.models.crud import crud_api_key, crud_telemetry_ingest
from preloop.models.db.session import get_db_session
from preloop.services import otlp_telemetry as otlp

logger = logging.getLogger(__name__)

router = APIRouter()

#: Requests per key per minute. Exporters back off on 429 + Retry-After.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("PRELOOP_OTLP_INGEST_RATE_LIMIT", "600"))
_RATE_WINDOW_SECONDS = 60.0
_rate_lock = threading.Lock()
_rate_hits: Dict[str, Deque[float]] = {}


def reset_rate_limits() -> None:
    """Clear the in-process rate limit windows (tests)."""
    with _rate_lock:
        _rate_hits.clear()


def _rate_limited(key_id: str, now: Optional[float] = None) -> Optional[int]:
    """Record a hit; return Retry-After seconds when over the limit."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = _rate_hits.setdefault(key_id, deque())
        while hits and hits[0] <= now - _RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MINUTE:
            return max(1, int(hits[0] + _RATE_WINDOW_SECONDS - now) + 1)
        hits.append(now)
    return None


def _error(status_code: int, message: str, headers: Optional[dict] = None) -> Response:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message},
        headers=headers,
    )


def _presented_key(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    api_key = request.headers.get("x-api-key")
    return api_key.strip() if api_key and api_key.strip() else None


def _authenticate(db: Session, token: str) -> Any:
    """Return the active key for ``token`` or None."""
    key = crud_api_key.get_by_key(db, key=token)
    if key is None or not key.is_active or key.is_expired:
        return None
    return key


@dataclass
class RawBody:
    """The request body, or the error reading it produced.

    Read by an async dependency so the handlers can stay synchronous (they
    run on the threadpool with a request-scoped session). The error is
    carried, not raised, so authentication still answers first.
    """

    data: bytes = b""
    error: Optional[otlp.OtlpError] = None


async def read_raw_body(request: Request) -> RawBody:
    """Read the raw body, refusing more than the cap before decompression."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > otlp.MAX_BODY_BYTES:
        return RawBody(error=otlp.OtlpTooLargeError("Request body exceeds 4 MiB"))
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > otlp.MAX_BODY_BYTES:
            return RawBody(error=otlp.OtlpTooLargeError("Request body exceeds 4 MiB"))
    return RawBody(data=bytes(chunks))


def _process(db: Session, signal: str, message: Any, key: Any) -> tuple[int, str]:
    """Extract and reconcile one decoded export. Returns (rejected, message)."""
    now = otlp.utc_now()
    if signal == otlp.SIGNAL_TRACES:
        logger.info(
            "OTLP traces acknowledged, not stored: %d spans", otlp.count_spans(message)
        )
        return 0, ""
    context = otlp.IngestContext(account_id=key.account_id, api_key_id=key.id, now=now)
    if signal == otlp.SIGNAL_LOGS:
        extraction = otlp.extract_logs(message, account_id=key.account_id, now=now)
    else:
        extraction = otlp.extract_metrics(message, account_id=key.account_id, now=now)
    ingestor = otlp.TelemetryIngestor(db, context)
    try:
        if signal == otlp.SIGNAL_LOGS:
            stats = ingestor.ingest_logs(extraction.logs)
        else:
            stats = ingestor.ingest_metrics(extraction.metrics)
        crud_telemetry_ingest.prune(db, now=now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info(
        "OTLP %s ingest for account %s: total=%d dropped=%d over_limit=%d "
        "enriched=%d created=%d aggregated=%d suppressed=%d duplicates=%d",
        signal,
        key.account_id,
        extraction.total,
        extraction.dropped,
        extraction.over_limit,
        stats.enriched,
        stats.created,
        stats.aggregated,
        stats.suppressed,
        stats.duplicates,
    )
    parts = []
    if extraction.dropped:
        parts.append(
            f"{extraction.dropped} records outside the Preloop allowlist dropped"
        )
    if extraction.over_limit:
        parts.append(
            f"{extraction.over_limit} records over the "
            f"{otlp.MAX_RECORDS_PER_REQUEST} per-request limit dropped"
        )
    return extraction.rejected, "; ".join(parts)


def _export(request: Request, signal: str, raw: RawBody, db: Session) -> Response:
    token = _presented_key(request)
    if token is None:
        return _error(status.HTTP_401_UNAUTHORIZED, "API key required")
    key = _authenticate(db, token)
    if key is None:
        return _error(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    scopes = key.scopes if isinstance(key.scopes, (list, tuple)) else []
    if TELEMETRY_INGEST_SCOPE not in scopes:
        return _error(
            status.HTTP_403_FORBIDDEN,
            f"API key lacks the {TELEMETRY_INGEST_SCOPE} scope",
        )
    retry_after = _rate_limited(str(key.id))
    if retry_after is not None:
        return _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        encoding = otlp.parse_content_type(request.headers.get("content-type"))
        if raw.error is not None:
            raise raw.error
        body = otlp.decompress_body(raw.data, request.headers.get("content-encoding"))
        message = otlp.decode_request(signal, body, encoding)
    except otlp.OtlpError as exc:
        logger.info(
            "OTLP %s export refused (%d): %s",
            signal,
            exc.status_code,
            type(exc).__name__,
        )
        return _error(exc.status_code, exc.public_message)
    rejected, error_message = _process(db, signal, message, key)
    return Response(
        content=otlp.encode_response(
            signal, encoding, rejected=rejected, error_message=error_message
        ),
        media_type=otlp.MEDIA_TYPES[encoding],
        status_code=status.HTTP_200_OK,
    )


_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    200: {
        "description": "Export*ServiceResponse, partial_success when records were dropped"
    },
    400: {"description": "Undecodable body"},
    401: {"description": "Missing or invalid API key"},
    403: {"description": "API key lacks the telemetry:ingest scope"},
    413: {"description": "Body over 4 MiB after decompression"},
    415: {
        "description": "Content type other than application/x-protobuf or application/json"
    },
    429: {"description": "Per-key rate limit; honour Retry-After"},
}


@router.post("/telemetry/otlp/v1/metrics", responses=_RESPONSES)
def export_metrics(
    request: Request,
    raw: RawBody = Depends(read_raw_body),
    db: Session = Depends(get_db_session),
) -> Response:
    """Ingest an OTLP metrics export (cost, token and session counters)."""
    return _export(request, otlp.SIGNAL_METRICS, raw, db)


@router.post("/telemetry/otlp/v1/logs", responses=_RESPONSES)
def export_logs(
    request: Request,
    raw: RawBody = Depends(read_raw_body),
    db: Session = Depends(get_db_session),
) -> Response:
    """Ingest an OTLP logs export (``api_request`` and ``api_error`` events)."""
    return _export(request, otlp.SIGNAL_LOGS, raw, db)


@router.post("/telemetry/otlp/v1/traces", responses=_RESPONSES)
def export_traces(
    request: Request,
    raw: RawBody = Depends(read_raw_body),
    db: Session = Depends(get_db_session),
) -> Response:
    """Acknowledge an OTLP traces export. Traces are counted, not stored."""
    return _export(request, otlp.SIGNAL_TRACES, raw, db)
