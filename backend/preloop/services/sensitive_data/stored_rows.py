"""Apply the current redaction and reference-only rules to stored rows (#2133).

Rules run at write time, so a rule installed or tightened today leaves rows
written yesterday as they were. This pass rewrites them, store by store, in
bounded batches, and keeps the audit hash chain verifiable while doing it.

## The audit chain

A sealed ``audit_log`` row cannot simply be edited: ``details`` is part of its
``row_hash`` and the next row's ``prev_hash`` commits to that hash. So:

* ``row_hash`` and ``prev_hash`` are never touched. The links and every
  signed checkpoint keep verifying as they are.
* The rewritten row records ``redacted_row_hash`` (the same canonical hash,
  same seq and prev_hash, over the new content), ``redaction_event_id`` and
  ``redacted_at``.
* Each batch that rewrites anything appends one ``redaction_applied`` audit
  row. Its details list, per sealed row, the id, seq, original ``row_hash``
  and ``redacted_row_hash``, plus rule ids, counts by type and by store, the
  actor and the window. Never the content. The sealer chains it like any
  other row.
* :func:`preloop.services.audit_chain.verify_chain` (and ``preloop audit
  verify``) accept a row that no longer hashes to ``row_hash`` only when it
  hashes to ``redacted_row_hash`` and the named event lists it with that hash.

Rows not yet sealed are rewritten in place; the sealer hashes the redacted
form. Each batch holds the account's chain head ``FOR UPDATE``, the lock the
sealer takes, so a rewrite and a seal never interleave.

## Properties

* Idempotent: the current rules are applied to the current content. Content
  already redacted, or already a reference record, does not change, so a
  second pass writes nothing and appends no event.
* Resumable: one transaction per batch. A failed batch rolls back on its own;
  the report carries the cursor of the last committed batch per store, and a
  re-run (with or without the cursor) finishes the job.
* Dry run: the same walk with no writes, reporting counts only.
* Legal holds: rows of a held runtime session, execution or approval are
  skipped and counted.
* Nothing here logs or returns stored content: reports carry ids, counts,
  rule ids and hashes.
"""

from __future__ import annotations

import argparse
import json
import re
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from preloop.config import settings

from preloop.models import models
from preloop.models.crud import audit_chain as crud_audit_chain
from preloop.models.crud import crud_audit_log
from preloop.models.crud import stored_redaction as crud_stored
from preloop.models.crud.session_search_document import content_hash_for
from preloop.models.models.session_search_document import (
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_REDACTED,
)
from preloop.services.audit_chain import (
    ACTION_REDACTION_APPLIED,
    canonical_row,
    hash_row,
    invalidate_chain_status,
    seal_account,
)
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data import reference as reference_module
from preloop.services.sensitive_data.policy_store import load_sensitive_data_config
from preloop.services.sensitive_data.redact import (
    Counts,
    merge_counts,
    redact_structure,
)
from preloop.services.sensitive_data.storage import (
    StorageScope,
    detector_config_for,
    redact_for_storage,
    redact_rules_for,
)

logger = logging.getLogger(__name__)

STORE_AUDIT = "audit_log"
STORE_APPROVALS = "approval_requests"
STORE_ACTIVITY = "runtime_session_activity"
STORE_SEARCH = "session_search_documents"
STORES: Tuple[str, ...] = (STORE_AUDIT, STORE_APPROVALS, STORE_ACTIVITY, STORE_SEARCH)

DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 1000

