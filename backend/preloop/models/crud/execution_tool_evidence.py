"""Scoped durable tool evidence and safe terminal-delivery diagnostics."""

import logging
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import String, cast, tuple_
from sqlalchemy.dialects.postgresql import insert

from sqlalchemy.orm import Session

from preloop.utils.jsonb_sanitize import remove_unencodable, sanitize_for_jsonb

from preloop.models import models
from preloop.services.execution_tool_evidence import (
    EVIDENCE_TYPE,
    REASON_CREDENTIAL_SCRUB,
    REASON_LONE_SURROGATES,
    REASON_MASKING,
    REASON_STORAGE_RULE,
    canonical_bytes,
    digest,
    note_incomplete,
)


logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 8 * 1024 * 1024


def _fail_soft(
    db: Session,
    *,
    execution_id: Any,
    source: str,
    source_id: str | Callable[[], str | None] | None,
    write: Callable[[], None],
) -> None:
    """Run an evidence write in a savepoint so a failure never aborts the caller.

    Evidence rides on the gateway usage transaction and the agent log batch.
    Losing either to an evidence failure would defeat the point of evidence,
    so failures are logged and replaced by a capture-failure marker.

    Args:
        db: Session of the surrounding transaction.
        execution_id: Execution the evidence belongs to.
        source: ``gateway`` or ``native``.
        source_id: Usage id, or lazy native item id used only after failure.
        write: Callable performing the evidence insert.
    """
    try:
        with db.begin_nested():
            write()
        return
    except Exception as exc:  # noqa: BLE001 - evidence must never break callers
        logger.warning(
            "Tool evidence capture failed for execution %s (%s): %s",
            execution_id,
            source,
            type(exc).__name__,
        )
    try:
        source_id = source_id() if callable(source_id) else source_id
        if not source_id:
            return
        with db.begin_nested():
            _persist_document(
                db,
                execution_id=UUID(str(execution_id)),
                source=source,
                source_id=source_id,
                document={
                    "version": 1,
                    "execution_id": str(execution_id),
                    "api_usage_id": source_id if source == "gateway" else None,
                    "capture_complete": False,
                    "capture_failed": True,
                    "observations": [],
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Tool evidence failure marker not stored for execution %s: %s",
            execution_id,
            type(exc).__name__,
        )


def _observation_key(observation: dict[str, Any]) -> str | None:
    """Identify an exact sanitized observation, preserving changed call content."""
    from preloop.utils.secret_scrubbing import scrub_structure

    call_id = observation.get("call_id")
    if observation.get("source") != "request" or not isinstance(call_id, str):
        return None
    return digest(scrub_structure(remove_unencodable(observation)[0]))


def _seen_request_keys(db: Session, execution_id: UUID) -> set[str]:
    """Load request-side observation keys already stored for an execution.

    The keys live in the evidence row's ``message`` column, which PostgreSQL
    stores and detoasts separately from ``metadata``. Selecting only that
    column on the indexed execution rows never reads the stored documents.
    """
    import json

    rows = db.query(models.FlowExecutionLog.message).filter(
        models.FlowExecutionLog.execution_id == execution_id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
        models.FlowExecutionLog.message.isnot(None),
    )
    seen: set[str] = set()
    for (encoded,) in rows:
        try:
            keys = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if isinstance(keys, list):
            seen.update(key for key in keys if isinstance(key, str))
    return seen


def persist_tool_evidence(
    db: Session, *, usage: models.ApiUsage, document: dict[str, Any]
) -> None:
    """Persist a bounded, sanitized record before the console payload is clipped.

    Never raises: a failure is logged and recorded as a capture-failure marker
    so the surrounding usage row survives.

    Args:
        db: Session of the gateway usage transaction.
        usage: Usage row the evidence belongs to.
        document: Document produced by ``capture_tools``.
    """
    if not usage.flow_execution_id:
        return
    _fail_soft(
        db,
        execution_id=usage.flow_execution_id,
        source="gateway",
        source_id=str(usage.id) if usage.id else None,
        write=lambda: _persist_tool_evidence(db, usage=usage, document=document),
    )


def _persist_tool_evidence(
    db: Session, *, usage: models.ApiUsage, document: dict[str, Any]
) -> None:
    """Deduplicate replayed request-side observations, then store the record."""
    from preloop.services.sensitive_data.storage import apply_storage_redaction

    document = {
        **document,
        "execution_id": str(usage.flow_execution_id),
        "api_usage_id": str(usage.id),
    }
    sanitized = apply_storage_redaction(str(usage.account_id), document)
    storage_rule_changed = sanitized != document
    sanitized["storage_redacted"] = (
        document.get("storage_redacted", False) or storage_rule_changed
    )
    if storage_rule_changed:
        note_incomplete(sanitized, REASON_STORAGE_RULE)
    document = sanitized
    observations = document.get("observations")
    if isinstance(observations, list) and any(
        _observation_key(item) for item in observations if isinstance(item, dict)
    ):
        seen = _seen_request_keys(db, usage.flow_execution_id)
        kept = [
            item
            for item in observations
            if not isinstance(item, dict) or _observation_key(item) not in seen
        ]
        document["observations"] = kept
        document["request_duplicates_omitted"] = len(observations) - len(kept)
    _persist_document(
        db,
        execution_id=usage.flow_execution_id,
        source="gateway",
        source_id=str(usage.id),
        document=document,
    )


def _persist_document(
    db: Session,
    *,
    execution_id: UUID,
    source: str,
    source_id: str,
    document: dict[str, Any],
) -> None:
    """Idempotent observations keep duplicate log fetches out of the audit.

    The document is stored as canonical JSON text in ``chunks``, so every
    control character, U+0000 included, is stored exactly. Capture has
    already removed and counted lone surrogates; one left here (a document
    not built by capture) is removed and named the same way.
    """
    from preloop.utils.secret_scrubbing import scrub_structure

    # The id is a JSONB value of its own, outside the canonical text.
    source_id = sanitize_for_jsonb(source_id)
    safe, removed = remove_unencodable(document)
    if removed:
        from preloop.services.execution_tool_evidence import removed_facts

        safe = dict(safe)
        safe["storage_redacted"] = True
        # Outside an observation value (an id, for example): counted on the
        # document, since no observation's ``removed`` covers it.
        safe["removed"] = removed_facts(removed)
        note_incomplete(safe, REASON_LONE_SURROGATES)
    cleaned = scrub_structure(safe)
    if cleaned != safe:
        cleaned["storage_redacted"] = True
        note_incomplete(cleaned, REASON_CREDENTIAL_SCRUB)
    document = cleaned
    import json

    encoded = canonical_bytes(document).decode("utf-8")
    sha256 = digest(document)
    request_keys = sorted(
        {
            key
            for item in document.get("observations") or []
            if isinstance(item, dict) and (key := _observation_key(item))
        }
    )
    identity = uuid5(
        NAMESPACE_URL,
        f"preloop:tool-evidence:{execution_id}:{source}:{source_id}:{sha256}",
    )
    db.execute(
        insert(models.FlowExecutionLog)
        .values(
            id=identity,
            execution_id=execution_id,
            log_type=EVIDENCE_TYPE,
            # Dedupe keys only; kept out of metadata so lookups skip chunks.
            message=json.dumps(request_keys) if request_keys else None,
            metadata_={
                "source": source,
                "api_usage_id": source_id if source == "gateway" else None,
                "sha256": sha256,
                "capture_complete": document.get("capture_complete", False),
                "chunks": [
                    encoded[i : i + 16000] for i in range(0, len(encoded), 16000)
                ],
            },
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.flush()


def persist_native_command_evidence(
    db: Session, *, execution_id: str, line: Any
) -> None:
    """Capture Codex command events before the ordinary log string is capped.

    Never raises: a failure is logged and recorded as a capture-failure marker
    so the surrounding log batch is still written.

    Args:
        db: Session of the log batch transaction.
        execution_id: Execution that emitted the log line.
        line: Raw agent log line.
    """
    _fail_soft(
        db,
        execution_id=execution_id,
        source="native",
        source_id=lambda: _native_item_id(line),
        write=lambda: _persist_native_command_evidence(
            db, execution_id=execution_id, line=line
        ),
    )


def _native_item_id(line: Any) -> str | None:
    """Best-effort native item id for a failure marker."""
    import json

    if not isinstance(line, str) or not line.startswith("{"):
        return None
    try:
        item = json.loads(line).get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
    except (ValueError, AttributeError):
        return None
    if (
        not isinstance(item_id, str)
        or not 0 < len(item_id) <= 256
        or item.get("type") != "command_execution"
    ):
        return None
    return str(sanitize_for_jsonb(item_id))


# Admission facts are immutable for a running execution. Cache only complete
# review contexts, and negative non-review decisions, never ORM/session objects.
_INSPECTION_CONTEXTS: dict[str, tuple[dict[str, Any], dict[str, Any], str] | None] = {}
_INSPECTION_CONTEXT_CACHE_LIMIT = 256


def _inspection_context(
    db: Session, execution_id: str
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Resolve private inspection context once after its startup receipt arrives."""
    from copy import deepcopy
    from preloop.models.crud import crud_flow_execution
    from preloop.services.review_diff import review_diff_identity
    from preloop.services.review_integrity import applies, inspection_root

    if execution_id in _INSPECTION_CONTEXTS:
        return _INSPECTION_CONTEXTS[execution_id]
    execution = crud_flow_execution.get(db, id=execution_id)
    if execution is None:
        return None
    context = None
    cacheable = True
    if applies(execution.flow, execution):
        binding = execution.ci_review_binding
        diff = review_diff_identity(execution)
        context = (
            deepcopy(binding),
            deepcopy(diff or {}),
            inspection_root(execution.flow, binding),
        )
        # An early log can precede the startup receipt. Retry until it is known.
        receipt = getattr(execution, "startup_receipt", None)
        cacheable = diff is not None or (
            isinstance(receipt, dict)
            and receipt.get("checkout_verified") is True
            and receipt.get("setup_completed") is True
        )
    if cacheable:
        if len(_INSPECTION_CONTEXTS) >= _INSPECTION_CONTEXT_CACHE_LIMIT:
            _INSPECTION_CONTEXTS.clear()
        _INSPECTION_CONTEXTS[execution_id] = context
    return context


def _persist_native_command_evidence(
    db: Session, *, execution_id: str, line: Any
) -> None:
    """Build and store one native command observation."""
    import json
    from preloop.config import settings
    from preloop.models.crud.flow_execution_log import _account_for_execution
    from preloop.services.execution_tool_evidence import (
        MAX_EVIDENCE_BYTES,
        REASON_TRUNCATED,
        REASON_UNSAFE_TEXT,
        REASON_VALUE_DEPTH,
        ValueTooDeepError,
        _redact_value,
        removed_facts,
    )
    from preloop.services.sensitive_data.storage import apply_storage_redaction

    if not isinstance(line, str) or not line.startswith("{"):
        return
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("type") not in {
        "item.started",
        "item.completed",
    }:
        return
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str) or not 0 < len(item_id) <= 256:
        return
    account_id = _account_for_execution(db, execution_id)
    if account_id is None:
        return
    from preloop.services.model_gateway_events import REDACTION_RULESET

    raw = item.get("command")
    scrubbed = False
    too_deep = False
    removed: dict[str, int] = {}
    if settings.model_gateway_capture_content:
        try:
            value, redaction_facts, scrubbed = _redact_value(raw)
        except ValueTooDeepError:
            value, redaction_facts, too_deep = None, [], True
        value, removed = remove_unencodable(value)
    else:
        value, redaction_facts = None, []
    bounded = len(canonical_bytes(value)) <= MAX_EVIDENCE_BYTES
    storage_redacted = bool(removed) or too_deep
    complete = bounded and not storage_redacted
    observation: dict[str, Any] = {
        "source": "native",
        "kind": "command",
        "event": event["type"],
        "item_id": item_id,
        "value": value if bounded else None,
        "exit_code": item.get("exit_code")
        if type(item.get("exit_code")) is int
        else None,
        "redacted": raw != value,
        "truncated": not bounded,
    }
    if redaction_facts:
        observation["redactions"] = redaction_facts
    if removed:
        observation["removed"] = removed_facts(removed)
    reasons: list[str] = []
    if not bounded:
        reasons.append(REASON_TRUNCATED)
    if removed:
        reasons.append(REASON_LONE_SURROGATES)
    if too_deep:
        reasons.append(REASON_VALUE_DEPTH)
    # A named rule keeps capture_complete. The credential scrubber, or a
    # change nothing accounts for, is incomplete, as on the gateway path.
    if scrubbed:
        complete = False
        reasons.append(REASON_CREDENTIAL_SCRUB)
    if (
        settings.model_gateway_capture_content
        and value != raw
        and not (redaction_facts or removed or scrubbed or too_deep)
    ):
        complete = False
        reasons.append(REASON_UNSAFE_TEXT)
    document = {
        "version": 1,
        "execution_id": str(execution_id),
        "capture_complete": complete,
        "storage_redacted": storage_redacted,
        "redaction_ruleset": REDACTION_RULESET,
        "content_capture_enabled": bool(settings.model_gateway_capture_content),
        "observations": [observation],
    }
    if reasons:
        document["incomplete_reasons"] = sorted(reasons)
    sanitized = apply_storage_redaction(account_id, document)
    storage_rule_changed = sanitized != document
    sanitized["storage_redacted"] = (
        document.get("storage_redacted", False) or storage_rule_changed
    )
    if storage_rule_changed:
        note_incomplete(sanitized, REASON_STORAGE_RULE)
    observation = sanitized["observations"][0]
    if (
        event["type"] == "item.completed"
        and type(item.get("exit_code")) is int
        and item["exit_code"] == 0
        and (observation.get("value") != raw or observation.get("truncated"))
    ):
        from preloop.services.review_inspection import (
            command_reads_change,
            inspection_receipt,
        )

        context = _inspection_context(db, execution_id)
        if context is not None:
            binding, diff, root = context
            if command_reads_change(
                raw,
                base=binding.get("base_sha", ""),
                head=binding.get("head_sha", ""),
                files=diff.get("files", []),
                root=root,
                merge_base=diff.get("merge_base_sha") or "",
            ):
                observation["inspection_receipt"] = inspection_receipt(
                    binding, diff, root
                )
    _persist_document(
        db,
        execution_id=UUID(str(execution_id)),
        source="native",
        source_id=item_id,
        document=sanitized,
    )


def inspection_summary(
    db: Session,
    *,
    execution_id: Any,
    binding: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    root: str = "",
) -> dict[str, int]:
    """Count inspecting tool calls and native commands for one execution.

    Reads only integrity-valid stored evidence, oldest first, and stops at
    the first inspecting observation. A record whose digest does not verify
    counts as no evidence.

    Args:
        db: Database session.
        execution_id: Execution to summarise.
        binding: Admitted revision pair.
        diff: Trusted changed-file list.
        root: Admitted checkout path for absolute file reads.

    Returns:
        ``repository_tool_calls``, ``other_tool_calls`` and
        ``native_commands`` and ``diff_commands`` (lower bounds once a
        qualifying inspection is seen).
    """
    import json

    from preloop.services.review_integrity import ObservationCounter

    query = db.query(
        models.FlowExecutionLog.id,
        models.FlowExecutionLog.timestamp,
        models.FlowExecutionLog.metadata_,
    ).filter(
        models.FlowExecutionLog.execution_id == UUID(str(execution_id)),
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
    )
    counter = ObservationCounter(binding=binding, diff=diff, root=root)
    cursor = None
    while True:
        page = query
        if cursor is not None:
            page = page.filter(
                tuple_(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
                > cursor
            )
        rows = (
            page.order_by(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
            .limit(32)
            .all()
        )
        if not rows:
            break
        for row_id, timestamp, stored in rows:
            cursor = (timestamp, row_id)
            try:
                document = json.loads("".join((stored or {})["chunks"]))
            except (KeyError, TypeError, ValueError):
                continue
            if digest(document) != stored.get("sha256"):
                continue
            counter.add(document)
            if counter.inspected():
                return counter.counts()
        if len(rows) < 32:
            break
    return counter.counts()


def _gateway_coverage(db: Session, execution_id: UUID) -> tuple[int, int]:
    """Count gateway usage rows and how many have no evidence record.

    Args:
        db: Database session.
        execution_id: Execution whose usage rows are the expected requests.

    Returns:
        ``(gateway_requests, missing_records)``.
    """
    expected_query = db.query(models.ApiUsage).filter(
        models.ApiUsage.flow_execution_id == execution_id
    )
    expected = expected_query.count()
    captured_ids = db.query(
        models.FlowExecutionLog.metadata_["api_usage_id"].astext
    ).filter(
        models.FlowExecutionLog.execution_id == execution_id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
        models.FlowExecutionLog.metadata_["source"].astext == "gateway",
    )
    missing = expected_query.filter(
        ~cast(models.ApiUsage.id, String).in_(captured_ids)
    ).count()
    return expected, missing


_RULE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _valid_named_redaction(item: Any) -> bool:
    """True for one ``{"rule", "count"}`` fact the gate will count.

    Args:
        item: One entry of an observation's ``redactions`` list.

    Returns:
        Whether ``item`` names a rule and a positive count.
    """
    if not isinstance(item, dict):
        return False
    rule = item.get("rule")
    count = item.get("count")
    return (
        isinstance(rule, str)
        and _RULE_NAME.fullmatch(rule) is not None
        and type(count) is int
        and count >= 1
    )


def _masking_counts(document: dict[str, Any]) -> dict[str, int]:
    """Named masking rules on one document. Rule ids only, never values."""
    totals: dict[str, int] = {}
    observations = document.get("observations")
    if not isinstance(observations, list):
        return totals
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        redactions = observation.get("redactions")
        if not isinstance(redactions, list):
            continue
        for item in redactions:
            if not _valid_named_redaction(item):
                continue
            rule = str(item["rule"])
            count = int(item["count"])
            totals[rule] = totals.get(rule, 0) + count
    return totals


def _redacted_without_named_rule(document: dict[str, Any]) -> bool:
    """True when an observation was redacted and no named rule explains it.

    Args:
        document: Stored evidence document.

    Returns:
        Whether any observation has ``redacted`` set and no valid named fact.
    """
    observations = document.get("observations")
    if not isinstance(observations, list):
        return False
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        if observation.get("redacted") is not True:
            continue
        redactions = observation.get("redactions")
        named = isinstance(redactions, list) and any(
            _valid_named_redaction(item) for item in redactions
        )
        if not named:
            return True
    return False


def _record_class(document: Any, *, valid: bool) -> str:
    """Classify one stored record for the evidence gate.

    A record stored before an unexplained scrub or an account storage rule
    was written down as incomplete still fails closed: ``capture_complete``
    with no reasons is ``incomplete`` when ``storage_redacted`` is set, or
    when an observation is redacted without a named rule. A named mask that
    kept ``capture_complete``, and a record whose reasons are exactly
    ``{masking}``, stay ``masking_only``.

    Returns:
        ``ok``, ``masking_only`` or ``incomplete``. An unreadable record,
        a capture-failure marker, content capture turned off, or an
        incomplete record with no reason list is ``incomplete``.
    """
    if not valid or not isinstance(document, dict):
        return "incomplete"
    if document.get("capture_failed") is True:
        return "incomplete"
    if document.get("content_capture_enabled") is False:
        return "incomplete"
    raw_reasons = document.get("incomplete_reasons")
    reasons = (
        {item for item in raw_reasons if isinstance(item, str)}
        if isinstance(raw_reasons, list)
        else set()
    )
    if document.get("capture_complete") is True and not reasons:
        if document.get("storage_redacted") is True or _redacted_without_named_rule(
            document
        ):
            return "incomplete"
        # Native command records keep capture_complete when a named rule masked a value.
        if _masking_counts(document):
            return "masking_only"
        return "ok"
    if reasons == {REASON_MASKING} and _masking_counts(document):
        return "masking_only"
    return "incomplete"


def _incomplete_labels(document: Any, *, valid: bool) -> list[str]:
    """Why one record the gate counts as incomplete is incomplete.

    The stored ``incomplete_reasons`` when there are any. A record without
    them is named by what made it incomplete: ``integrity_invalid``
    (unreadable, or the digest does not verify), ``capture_failed``,
    ``content_capture_off``, or ``unstated`` for an older record that was
    marked incomplete or redacted without saying why.

    Args:
        document: Decoded stored document, or None.
        valid: Whether the record decoded and its digest verified.

    Returns:
        Reason names, sorted.
    """
    if not valid or not isinstance(document, dict):
        return ["integrity_invalid"]
    if document.get("capture_failed") is True:
        return ["capture_failed"]
    raw_reasons = document.get("incomplete_reasons")
    reasons = sorted(
        {item for item in raw_reasons if isinstance(item, str)}
        if isinstance(raw_reasons, list)
        else set()
    )
    if document.get("content_capture_enabled") is False:
        return sorted({*reasons, "content_capture_off"})
    if reasons:
        return reasons
    # Without stored reasons, agree with the gate: masking only when the
    # record is masking_only, never for a record storage_redacted refuses.
    if _record_class(document, valid=True) == "masking_only":
        return [REASON_MASKING]
    return ["unstated"]


def _removed_counts(document: dict[str, Any]) -> dict[str, int]:
    """Removed-text categories on one document. Categories only, never text."""
    totals: dict[str, int] = {}
    observations = document.get("observations")
    holders = [document, *(observations if isinstance(observations, list) else [])]
    for observation in holders:
        removed = observation.get("removed") if isinstance(observation, dict) else None
        if not isinstance(removed, list):
            continue
        for item in removed:
            if not isinstance(item, dict):
                continue
            category, count = item.get("category"), item.get("count")
            if (
                isinstance(category, str)
                and _RULE_NAME.fullmatch(category)
                and type(count) is int
                and count >= 1
            ):
                totals[category] = totals.get(category, 0) + count
    return totals


def tool_evidence_summary(db: Session, *, execution_id: Any) -> dict[str, Any]:
    """Count stored tool evidence without returning its contents.

    The same coverage figures as :func:`evidence_page`. Counts are safe for
    a CI identity that can read its own result: no observation values, paths
    or text.

    Args:
        db: Database session.
        execution_id: Execution to summarise.

    Returns:
        Gateway requests, missing records, records, incomplete records,
        records incomplete only because a named masking rule fired, masking
        counts by rule, incomplete records by reason (a record with two
        reasons counts under both), and removed text by category.
    """
    import json

    execution = UUID(str(execution_id))
    gateway_requests, missing_records = _gateway_coverage(db, execution)
    query = db.query(
        models.FlowExecutionLog.id,
        models.FlowExecutionLog.timestamp,
        models.FlowExecutionLog.metadata_,
    ).filter(
        models.FlowExecutionLog.execution_id == execution,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
    )
    records = 0
    incomplete_records = 0
    masking_only_records = 0
    masking: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    removed: dict[str, int] = {}
    cursor = None
    while True:
        page = query
        if cursor is not None:
            page = page.filter(
                tuple_(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
                > cursor
            )
        rows = (
            page.order_by(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
            .limit(32)
            .all()
        )
        if not rows:
            break
        for row_id, timestamp, stored in rows:
            cursor = (timestamp, row_id)
            records += 1
            meta = stored or {}
            try:
                document = json.loads("".join(meta["chunks"]))
                valid = digest(document) == meta.get("sha256")
            except (KeyError, TypeError, ValueError):
                document, valid = None, False
            kind = _record_class(document, valid=valid)
            if kind != "ok":
                incomplete_records += 1
                for reason in _incomplete_labels(document, valid=valid):
                    by_reason[reason] = by_reason.get(reason, 0) + 1
            if kind == "masking_only":
                masking_only_records += 1
            if valid and isinstance(document, dict):
                for rule, count in _masking_counts(document).items():
                    masking[rule] = masking.get(rule, 0) + count
                for category, count in _removed_counts(document).items():
                    removed[category] = removed.get(category, 0) + count
        if len(rows) < 32:
            break
    return {
        "gateway_requests": gateway_requests,
        "missing_records": missing_records,
        "records": records,
        "incomplete_records": incomplete_records,
        "masking_only_records": masking_only_records,
        "masking_by_rule": dict(sorted(masking.items())),
        "incomplete_by_reason": dict(sorted(by_reason.items())),
        "removed_by_category": dict(sorted(removed.items())),
    }


def evidence_page(
    db: Session, *, execution: models.FlowExecution, skip: int, limit: int
) -> dict[str, Any]:
    """Read only an execution already checked against fresh principal authority."""
    import json

    query = db.query(models.FlowExecutionLog).filter(
        models.FlowExecutionLog.execution_id == execution.id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
    )
    total = query.count()
    ordered = query.order_by(
        models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id
    )
    records = []
    page_bytes = 2  # JSON array brackets; metadata envelope is bounded separately.
    # One-row queries bound driver buffering too (yield_per is not sufficient
    # with every DBAPI cursor). Fetch at most one candidate beyond the page.
    for index in range(limit):
        row = ordered.offset(skip + index).limit(1).first()
        if row is None:
            break
        stored = row.metadata_ or {}
        try:
            document = json.loads("".join(stored["chunks"]))
            valid = digest(document) == stored["sha256"]
        except (KeyError, TypeError, ValueError):
            document, valid = None, False
        record = {
            "id": str(row.id),
            "api_usage_id": stored.get("api_usage_id"),
            "sha256": stored.get("sha256"),
            "integrity_valid": valid,
            "document": document if valid else None,
        }
        record_bytes = len(canonical_bytes(record)) + bool(records)
        if records and page_bytes + record_bytes > MAX_PAGE_BYTES:
            break
        records.append(record)
        page_bytes += record_bytes
    expected, missing = _gateway_coverage(db, execution.id)
    return {
        "records": records,
        "skip": skip,
        "total": total,
        "has_more": skip + len(records) < total,
        "next_skip": skip + len(records),
        "gateway_request_count": expected,
        "missing_gateway_requests": missing,
        "coverage": "missing" if missing else ("captured" if total else "unavailable"),
        "native_command_coverage": "observed_events_only",
        "representation": (
            "preloop.tool-evidence/v1; sorted compact JSON; UTF-8; "
            "text exact except removed lone surrogates; credentials masked"
        ),
        "page_sha256": digest(records),
    }


def callback_diagnostics(
    db: Session, *, execution: models.FlowExecution, principal_id: UUID
) -> list[dict[str, Any]]:
    """No URL, signing material, response body or other principal's deliveries."""
    rows = (
        db.query(models.WebhookDelivery)
        .join(models.WebhookEndpoint)
        .filter(
            models.WebhookDelivery.subject_id == execution.id,
            models.WebhookEndpoint.ci_principal_id == principal_id,
            models.WebhookDelivery.event_type == "flow.execution.finished",
        )
        .order_by(models.WebhookDelivery.created_at)
        .limit(100)
        .all()
    )
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "attempt_count": row.attempt_count,
            "response_status": row.response_status,
            "next_attempt_at": row.next_attempt_at,
        }
        for row in rows
    ]
