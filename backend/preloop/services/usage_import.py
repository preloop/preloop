"""Usage ingest service: spend the model gateway cannot see (issue #123).

Cursor's bundled models (Composer, Auto, ...) never traverse the Preloop
model gateway, so their spend is invisible to Cost analytics. This service
ingests normalized usage events — pushed through the API or parsed from the
Cursor dashboard Usage CSV export — into the cost ledger as
``action_type='imported_usage'`` rows labeled ``usage_source='imported'``.

Design invariants:

- Imported rows NEVER mix with gateway-metered spend: every gateway
  aggregation filters on ``action_type == 'model_gateway'`` and budget-bucket
  accumulation is not performed for imported rows.
- Re-importing the same data is idempotent: each event carries a stable
  fingerprint and duplicate fingerprints are skipped.
- No reverse-engineered vendor auth: input is either the caller's own
  normalized events or the CSV file the vendor's dashboard officially exports.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_managed_agent
from preloop.models.models.managed_agent import ManagedAgent
from preloop.schemas.usage_import import (
    UsageImportEvent,
    UsageIngestRecord,
    UsageIngestRecordResult,
)

logger = logging.getLogger(__name__)

#: Default agent kind resolved when the caller does not name an agent.
DEFAULT_AGENT_KIND = "cursor"

#: Hard cap on CSV size accepted by the import endpoint (10 MiB).
MAX_CSV_BYTES = 10 * 1024 * 1024

#: Hard cap on data rows per CSV import; larger exports must be split.
MAX_CSV_ROWS = 10_000


class UsageImportError(ValueError):
    """Raised when an ingest request cannot be attributed or parsed."""


@dataclass
class CsvParseResult:
    """Outcome of parsing a Cursor usage CSV export."""

    events: List[UsageImportEvent] = field(default_factory=list)
    parsed_rows: int = 0
    skipped_rows: int = 0
    skipped_row_reasons: List[str] = field(default_factory=list)


def resolve_target_agent(
    db: Session,
    *,
    account_id: str,
    agent_id: Optional[str] = None,
    default_agent_kind: str = DEFAULT_AGENT_KIND,
) -> ManagedAgent:
    """Resolve the managed agent imported events are attributed to.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent_id: Explicit managed agent id, when the caller provided one.
        default_agent_kind: Agent kind used for default resolution.

    Returns:
        The target ManagedAgent.

    Raises:
        UsageImportError: When the explicit agent does not exist in the
            account, when no agent of the default kind exists, or when
            several exist and the caller must disambiguate.
    """
    if agent_id:
        agent = crud_managed_agent.get_for_account(
            db, account_id=account_id, agent_id=str(agent_id)
        )
        if agent is None:
            raise UsageImportError(f"Managed agent {agent_id} not found")
        return agent

    candidates = crud_managed_agent.list_by_kind(
        db, account_id=account_id, agent_kind=default_agent_kind
    )
    if not candidates:
        raise UsageImportError(
            f"No managed '{default_agent_kind}' agent found. Onboard one with "
            f"`preloop agents onboard {default_agent_kind.title()}`, register "
            f"one with `POST /api/v1/agents` "
            f'(`{{"display_name": "...", "agent_kind": "{default_agent_kind}"}}`), '
            "or pass agent_id explicitly."
        )
    if len(candidates) > 1:
        ids = ", ".join(str(agent.id) for agent in candidates[:5])
        raise UsageImportError(
            f"Multiple managed '{default_agent_kind}' agents found ({ids}). "
            "Pass agent_id to choose one."
        )
    return candidates[0]


def event_fingerprint(
    event: UsageImportEvent, *, source: str, agent_principal_id: str
) -> str:
    """Compute a stable dedupe fingerprint for one normalized event.

    The fingerprint covers the fields that identify a usage row at the
    source (timestamp, model, token counts, charged amount, session id) plus
    the attribution target, so importing the same CSV twice — or replaying
    the same API batch — cannot double-count spend, while two genuinely
    distinct events that happen to share a timestamp+model still both land
    if any measured quantity differs.
    """
    cost = event.resolved_cost_usd()
    payload = "|".join(
        [
            source,
            agent_principal_id,
            event.timestamp.isoformat(),
            event.model,
            str(event.prompt_tokens),
            str(event.completion_tokens),
            str(event.total_tokens),
            str(event.cache_read_tokens),
            str(event.cache_creation_tokens),
            f"{cost:.6f}" if cost is not None else "None",
            event.session_id or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ingest_events(
    db: Session,
    *,
    account_id: str,
    user_id: Optional[str],
    agent: ManagedAgent,
    events: List[UsageImportEvent],
    source: str,
) -> Tuple[int, int]:
    """Write normalized events into the cost ledger, deduplicated.

    Args:
        db: Database session.
        account_id: Owning account id.
        user_id: Importing user's id, for audit.
        agent: Managed agent the events are attributed to.
        events: Normalized usage events.
        source: Origin label (e.g. ``cursor``).

    Returns:
        Tuple of (imported_count, skipped_duplicate_count). The write is
        committed once at the end so a batch is all-or-nothing.

    Raises:
        UsageImportError: When the agent has no ``session_source_id`` — the
            fingerprint is scoped by it, so a missing value would silently
            merge the dedupe scopes of unrelated agents.
    """
    imported = 0
    skipped = 0
    principal_id = agent.session_source_id
    if not principal_id:
        raise UsageImportError(
            f"Managed agent {agent.id} has no session_source_id; imported "
            "events cannot be fingerprinted for deduplication"
        )
    for event in events:
        timestamp = event.timestamp
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        meta: Dict[str, Any] = {"managed_agent_id": str(agent.id)}
        if event.session_id:
            meta["source_session_id"] = event.session_id
        if event.kind:
            meta["source_kind"] = event.kind
        if event.max_mode is not None:
            meta["max_mode"] = event.max_mode
        if event.meta:
            for key, value in event.meta.items():
                meta.setdefault(key, value)
        row = crud_api_usage.log_imported_usage_event(
            db,
            account_id=account_id,
            user_id=user_id,
            timestamp=timestamp,
            model_alias=event.model,
            source=source,
            prompt_tokens=event.prompt_tokens,
            completion_tokens=event.completion_tokens,
            total_tokens=event.total_tokens,
            cache_read_tokens=event.cache_read_tokens,
            cache_creation_tokens=event.cache_creation_tokens,
            cost_usd=event.resolved_cost_usd(),
            runtime_principal_type=agent.session_source_type,
            runtime_principal_id=principal_id,
            runtime_principal_name=agent.display_name,
            import_fingerprint=event_fingerprint(
                event, source=source, agent_principal_id=principal_id
            ),
            meta_data=meta,
            commit=False,
        )
        if row is None:
            skipped += 1
        else:
            imported += 1
    db.commit()
    return imported, skipped


def push_record_fingerprint(*, source: str, external_id: str) -> str:
    """Compute the dedupe fingerprint for one pushed usage record.

    Pushed records are identified by (account, source, external_id): the
    caller supplies a stable source-side id, so a harness retrying a batch
    after a network timeout cannot double-count spend. The ``ingest|``
    prefix keeps this namespace disjoint from the content-hash fingerprints
    of the CSV/JSON import path, which share the same unique index.
    """
    payload = f"ingest|{source}|{external_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def push_record_content_hash(record: UsageIngestRecord) -> str:
    """Compute the canonical payload hash of one pushed usage record.

    Stored as ``meta_data.ingest_content_hash`` so a replay of the same
    (source, external_id) with a DIFFERENT payload can be flagged
    ``conflict`` in the response. First write still wins — the marker is a
    heuristic for the shipper's operator, never a rejection.
    """
    canonical = json.dumps(
        record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_push_records(
    db: Session,
    *,
    account_id: str,
    user_id: Optional[str],
    agent: ManagedAgent,
    records: List[UsageIngestRecord],
    source: str,
) -> List[UsageIngestRecordResult]:
    """Write pushed usage records into the cost ledger, deduplicated.

    Reuses the imported-usage normalization chokepoint
    (:meth:`~preloop.models.crud.api_usage.CRUDApiUsage.log_imported_usage_event`):
    rows land as ``action_type='imported_usage'`` / ``usage_source='imported'``,
    ``total_tokens`` derives from input+output only, and cache-read tokens
    stay in their own column — never summed into totals or charged spend.
    Conversation ids, message/tool counts, and cost_basis land in their
    first-class columns; lifecycle events (``event_type != 'usage'``) land
    as zero-cost rows unless explicitly priced.

    Args:
        db: Database session.
        account_id: Owning account id.
        user_id: Pushing user's id, for audit.
        agent: Managed agent the records are attributed to.
        records: Pushed usage records.
        source: Origin label (e.g. ``cursor``); scopes the dedupe key.

    Returns:
        Per-record results: ``deduplicated`` when the (source, external_id)
        was already stored, plus ``conflict`` when the replayed payload
        differs from the stored one (first write wins either way).
        Committed once at the end.
    """
    results: List[UsageIngestRecordResult] = []
    for record in records:
        fingerprint = push_record_fingerprint(
            source=source, external_id=record.external_id
        )
        content_hash = push_record_content_hash(record)
        existing = crud_api_usage.get_imported_row_by_fingerprint(
            db, account_id=account_id, import_fingerprint=fingerprint
        )
        if existing is None:
            timestamp = record.timestamp
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
            meta: Dict[str, Any] = {
                "managed_agent_id": str(agent.id),
                "external_id": record.external_id,
                "event_type": record.event_type,
                "ingest_content_hash": content_hash,
            }
            if record.metadata:
                for key, value in record.metadata.items():
                    meta.setdefault(key, value)
            row = crud_api_usage.log_imported_usage_event(
                db,
                account_id=account_id,
                user_id=user_id,
                timestamp=timestamp,
                model_alias=record.model,
                source=source,
                prompt_tokens=record.input_tokens,
                completion_tokens=record.output_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cost_usd=(
                    float(record.charged_cost)
                    if record.charged_cost is not None
                    else None
                ),
                cost_basis=record.cost_basis,
                conversation_id=record.conversation_id,
                parent_conversation_id=record.parent_conversation_id,
                message_count=record.message_count,
                tool_call_count=record.tool_call_count,
                runtime_principal_type=agent.session_source_type,
                runtime_principal_id=agent.session_source_id,
                runtime_principal_name=agent.display_name,
                import_fingerprint=fingerprint,
                meta_data=meta,
                commit=False,
            )
            if row is not None:
                results.append(UsageIngestRecordResult(external_id=record.external_id))
                continue
            # A concurrent request landed this fingerprint between the
            # existence check and the insert; re-read it for the conflict
            # comparison below.
            existing = crud_api_usage.get_imported_row_by_fingerprint(
                db, account_id=account_id, import_fingerprint=fingerprint
            )
        stored_hash = (
            (existing.meta_data or {}).get("ingest_content_hash")
            if existing is not None
            else None
        )
        results.append(
            UsageIngestRecordResult(
                external_id=record.external_id,
                deduplicated=True,
                # Rows written before content hashing (or by the CSV path)
                # have no stored hash and are non-comparable: no conflict.
                conflict=bool(stored_hash and stored_hash != content_hash),
            )
        )
    db.commit()
    return results


# ---------------------------------------------------------------------------
# Cursor dashboard Usage CSV export parsing
# ---------------------------------------------------------------------------
#
# Observed header shape (Cursor dashboard → Usage → Export CSV; confirmed via
# Cursor forum/staff posts and community parsers, 2026-07):
#
#   Date, [User,] Kind, Model, [Max Mode,] Input (w/ Cache Write),
#   Input (w/o Cache Write), Cache Read, Output Tokens, Total Tokens, Cost
#
# "Max Mode" and "User" appear in newer/team exports. The Cost column may
# contain "$1.23", "Included", "-", or empty. Header matching below is
# case-insensitive and prefix-based, so column reordering and the known
# variants parse without configuration; a custom ``column_map`` can override
# any logical field for future shapes.

_CURSOR_LOGICAL_FIELDS = (
    "date",
    "kind",
    "model",
    "max_mode",
    "input_with_cache_write",
    "input_without_cache_write",
    "cache_read",
    "output_tokens",
    "total_tokens",
    "cost",
)

_CURSOR_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
)


def _match_cursor_header(header: str) -> Optional[str]:
    """Map one raw CSV header to a logical field name, or None."""
    normalized = header.strip().lower().lstrip("﻿")
    if normalized == "date":
        return "date"
    if normalized == "kind" or normalized == "type":
        return "kind"
    if normalized == "model":
        return "model"
    if normalized == "max mode":
        return "max_mode"
    if normalized.startswith("input (w/ cache") or normalized.startswith(
        "input (w/cache"
    ):
        return "input_with_cache_write"
    if normalized.startswith("input (w/o cache"):
        return "input_without_cache_write"
    if normalized.startswith("cache read"):
        return "cache_read"
    if normalized.startswith("output"):
        return "output_tokens"
    if normalized.startswith("total"):
        return "total_tokens"
    if normalized == "cost" or normalized.startswith("cost to you"):
        return "cost"
    return None


def _parse_int(value: str) -> Optional[int]:
    """Parse a token count; tolerate separators and placeholder values."""
    cleaned = value.strip().replace(",", "")
    if not cleaned or cleaned in {"-", "N/A", "n/a"}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_cost(value: str) -> Optional[float]:
    """Parse a Cost cell; 'Included'/'-'/'' mean no charged amount."""
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower() in {"included", "-", "nan", "n/a", "free"}:
        return None
    cleaned = cleaned.replace("$", "").replace(",", "").strip()
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_date(value: str) -> Optional[datetime]:
    """Parse the Date cell across formats Cursor has emitted."""
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in _CURSOR_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def parse_cursor_usage_csv(
    content: bytes,
    *,
    column_map: Optional[Dict[str, str]] = None,
    max_skipped_reasons: int = 20,
    max_rows: int = MAX_CSV_ROWS,
) -> CsvParseResult:
    """Parse a Cursor dashboard Usage CSV export into normalized events.

    Args:
        content: Raw CSV bytes as exported from the Cursor dashboard.
        column_map: Optional overrides mapping logical field names
            (``date``, ``model``, ``cost``, ``kind``, ``max_mode``,
            ``input_with_cache_write``, ``input_without_cache_write``,
            ``cache_read``, ``output_tokens``, ``total_tokens``) to exact
            CSV header names, for export shapes the built-in matcher does
            not recognize.
        max_skipped_reasons: Cap on per-row skip reasons returned.
        max_rows: Hard cap on data rows; parsing aborts beyond it so one
            request cannot fan out into an unbounded number of events.

    Returns:
        CsvParseResult with normalized events and per-row skip accounting.

    Raises:
        UsageImportError: When the CSV has no parseable header (required
            columns ``Date`` and ``Model`` not found), or when it carries
            more than ``max_rows`` data rows (split the export and import
            in batches).
    """
    for key in column_map or {}:
        if key not in _CURSOR_LOGICAL_FIELDS:
            raise UsageImportError(f"Unknown column_map field: {key!r}")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UsageImportError("CSV is not valid UTF-8") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        raise UsageImportError("CSV is empty") from None

    index: Dict[str, int] = {}
    explicit = {
        header.strip().lower(): logical
        for logical, header in (column_map or {}).items()
    }
    for position, header in enumerate(headers):
        logical = explicit.get(header.strip().lower()) or _match_cursor_header(header)
        if logical and logical not in index:
            index[logical] = position

    if "date" not in index or "model" not in index:
        raise UsageImportError(
            "Unrecognized CSV header: required columns 'Date' and 'Model' not "
            f"found in {headers!r}. Pass column_map to map your export's "
            "columns onto the expected fields."
        )

    result = CsvParseResult()

    def cell(row: List[str], logical: str) -> str:
        position = index.get(logical)
        if position is None or position >= len(row):
            return ""
        return row[position]

    def skip(line_number: int, reason: str) -> None:
        result.skipped_rows += 1
        if len(result.skipped_row_reasons) < max_skipped_reasons:
            result.skipped_row_reasons.append(f"line {line_number}: {reason}")

    for line_number, row in enumerate(reader, start=2):
        if not row or all(not value.strip() for value in row):
            continue
        if result.parsed_rows >= max_rows:
            raise UsageImportError(
                f"CSV has more than {max_rows} data rows; split the export "
                "and import it in batches"
            )
        result.parsed_rows += 1

        timestamp = _parse_date(cell(row, "date"))
        if timestamp is None:
            skip(line_number, f"unparseable date {cell(row, 'date')!r}")
            continue
        model = cell(row, "model").strip()
        if not model:
            skip(line_number, "missing model")
            continue

        input_with = _parse_int(cell(row, "input_with_cache_write"))
        input_without = _parse_int(cell(row, "input_without_cache_write"))
        cache_read = _parse_int(cell(row, "cache_read"))
        output_tokens = _parse_int(cell(row, "output_tokens"))
        total_tokens = _parse_int(cell(row, "total_tokens"))
        cost = _parse_cost(cell(row, "cost"))

        # "Input (w/ Cache Write)" counts tokens that also wrote cache;
        # the two input columns are disjoint slices of prompt tokens.
        prompt_tokens = None
        if input_with is not None or input_without is not None:
            prompt_tokens = (input_with or 0) + (input_without or 0)

        if (
            prompt_tokens is None
            and output_tokens is None
            and (total_tokens is None and cost is None)
        ):
            skip(line_number, "row carries neither token counts nor a cost")
            continue

        kind = cell(row, "kind").strip() or None
        max_mode_raw = cell(row, "max_mode").strip().lower()
        max_mode = max_mode_raw in {"true", "yes", "1", "on"} if max_mode_raw else None

        result.events.append(
            UsageImportEvent(
                timestamp=timestamp,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_creation_tokens=input_with,
                cache_read_tokens=cache_read,
                cost_usd=cost,
                kind=kind,
                max_mode=max_mode,
            )
        )

    return result