#: Keys :func:`reference.build_reference_record` writes. A stored dict that
#: carries the reference marker but any other key was not built by the
#: server (the marker is caller-supplied data) and is treated as content.
REFERENCE_RECORD_KEYS = frozenset(
    {
        reference_module.REFERENCE_MARKER,
        "schema",
        "rule_id",
        "tool_name",
        "server_name",
        "principal",
        "decision",
        "kept",
        "kept_result",
        "args_hmac",
        "result_hmac",
        "fingerprint_algo",
        "salt_id",
        "args_bytes",
        "result_bytes",
        "arg_keys",
        "timing_ms",
        "cost",
        "recorded_at",
    }
)
REFERENCE_SUMMARY_PREFIX = "[reference-only:"
#: Exactly what :func:`reference.reference_summary` writes. A string that
#: only starts like one is caller data and goes through redaction.
REFERENCE_SUMMARY_RE = re.compile(
    r"\[reference-only:[^\]\s]+\] \S+ args_hmac=[0-9a-f]{0,16} "
    r"result_hmac=[0-9a-f]{0,16} salt_id=\S+"
)

PENDING_APPROVAL = "pending"


@dataclass
class StoreReport:
    """What one store saw and did."""

    scanned: int = 0
    changed: int = 0
    skipped_legal_hold: int = 0
    skipped_pending: int = 0
    references: int = 0
    counts: Counts = field(default_factory=dict)
    cursor: Optional[Tuple[str, str]] = None
    done: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """API and CLI shape."""
        return {
            "scanned": self.scanned,
            "changed": self.changed,
            "skipped_legal_hold": self.skipped_legal_hold,
            "skipped_pending": self.skipped_pending,
            "references": self.references,
            "counts_by_type": dict(sorted(self.counts.items())),
            "cursor": list(self.cursor) if self.cursor else None,
            "done": self.done,
        }


@dataclass
class RedactionReport:
    """Outcome of one pass over one account."""

    account_id: str
    dry_run: bool
    since: Optional[str] = None
    until: Optional[str] = None
    stores: Dict[str, StoreReport] = field(default_factory=dict)
    rule_ids: List[str] = field(default_factory=list)
    batches: int = 0
    events: List[str] = field(default_factory=list)
    chained_rows_rewritten: int = 0
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        """True when every requested store reached its end without error."""
        return self.error is None and all(item.done for item in self.stores.values())

    def as_dict(self) -> Dict[str, Any]:
        """API and CLI shape. Ids, counts and rule ids only."""
        return {
            "account_id": self.account_id,
            "dry_run": self.dry_run,
            "since": self.since,
            "until": self.until,
            "complete": self.complete,
            "batches": self.batches,
            "rule_ids": sorted(self.rule_ids),
            "redaction_events": list(self.events),
            "chained_rows_rewritten": self.chained_rows_rewritten,
            "error": self.error,
            "stores": {name: item.as_dict() for name, item in self.stores.items()},
        }


@dataclass
class _Outcome:
    """The rewritten value of one field and what produced it."""

    value: Any
    changed: bool
    counts: Counts
    rule_ids: List[str]
    reference: bool = False


def backfill_config(config: SensitiveDataConfig) -> SensitiveDataConfig:
    """The account block as it applies to stored rows.

    Stored audit and approval rows do not record the MCP server a call went
    to, so a rule that names servers could never match them. A rule that also
    names tools applies by tool name here; a rule that names only servers
    still matches nothing. Agent scopes are kept as written.
    """
    copy = config.model_copy(deep=True)
    for rule in list(copy.rules) + list(copy.reference_only):
        if rule.scope.tools and rule.scope.servers:
            rule.scope.servers = []
    return copy


def _is_stored_reference(value: Any) -> bool:
    return (
        reference_module.is_reference_record(value)
        and value.get("schema") == reference_module.RECORD_SCHEMA
        and set(value.keys()) <= REFERENCE_RECORD_KEYS
    )


def _retighten_reference(
    record: Dict[str, Any],
    *,
    config: SensitiveDataConfig,
    scope: StorageScope,
) -> _Outcome:
    """Re-apply the current rule to a stored reference record.

    The fingerprints stay (they are what the record is for). Kept values are
    cut to the paths the current rule still keeps and run through the redact
    rules in scope, which is what a record built today would hold.
    """
    rule = reference_module.reference_rule_for(
        config,
        tool_name=scope.tool_name,
        server_name=scope.server_name,
        managed_agent_id=scope.managed_agent_id,
    )
    # No matching rule any more means the rule was removed or narrowed: a
    # loosening, so kept paths stay. Current redact rules still apply.
    allowed_paths: Optional[Tuple[List[str], List[str]]] = (
        reference_module.split_keep_fields(rule.keep_fields) if rule else None
    )
    updated = dict(record)
    counts: Counts = {}
    rule_ids: List[str] = []
    redact_rules = redact_rules_for(config, scope)
    for index, key in enumerate(("kept", "kept_result")):
        kept = record.get(key)
        if not isinstance(kept, dict):
            continue
        trimmed = dict(kept)
        if rule is not None and allowed_paths is not None:
            allowed = allowed_paths[index]
            trimmed = {path: value for path, value in kept.items() if path in allowed}
            if trimmed != kept:
                rule_ids.append(rule.id)
        if trimmed and redact_rules:
            trimmed, found = redact_structure(
                trimmed, detector_config_for(config, redact_rules)
            )
            if found:
                counts = merge_counts(counts, found)
                rule_ids.extend(item.id for item in redact_rules)
        updated[key] = trimmed
    changed = updated != record
    return _Outcome(updated, changed, counts, sorted(set(rule_ids)) if changed else [])


def apply_rules_to_value(
    account_id: Any,
    value: Any,
    *,
    config: SensitiveDataConfig,
    scope: StorageScope,
) -> _Outcome:
    """The current rules applied to one stored value.

    Args:
        account_id: Account whose salts fingerprint a reference record.
        value: The stored value (dict, list or string).
        config: The account block, already passed through
            :func:`backfill_config`.
        scope: Target, tool and agent the value was stored under.

    Returns:
        The rewritten value, whether it changed, counts by detected type and
        the ids of the rules that changed it.
    """
    if value is None or value == "" or value == {} or value == []:
        return _Outcome(value, False, {}, [])
    if _is_stored_reference(value):
        return _retighten_reference(value, config=config, scope=scope)
    if isinstance(value, str) and REFERENCE_SUMMARY_RE.fullmatch(value):
        return _Outcome(value, False, {}, [])
    redacted, counts, rules = redact_for_storage(
        account_id, value, scope=scope, config=config
    )
    if redacted == value:
        return _Outcome(value, False, {}, [])
    reference = reference_module.is_reference_record(redacted) or (
        isinstance(redacted, str) and redacted.startswith(REFERENCE_SUMMARY_PREFIX)
    )
    if reference:
        rule = reference_module.reference_rule_for(
            config,
            tool_name=scope.tool_name,
            server_name=scope.server_name,
            managed_agent_id=scope.managed_agent_id,
        )
        rule_ids = [rule.id] if rule is not None else []
        if isinstance(redacted, dict):
            redacted = dict(redacted)
        return _Outcome(redacted, True, {}, rule_ids, reference=True)
    return _Outcome(redacted, True, counts, [rule.id for rule in rules])


def _held(holds: Dict[str, set[str]], kind: str, value: Any) -> bool:
    return value is not None and str(value) in holds.get(kind, set())


def _cursor_of(stamp: Any, row_id: Any) -> Tuple[str, str]:
    return (
        stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
        str(row_id),
    )


def _parse_cursor(value: Optional[Sequence[str]]) -> crud_stored.Cursor:
    if not value:
        return None
    if len(value) != 2:
        raise ValueError("a cursor is [timestamp, id]")
    stamp, row_id = value[0], value[1]
    return (datetime.fromisoformat(stamp), uuid.UUID(str(row_id)))


class _Pass:
    """State shared by the store walkers of one run."""

    def __init__(
        self,
        db: Session,
        *,
        account_id: Any,
        config: SensitiveDataConfig,
        report: RedactionReport,
        actor_user_id: Optional[Any],
        since: Optional[datetime],
        until: Optional[datetime],
        batch_size: int,
    ) -> None:
        self.db = db
        self.account_id = account_id
        self.config = config
        self.report = report
        self.actor_user_id = actor_user_id
        self.since = since
        self.until = until
        self.batch_size = batch_size
        self.holds = crud_stored.held_ids(db, account_id=account_id)

    def outcome(self, value: Any, scope: StorageScope, store: StoreReport) -> _Outcome:
        result = apply_rules_to_value(
            self.account_id, value, config=self.config, scope=scope
        )
        if result.changed:
            store.counts = merge_counts(store.counts, result.counts)
            if result.reference:
                store.references += 1
            for rule_id in result.rule_ids:
                if rule_id not in self.report.rule_ids:
                    self.report.rule_ids.append(rule_id)
        return result

    # -- audit_log -----------------------------------------------------------

    def audit_batch(
        self, cursor: crud_stored.Cursor, store: StoreReport
    ) -> Tuple[int, crud_stored.Cursor, Dict[str, Any]]:
        crud_audit_chain.get_state(
            self.db, account_id=self.account_id, for_update=True, create=True
        )
        rows = crud_stored.audit_rows_batch(
            self.db,
            account_id=self.account_id,
            cursor=cursor,
            since=self.since,
            until=self.until,
            limit=self.batch_size,
            exclude_actions=(ACTION_REDACTION_APPLIED,),
        )
        rewritten: List[Tuple[models.AuditLog, Dict[str, Any]]] = []
        for row in rows:
            store.scanned += 1
            details = row.details if isinstance(row.details, dict) else {}
            if (
                _held(self.holds, "runtime_session", details.get("runtime_session_id"))
                or _held(self.holds, "execution", details.get("execution_id"))
                or _held(self.holds, "approval", details.get("approval_id"))
            ):
                store.skipped_legal_hold += 1
                continue
            tool_name = details.get("tool_name")
            agent = details.get("managed_agent_id")
            if not agent and details.get("runtime_principal_type") == "managed_agent":
                agent = details.get("runtime_principal_id")
            new_details = dict(details)
            changed = False
            for key, target in (
                ("tool_args", "tool.args"),
                ("result_preview", "tool.result"),
            ):
                if key not in details:
                    continue
                result = self.outcome(
                    details[key],
                    StorageScope(
                        target=target,
                        tool_name=str(tool_name) if tool_name else None,
                        managed_agent_id=str(agent) if agent else None,
                    ),
                    store,
                )
                if result.changed:
                    new_details[key] = result.value
                    changed = True
            if changed:
                rewritten.append((row, new_details))
        store.changed += len(rewritten)
        listing: List[Dict[str, Any]] = []
        if not self.report.dry_run and rewritten:
            stamp = datetime.now(UTC)
            for row, new_details in rewritten:
                row.details = new_details
                row.redacted_at = stamp
                if row.chain_seq is not None:
                    redacted_hash = hash_row(
                        canonical_row(
                            row,
                            seq=int(row.chain_seq),
                            prev_hash=str(row.prev_hash or ""),
                        )
                    )
                    row.redacted_row_hash = redacted_hash
                    listing.append(
                        {
                            "id": str(row.id),
                            "seq": int(row.chain_seq),
                            "row_hash": row.row_hash,
                            "redacted_row_hash": redacted_hash,
                        }
                    )
                self.db.add(row)
            self.report.chained_rows_rewritten += len(listing)
        last = (rows[-1].timestamp, rows[-1].id) if rows else None
        return (
            len(rows),
            last,
            {"rows": listing, "rewritten": [r for r, _ in rewritten]},
        )

    # -- approval_request ----------------------------------------------------

    def approval_batch(
        self, cursor: crud_stored.Cursor, store: StoreReport
    ) -> Tuple[int, crud_stored.Cursor, Dict[str, Any]]:
        rows = crud_stored.approval_rows_batch(
            self.db,
            account_id=self.account_id,
            cursor=cursor,
            since=self.since,
            until=self.until,
            limit=self.batch_size,
        )
        for row in rows:
            store.scanned += 1
            if (
                row.legal_hold
                or _held(self.holds, "approval", row.id)
                or _held(self.holds, "execution", row.execution_id)
                or _held(self.holds, "runtime_session", row.runtime_session_id)
            ):
                store.skipped_legal_hold += 1
                continue
            if row.status == PENDING_APPROVAL:
                # A pending approval may still be executed from what it
                # stores; rewriting it would change what an approver decides.
                store.skipped_pending += 1
                continue
            agent = str(row.managed_agent_id) if row.managed_agent_id else None
            changed = False
            for attr, target in (
                ("tool_args", "tool.args"),
                ("tool_result", "tool.result"),
            ):
                value = getattr(row, attr)
                result = self.outcome(
                    value,
                    StorageScope(
                        target=target, tool_name=row.tool_name, managed_agent_id=agent
                    ),
                    store,
                )
                if result.changed:
                    changed = True
                    if not self.report.dry_run:
                        setattr(row, attr, result.value)
            if changed:
                store.changed += 1
                if not self.report.dry_run:
                    self.db.add(row)
        return len(rows), ((rows[-1].requested_at, rows[-1].id) if rows else None), {}

    # -- runtime_session_activity --------------------------------------------

    def activity_batch(
        self, cursor: crud_stored.Cursor, store: StoreReport
    ) -> Tuple[int, crud_stored.Cursor, Dict[str, Any]]:
        rows = crud_stored.activity_rows_batch(
            self.db,
            account_id=self.account_id,
            cursor=cursor,
            since=self.since,
            until=self.until,
            limit=self.batch_size,
        )
        for row in rows:
            store.scanned += 1
            if _held(self.holds, "runtime_session", row.runtime_session_id) or _held(
                self.holds, "execution", row.flow_execution_id
            ):
                store.skipped_legal_hold += 1
                continue
            changed = False
            summary = self.outcome(
                row.summary,
                StorageScope(tool_name=row.tool_name, server_name=row.server_name),
                store,
            )
            # Metadata is written through the account-wide rules only.
            metadata = self.outcome(row.metadata_, StorageScope(), store)
            if summary.changed:
                changed = True
                if not self.report.dry_run:
                    row.summary = summary.value
            if metadata.changed:
                changed = True
                if not self.report.dry_run:
                    row.metadata_ = metadata.value
            if changed:
                store.changed += 1
                if not self.report.dry_run:
                    self.db.add(row)
        return len(rows), ((rows[-1].timestamp, rows[-1].id) if rows else None), {}

    # -- session_search_document ---------------------------------------------

    def search_batch(
        self, cursor: crud_stored.Cursor, store: StoreReport
    ) -> Tuple[int, crud_stored.Cursor, Dict[str, Any]]:
        rows = crud_stored.search_documents_batch(
            self.db,
            account_id=self.account_id,
            cursor=cursor,
            since=self.since,
            until=self.until,
            limit=self.batch_size,
        )
        for row in rows:
            store.scanned += 1
            if _held(self.holds, "runtime_session", row.runtime_session_id):
                store.skipped_legal_hold += 1
                continue
            # The index is a mixed store: the writer applies account-wide
            # redact rules only, and so does this pass.
            result = self.outcome(row.content, StorageScope(), store)
            if not result.changed:
                continue
            store.changed += 1
            if self.report.dry_run:
                continue
            row.content = result.value
            row.content_hash = content_hash_for(result.value)
            row.redaction_state = REDACTION_STATE_REDACTED
            # The vector was computed from the original text; it goes too and
            # the chunk is queued to be embedded again from the redacted text.
            row.embedding = None
            row.embedding_model = None
            row.embedded_at = None
            row.embedding_attempts = 0
            row.embedding_state = EMBEDDING_STATE_PENDING
            self.db.add(row)
        return len(rows), ((rows[-1].occurred_at, rows[-1].id) if rows else None), {}


def _snapshot(store: StoreReport) -> Dict[str, Any]:
    return {
        "scanned": store.scanned,
        "changed": store.changed,
        "skipped_legal_hold": store.skipped_legal_hold,
        "skipped_pending": store.skipped_pending,
        "references": store.references,
        "counts": dict(store.counts),
    }


def _restore(store: StoreReport, snapshot: Dict[str, Any]) -> None:
    for key, value in snapshot.items():
        setattr(store, key, value)


def apply_to_stored_rows(
    db: Session,
    *,
    account_id: Any,
    actor_user_id: Optional[Any] = None,
    dry_run: bool = True,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    stores: Optional[Sequence[str]] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: Optional[int] = None,
    cursors: Optional[Dict[str, Sequence[str]]] = None,
    config: Optional[SensitiveDataConfig] = None,
    on_batch: Optional[Callable[[str, int], None]] = None,
) -> RedactionReport:
    """Apply the account's current rules to rows it already stored.

    Args:
        db: Session. The pass commits (or, in a dry run, rolls back) once per
            batch, so it must not be holding other work.
        account_id: Account whose rows and rules are used.
        actor_user_id: Who asked; recorded on every redaction event.
        dry_run: Count only. Nothing is written and no event is recorded.
        since: Only rows at or after this instant.
        until: Only rows before this instant.
        stores: Subset of :data:`STORES`; all of them by default.
        batch_size: Rows per batch and transaction.
        max_batches: Stop after this many batches in total; the report then
            carries per-store cursors to continue from.
        cursors: ``{store: [timestamp, id]}`` from an earlier report.
        config: The account block; read strictly from the account if omitted.
        on_batch: Test and progress hook, called before each batch commits
            with the store name and the batch number.

    Returns:
        Counts, rule ids, the redaction event ids and per-store cursors.

    Raises:
        ValueError: An unknown store name or an empty window.
    """
    selected = list(stores or STORES)
    unknown = [name for name in selected if name not in STORES]
    if unknown:
        raise ValueError(f"unknown store: {', '.join(unknown)}")
    if since is not None and until is not None and since >= until:
        raise ValueError("since must be before until")
    size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    block = config or load_sensitive_data_config(db, account_id, strict=True)
    report = RedactionReport(
        account_id=str(account_id),
        dry_run=dry_run,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
    )
    run = _Pass(
        db,
        account_id=account_id,
        config=backfill_config(block),
        report=report,
        actor_user_id=actor_user_id,
        since=since,
        until=until,
        batch_size=size,
    )
    walkers = {
        STORE_AUDIT: run.audit_batch,
        STORE_APPROVALS: run.approval_batch,
        STORE_ACTIVITY: run.activity_batch,
        STORE_SEARCH: run.search_batch,
    }
    for name in selected:
        store = StoreReport()
        report.stores[name] = store
        start = (cursors or {}).get(name)
        cursor = _parse_cursor(start)
        if cursor is not None:
            store.cursor = (str(start[0]), str(start[1]))  # type: ignore[index]
    for name in selected:
        store = report.stores[name]
        cursor = _parse_cursor(store.cursor)
        while True:
            if max_batches is not None and report.batches >= max_batches:
                break
            before = _snapshot(store)
            rules_before = list(report.rule_ids)
            chained_before = report.chained_rows_rewritten
            event_id: Optional[str] = None
            try:
                count, last, extra = walkers[name](cursor, store)
                changed = store.changed - before["changed"]
                if not dry_run and changed:
                    event = _record_event(
                        db,
                        account_id=account_id,
                        actor_user_id=actor_user_id,
                        store_name=name,
                        changed=changed,
                        counts={
                            key: value - before["counts"].get(key, 0)
                            for key, value in store.counts.items()
                            if value - before["counts"].get(key, 0)
                        },
                        references=store.references - before["references"],
                        rule_ids=report.rule_ids,
                        rows=extra.get("rows", []),
                        since=report.since,
                        until=report.until,
                    )
                    for row in extra.get("rewritten", []):
                        row.redaction_event_id = event.id
                        db.add(row)
                    event_id = str(event.id)
                if on_batch is not None:
                    on_batch(name, report.batches + 1)
                if dry_run:
                    db.rollback()
                else:
                    db.commit()
                    if event_id is not None:
                        _seal_now(db, account_id)
            except Exception as exc:  # noqa: BLE001 - report, keep the cursor
                db.rollback()
                _restore(store, before)
                report.rule_ids = rules_before
                report.chained_rows_rewritten = chained_before
                report.error = f"{name}: {type(exc).__name__}"
                # Type only: an exception message can quote stored content.
                logger.warning(
                    "Stored-row redaction stopped in %s for account %s: %s",
                    name,
                    account_id,
                    type(exc).__name__,
                )
                return report
            report.batches += 1
            if event_id is not None:
                report.events.append(event_id)
            if last is not None:
                cursor = last
                store.cursor = _cursor_of(last[0], last[1])
            if count < size:
                store.done = True
                break
    logger.info(
        "Stored-row redaction for account %s: dry_run=%s batches=%s changed=%s",
        account_id,
        dry_run,
        report.batches,
        {name: item.changed for name, item in report.stores.items()},
    )
    return report


def _seal_now(db: Session, account_id: Any) -> None:
    """Chain the redaction event now instead of after the seal lag.

    The verifier only accepts a rewrite whose event is sealed, so the window
    in which a rewritten row reads as unanchored is this call, not a sealing
    interval. A failure is logged; the next sealer pass closes it.
    """
    if not settings.audit_chain_enabled:
        invalidate_chain_status(account_id)
        return
    try:
        seal_account(db, account_id=account_id, lag=timedelta(0))
    except Exception:  # noqa: BLE001 - the sealer pass retries
        db.rollback()
        logger.warning("Sealing the redaction event failed for %s", account_id)
    invalidate_chain_status(account_id)


def _record_event(
    db: Session,
    *,
    account_id: Any,
    actor_user_id: Optional[Any],
    store_name: str,
    changed: int,
    counts: Counts,
    references: int,
    rule_ids: Sequence[str],
    rows: List[Dict[str, Any]],
    since: Optional[str],
    until: Optional[str],
) -> models.AuditLog:
    """Append the ``redaction_applied`` row for one batch (no content)."""
    return crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=actor_user_id,
        action=ACTION_REDACTION_APPLIED,
        resource_type="sensitive_data",
        resource_id=store_name,
        status="success",
        details={
            "store": store_name,
            "changed": changed,
            "references": references,
            "counts_by_type": dict(sorted(counts.items())),
            "rule_ids": sorted(set(rule_ids)),
            "since": since,
            "until": until,
            "applied_at": datetime.now(UTC).isoformat(),
            # Sealed rows rewritten by this batch, with the hash the chain
            # committed to and the hash of what the row holds now.
            "rows": rows,
        },
        commit=False,
    )


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Operator entry point: ``python -m preloop.services.sensitive_data.stored_rows``.

    Dry run unless ``--apply`` is given. Prints the JSON report (counts, rule
    ids, event ids, cursors; never content). Exit 0 when complete, 3 when
    stopped early (``--max-batches`` or an error), with cursors to resume.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--since", help="ISO 8601; rows at or after")
    parser.add_argument("--until", help="ISO 8601; rows before")
    parser.add_argument("--store", action="append", choices=STORES)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument(
        "--resume", help="JSON report (or its stores object) from an earlier run"
    )
    args = parser.parse_args(argv)

    cursors: Dict[str, Sequence[str]] = {}
    if args.resume:
        with open(args.resume, encoding="utf-8") as handle:
            previous = json.load(handle)
        for name, item in (previous.get("stores") or {}).items():
            if isinstance(item, dict) and item.get("cursor") and not item.get("done"):
                cursors[name] = item["cursor"]

    from preloop.models.db.session import get_session_factory

    db = get_session_factory()()
    try:
        report = apply_to_stored_rows(
            db,
            account_id=args.account_id,
            dry_run=not args.apply,
            since=_parse_time(args.since),
            until=_parse_time(args.until),
            stores=args.store,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            cursors=cursors,
        )
    finally:
        db.close()
    json.dump(report.as_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
