"""The per-account hash chain over audit rows, and the sealer that builds it.

## What the chain is

Every audit row gets a position in its account's chain: a ``chain_seq``, the
``prev_hash`` of the row before it, and a ``row_hash`` over a canonical
serialisation of the row *including* that ``prev_hash``. Changing any field of
a sealed row, deleting one, or inserting one between two others changes a hash
that the next row committed to, and the walk stops at the first link that does
not hold.

The canonical serialisation is
:func:`preloop.cra.evidence_pack.canonical_manifest_json`, the same sorted-key
compact JSON the evidence pack manifest (#511) and the period export manifest
(#559) already hash. One canonical form in the product, not three.

## Why sealing is a background job

The obvious implementation chains inside ``crud_audit_log.log_action``. It is
the wrong one here. It needs the account's chain head under a lock held until
the caller's transaction commits, and ``model_gateway_request`` writes an audit
row per gateway call: that would serialise the highest throughput path in the
product on one row per account, and would let a chain failure fail the action
being audited. Recording the event has to be more reliable than sequencing it.

So a bounded sweeper seals rows off the request path, in ``(timestamp, id)``
order, in batches, the same shape as #559's ``RetentionPurgeSweeper``. The
cost is stated rather than hidden: sealing lags writes by up to one pass, and
a row deleted before it was sealed leaves no gap to find. That is in the docs
next to everything else this does not prove.

## Sequence allocation

``audit_chain_state`` is one row per account, taken ``FOR UPDATE`` for the
duration of a batch. Two sealer passes (two API replicas, say) cannot both
take seq N, and a pass whose transaction rolls back gives the numbers back
instead of leaving a hole that looks like a deletion.

## Checkpoints

Every ``AUDIT_CHAIN_CHECKPOINT_INTERVAL`` sealed rows the sealer writes a
checkpoint: the head hash at that sequence, signed with the account's Ed25519
key. This is the part that makes a rewrite detectable instead of merely
laborious. A server that can write the database can recompute an entire
self-consistent chain; it cannot produce a checkpoint from last month whose
signature still verifies, unless it also holds the key. Which, on a
compromised platform, it does. The anchor is only worth what the copy the
customer keeps off the platform is worth.

## The purge

Retention deletes the oldest audit rows on purpose (#559). That is not
tampering and must not look like it, so the purge raises
``pruned_below_seq`` on the chain state and the verifier reports ``pruned``
under that floor instead of a break.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models.crud import audit_chain as crud_audit_chain
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.audit_chain import (
    GENESIS_HASH,
    AuditChainCheckpoint,
    AuditChainState,
)
from preloop.models.models.audit_log import AuditLog
from preloop.services import record_signing
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)

logger = logging.getLogger(__name__)

ROW_SCHEMA = "preloop.audit.chain_row/v1"
CHECKPOINT_SCHEMA = record_signing.PAYLOAD_AUDIT_CHECKPOINT
ROW_DOMAIN = b"preloop.audit.chain/v1\n"

#: Verification outcomes.
STATUS_OK = "ok"
STATUS_BROKEN = "broken"
STATUS_EMPTY = "empty"

#: Break kinds, in the order the walk can meet them.
BREAK_MISSING_ROW = "missing_row"
BREAK_PREV_HASH = "prev_hash_mismatch"
BREAK_ROW_HASH = "row_hash_mismatch"
BREAK_DUPLICATE_SEQ = "duplicate_seq"
BREAK_REDACTION_UNANCHORED = "redaction_unanchored"

#: The audit action a stored-row redaction appends per batch (#2133). Its
#: ``details["rows"]`` lists every sealed row it rewrote with the original and
#: the redacted hash; the verifier accepts a rewritten row only against it.
ACTION_REDACTION_APPLIED = "redaction_applied"


def _iso(value: Any) -> Optional[str]:
    """ISO 8601 for a datetime, passthrough for a string, None for None."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _canonical_timestamp(value: Any) -> Optional[str]:
    """The one timestamp spelling a row hash is allowed to see.

    ``audit_log.timestamp`` is a naive UTC column, but callers hand
    ``log_action`` an aware ``datetime``, so the same instant is
    ``...+00:00`` in the session that wrote it and naive once it comes back
    from Postgres. Hashing whichever one was in memory would make a row's
    hash depend on who read it. Aware values are converted to UTC and the
    offset dropped, which is also exactly what
    ``preloop.services.retention_export._iso`` produces for a row read from
    the database, so an offline verifier rehashing an exported bundle gets
    the same bytes.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # Drop the offset without shifting the clock. ``audit_log.timestamp``
        # is a naive column and the driver writes an aware value as the local
        # wall clock, so the number Postgres keeps is the number here; a
        # conversion to UTC would hash something the database does not hold.
        return value.replace(tzinfo=None).isoformat()
    return _iso(value)


def canonical_row(
    row: Any, *, seq: int, prev_hash: str, account_id: Optional[Any] = None
) -> dict[str, Any]:
    """The exact fields a row hash commits to.

    Every audited field is in here. A column that a future change adds and
    forgets to include would be a field an attacker could edit without
    breaking the chain, which is why this is one explicit dict and not
    ``row.to_dict()``: a silent addition is safer than a silent omission.
    """
    account = account_id if account_id is not None else getattr(row, "account_id", None)
    return {
        "schema": ROW_SCHEMA,
        "account_id": str(account) if account is not None else None,
        "id": str(getattr(row, "id", "")),
        "seq": int(seq),
        "prev_hash": prev_hash,
        "timestamp": _canonical_timestamp(getattr(row, "timestamp", None)),
        "user_id": (
            str(row.user_id) if getattr(row, "user_id", None) is not None else None
        ),
        "action": getattr(row, "action", None),
        "resource_type": getattr(row, "resource_type", None),
        "resource_id": getattr(row, "resource_id", None),
        "status": getattr(row, "status", None),
        "ip_address": getattr(row, "ip_address", None),
        "user_agent": getattr(row, "user_agent", None),
        "details": getattr(row, "details", None),
    }


def hash_row(payload: dict[str, Any]) -> str:
    """Domain separated sha256 over the canonical row payload."""
    return hashlib.sha256(ROW_DOMAIN + canonical_manifest_json(payload)).hexdigest()


def checkpoint_payload(
    *, account_id: Any, seq: int, chain_hash: str, row_count: int, taken_at: Any
) -> dict[str, Any]:
    """The canonical form of a checkpoint, which is what gets signed.

    ``checkpointed_at`` goes through ``format_signed_at``: UTC, second
    resolution, one spelling. The column is timezone aware and the driver may
    hand it back in the server's zone, and a digest that changed depending on
    which zone the reader was in would invalidate its own signature.
    """
    return {
        "schema": CHECKPOINT_SCHEMA,
        "account_id": str(account_id),
        "seq": int(seq),
        "chain_hash": chain_hash,
        "row_count": int(row_count),
        "checkpointed_at": record_signing.format_signed_at(taken_at),
    }


def checkpoint_interval() -> int:
    """Rows between checkpoints, at least one."""
    return max(1, int(settings.audit_chain_checkpoint_interval))


def seal_lag() -> timedelta:
    """How far behind now the sealer stays.

    A transaction that started before the last pass can still commit an audit
    row with an earlier timestamp than rows already sealed. Waiting a lag
    keeps that rare rather than routine; when it happens anyway the row is
    still sealed, at the end of the chain, and the walk reports it as
    out of order rather than as a break.
    """
    return timedelta(seconds=max(0, int(settings.audit_chain_seal_lag_seconds)))


@dataclass
class SealResult:
    """What one sealing pass did to one account."""

    account_id: str
    sealed: int = 0
    checkpoints: int = 0
    batches: int = 0
    #: True when rows were still waiting when the pass stopped.
    more_remaining: bool = False
    out_of_order: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Log shape."""
        return {
            "account_id": self.account_id,
            "sealed": self.sealed,
            "checkpoints": self.checkpoints,
            "batches": self.batches,
            "more_remaining": self.more_remaining,
            "out_of_order": self.out_of_order,
        }


@dataclass
class SweepResult:
    """Totals for one pass across every account it reached."""

    accounts: int = 0
    sealed: int = 0
    checkpoints: int = 0
    budget_exhausted: bool = False
    skipped_reason: Optional[str] = None
    results: list[SealResult] = field(default_factory=list)

    def record(self, result: SealResult) -> None:
        """Fold one account's result into the totals."""
        self.results.append(result)
        self.sealed += result.sealed
        self.checkpoints += result.checkpoints
        if result.more_remaining:
            self.budget_exhausted = True

    def as_dict(self) -> dict[str, Any]:
        """Log shape."""
        return {
            "accounts": self.accounts,
            "sealed": self.sealed,
            "checkpoints": self.checkpoints,
            "budget_exhausted": self.budget_exhausted,
            "skipped_reason": self.skipped_reason,
        }


def get_state(
    db: Session, *, account_id: Any, for_update: bool = False, create: bool = True
) -> Optional[AuditChainState]:
    """Return (and optionally create) the chain head row for an account."""
    return crud_audit_chain.get_state(
        db, account_id=account_id, for_update=for_update, create=create
    )


def note_pruned(db: Session, *, account_id: Any, up_to_seq: int, now: Any) -> None:
    """Raise the floor after a retention purge removed audit rows.

    Deleting the oldest rows under a stated retention is the purge doing its
    job. Without this the next verification would call it a break, and a
    tamper-evidence feature that cries wolf every night teaches people to
    ignore it.
    """
    if up_to_seq <= 0:
        return
    state = get_state(db, account_id=account_id, for_update=True)
    if state is None:
        return
    if up_to_seq > int(state.pruned_below_seq or 0):
        state.pruned_below_seq = int(up_to_seq)
        state.pruned_at = now
        db.add(state)
        invalidate_chain_status(account_id)


def _write_checkpoint(
    db: Session,
    *,
    account_id: Any,
    seq: int,
    chain_hash: str,
    row_count: int,
    taken_at: datetime,
) -> AuditChainCheckpoint:
    """Write one checkpoint, signed when the account has a usable key."""
    payload = checkpoint_payload(
        account_id=account_id,
        seq=seq,
        chain_hash=chain_hash,
        row_count=row_count,
        taken_at=taken_at,
    )
    key_id: Optional[str] = None
    signature: Optional[str] = None
    document = record_signing.sign_manifest(
        db,
        account_id=account_id,
        payload_type=CHECKPOINT_SCHEMA,
        manifest=payload,
        signed_at=taken_at,
        commit=False,
    )
    if document is not None:
        key_id = str(document.get("key_id"))
        signature = str(document.get("signature"))
    checkpoint = AuditChainCheckpoint(
        account_id=account_id,
        seq=seq,
        chain_hash=chain_hash,
        row_count=row_count,
        checkpointed_at=taken_at,
        signing_key_id=key_id,
        signature=signature,
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint


def seal_account(
    db: Session,
    *,
    account_id: Any,
    batch_size: Optional[int] = None,
    max_batches: Optional[int] = None,
    now: Optional[datetime] = None,
    lag: Optional[timedelta] = None,
    deadline: Optional[float] = None,
) -> SealResult:
    """Seal this account's unsealed audit rows, in batches.

    One transaction per batch, so a backlog never holds a long lock on the
    table the request path writes to. The chain head is taken ``FOR UPDATE``
    inside each batch, which is what makes two sealers safe.
    """
    stamp = now or datetime.now(UTC)
    window = stamp - (seal_lag() if lag is None else lag)
    size = max(1, int(batch_size or settings.audit_chain_seal_batch_size))
    ceiling = max(1, int(max_batches or settings.audit_chain_seal_max_batches))
    interval = checkpoint_interval()
    result = SealResult(account_id=str(account_id))

    for _ in range(ceiling):
        if deadline is not None and time.monotonic() >= deadline:
            result.more_remaining = True
            break
        state = get_state(db, account_id=account_id, for_update=True)
        assert state is not None  # create=True
        # Naive comparison: audit_log.timestamp is a naive UTC column.
        cutoff = window.replace(tzinfo=None)
        rows = list(
            db.execute(
                select(AuditLog)
                .where(
                    AuditLog.account_id == account_id,
                    AuditLog.chain_seq.is_(None),
                    AuditLog.timestamp <= cutoff,
                )
                .order_by(AuditLog.timestamp, AuditLog.id)
                .limit(size)
                # Hash what the database holds, never a copy of the row that
                # happens to be in this session's identity map. The two can
                # differ: a caller hands ``log_action`` an aware datetime and
                # the naive column gives back the local wall clock. A hash
                # over the in-memory value would verify in the session that
                # wrote it and nowhere else.
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        if not rows:
            db.commit()
            break
        seq = int(state.last_seq or 0)
        prev = str(state.last_hash or GENESIS_HASH)
        last_timestamp = state.last_sealed_at
        pending_checkpoints: list[tuple[int, str, int]] = []
        since_checkpoint = seq % interval
        for row in rows:
            seq += 1
            payload = canonical_row(row, seq=seq, prev_hash=prev)
            digest = hash_row(payload)
            row.chain_seq = seq
            row.prev_hash = prev
            row.row_hash = digest
            row.sealed_at = stamp
            db.add(row)
            prev = digest
            since_checkpoint += 1
            if since_checkpoint >= interval:
                pending_checkpoints.append((seq, digest, since_checkpoint))
                since_checkpoint = 0
        state.last_seq = seq
        state.last_hash = prev
        state.last_sealed_at = stamp
        db.add(state)
        db.flush()
        for cp_seq, cp_hash, cp_count in pending_checkpoints:
            _write_checkpoint(
                db,
                account_id=account_id,
                seq=cp_seq,
                chain_hash=cp_hash,
                row_count=cp_count,
                taken_at=stamp,
            )
            result.checkpoints += 1
        db.commit()
        # After the commit: a status read that starts now sees the new head.
        invalidate_chain_status(account_id)
        result.sealed += len(rows)
        result.batches += 1
        if last_timestamp is not None:
            first = rows[0].timestamp
            if first is not None and _canonical_timestamp(first) < _canonical_timestamp(
                last_timestamp
            ):
                result.out_of_order += 1
        if len(rows) == size:
            result.more_remaining = True
        else:
            result.more_remaining = False
            break
    return result


def run_seal_pass(
    db: Session,
    *,
    now: Optional[datetime] = None,
    account_ids: Optional[Sequence[Any]] = None,
    ignore_enabled: bool = False,
    lag: Optional[timedelta] = None,
) -> SweepResult:
    """One bounded pass over every active account.

    Returns totals rather than raising: the caller is a background loop, and
    an exception per pass would be noise on top of per account logging.
    """
    summary = SweepResult()
    if not (ignore_enabled or settings.audit_chain_enabled):
        summary.skipped_reason = "disabled"
        return summary
    stamp = now or datetime.now(UTC)
    deadline = time.monotonic() + max(1, int(settings.audit_chain_seal_max_seconds))
    stmt = select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
    if account_ids:
        stmt = stmt.where(Account.id.in_(list(account_ids)))
    for account in db.execute(stmt).scalars().all():
        if time.monotonic() >= deadline:
            summary.budget_exhausted = True
            break
        summary.accounts += 1
        try:
            summary.record(
                seal_account(
                    db, account_id=account.id, now=stamp, lag=lag, deadline=deadline
                )
            )
        except Exception:
            db.rollback()
            logger.error(
                "Audit chain sealing failed for account %s", account.id, exc_info=True
            )
    if summary.sealed:
        logger.info("Audit chain seal pass: %s", summary.as_dict())
    return summary


def _sealed_row_count(db: Session, *, account_id: Any, state: Any) -> int:
    """Sealed rows still stored, without counting the whole sealed history.

    The sealer hands out ``chain_seq`` 1..``last_seq`` without gaps, and a
    retention purge raises ``pruned_below_seq`` to the highest position it
    deleted, so every purged row sits at or under the floor. Above the floor
    the count is therefore ``last_seq - pruned_below_seq``; only the rows at
    or under it (the survivors of a purge that did not delete a clean prefix,
    normally none) are counted, through ``ix_audit_log_account_chain_seq``.
    Counting every sealed row read 1.2M index entries for the founder account
    on prod: 1.7 s cold (#1693). A row missing above the floor is tampering,
    which is the verifier's job to report, not this figure's.
    """
    if state is None:
        return 0
    head = int(state.last_seq or 0)
    floor = min(int(state.pruned_below_seq or 0), head)
    if floor <= 0:
        return head
    survivors = int(
        db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.account_id == account_id,
                AuditLog.chain_seq.is_not(None),
                AuditLog.chain_seq <= floor,
            )
        ).scalar_one()
        or 0
    )
    return head - floor + survivors


def chain_status(db: Session, *, account_id: Any) -> dict[str, Any]:
    """Head, floor, pending count and the newest checkpoint."""
    state = get_state(db, account_id=account_id, create=False)
    pending = int(
        db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.account_id == account_id, AuditLog.chain_seq.is_(None))
        ).scalar_one()
        or 0
    )
    sealed = _sealed_row_count(db, account_id=account_id, state=state)
    latest = db.execute(
        select(AuditChainCheckpoint)
        .where(AuditChainCheckpoint.account_id == account_id)
        .order_by(AuditChainCheckpoint.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    active_key = record_signing.get_active_key(db, account_id=account_id)
    return {
        "enabled": bool(settings.audit_chain_enabled),
        "head_seq": int(state.last_seq) if state else 0,
        "head_hash": str(state.last_hash) if state else GENESIS_HASH,
        "last_sealed_at": _iso(state.last_sealed_at) if state else None,
        "pruned_below_seq": int(state.pruned_below_seq) if state else 0,
        "sealed_rows": sealed,
        "unsealed_rows": pending,
        "seal_lag_seconds": int(settings.audit_chain_seal_lag_seconds),
        "checkpoint_interval": checkpoint_interval(),
        "latest_checkpoint": checkpoint_summary(latest) if latest else None,
        "active_key_id": active_key.key_id if active_key else None,
    }


#: How long a computed chain status is served from memory. The two row counts
#: behind it scan every audit row of the account (3.5 to 4.1 s on prod, #1693),
#: and the seal pass only moves the head once a minute, so a short-lived copy
#: is as current as the page needs.
CHAIN_STATUS_TTL_SECONDS = 30.0

_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_status_cache_lock = threading.Lock()
#: Bumped by every invalidation. A fill that started before one is dropped, so
#: a slow read that overlaps a seal cannot store the pre-seal snapshot.
_status_generation = 0


def cached_chain_status(
    db: Session, *, account_id: Any, ttl: float = CHAIN_STATUS_TTL_SECONDS
) -> dict[str, Any]:
    """Chain status, served from a per-process cache for ``ttl`` seconds.

    Args:
        db: Database session used when the cache misses.
        account_id: Account whose chain is described.
        ttl: Seconds a computed status stays fresh.

    Returns:
        The same payload as :func:`chain_status`.
    """
    key = str(account_id)
    now = time.monotonic()
    with _status_cache_lock:
        hit = _status_cache.get(key)
        generation = _status_generation
    if hit is not None and now - hit[0] < ttl:
        return dict(hit[1])
    status = chain_status(db, account_id=account_id)
    with _status_cache_lock:
        if _status_generation == generation:
            _status_cache[key] = (now, status)
    return dict(status)


def invalidate_chain_status(account_id: Any | None = None) -> None:
    """Drop cached chain status for one account, or for all when ``None``.

    Args:
        account_id: Account to forget; ``None`` clears the whole cache.
    """
    global _status_generation
    with _status_cache_lock:
        _status_generation += 1
        if account_id is None:
            _status_cache.clear()
        else:
            _status_cache.pop(str(account_id), None)


def checkpoint_summary(checkpoint: AuditChainCheckpoint) -> dict[str, Any]:
    """One checkpoint as an API payload, signature included."""
    payload = checkpoint_payload(
        account_id=checkpoint.account_id,
        seq=checkpoint.seq,
        chain_hash=checkpoint.chain_hash,
        row_count=checkpoint.row_count,
        taken_at=checkpoint.checkpointed_at,
    )
    digest = record_signing.digest_of(payload)
    signed_at = record_signing.format_signed_at(checkpoint.checkpointed_at)
    return {
        "seq": int(checkpoint.seq),
        "chain_hash": checkpoint.chain_hash,
        "row_count": int(checkpoint.row_count),
        "checkpointed_at": signed_at,
        "signing_key_id": checkpoint.signing_key_id,
        "signature": checkpoint.signature,
        "signed_payload": payload,
        "digest": digest,
        # The detached document a client verifies, assembled here so nobody
        # has to guess how the signed bytes were spelled.
        "signature_document": (
            {
                "schema": record_signing.SIGNATURE_SCHEMA,
                "algorithm": "ed25519",
                "key_id": checkpoint.signing_key_id,
                "payload_type": CHECKPOINT_SCHEMA,
                "digest": digest,
                "signed_at": signed_at,
                "signature": checkpoint.signature,
            }
            if checkpoint.signature
            else None
        ),
    }


def list_checkpoints(
    db: Session, *, account_id: Any, limit: int = 50, after_seq: int = 0
) -> list[dict[str, Any]]:
    """Checkpoints in sequence order, so a client can keep its own copies."""
    rows = (
        db.execute(
            select(AuditChainCheckpoint)
            .where(
                AuditChainCheckpoint.account_id == account_id,
                AuditChainCheckpoint.seq > after_seq,
            )
            .order_by(AuditChainCheckpoint.seq)
            .limit(max(1, min(int(limit), 1000)))
        )
        .scalars()
        .all()
    )
    return [checkpoint_summary(row) for row in rows]


def chain_segment(
    db: Session, *, account_id: Any, after_seq: int = 0, limit: int = 500
) -> dict[str, Any]:
    """Canonical payloads plus stored hashes, for a client side walk.

    A verification endpoint that only reports its own verdict is worth
    exactly the trust the caller already places in the server. This returns
    the material so the CLI can recompute every hash itself and disagree.
    """
    count = max(1, min(int(limit), 1000))
    rows = (
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.account_id == account_id,
                AuditLog.chain_seq.is_not(None),
                AuditLog.chain_seq > after_seq,
            )
            .order_by(AuditLog.chain_seq)
            .limit(count)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    state = get_state(db, account_id=account_id, create=False)
    entries = [
        {
            "seq": int(row.chain_seq),
            "row_id": str(row.id),
            "prev_hash": row.prev_hash,
            "row_hash": row.row_hash,
            "redacted_row_hash": row.redacted_row_hash,
            "redaction_event_id": (
                str(row.redaction_event_id) if row.redaction_event_id else None
            ),
            "payload": canonical_row(
                row, seq=int(row.chain_seq), prev_hash=str(row.prev_hash or "")
            ),
        }
        for row in rows
    ]
    try:
        from preloop.services.sensitive_data.reference import salt_ids

        reference_salt_ids = salt_ids(account_id, db)
    except Exception:  # noqa: BLE001 - the export must not depend on salts
        reference_salt_ids = []
    return {
        "account_id": str(account_id),
        "row_domain": ROW_DOMAIN.decode("utf-8"),
        # Reference records in details carry fingerprints in args_hmac and
        # result_hmac, keyed by one of these salts. The ids are listed; the
        # salts never are.
        "reference_salt_ids": reference_salt_ids,
        "after_seq": int(after_seq),
        "head_seq": int(state.last_seq) if state else 0,
        "pruned_below_seq": int(state.pruned_below_seq) if state else 0,
        "genesis_hash": GENESIS_HASH,
        "entries": entries,
        "has_more": len(entries) == count,
        "note": (
            "Recompute row_hash as sha256(row_domain + canonical JSON of "
            "payload) with sorted keys and no insignificant whitespace, and "
            "check that each prev_hash equals the previous row_hash. A row "
            "rewritten by a redaction hashes to redacted_row_hash instead, "
            "which the redaction_applied row named by redaction_event_id "
            "must list. A verdict from this server is not a verification."
        ),
    }


def verify_chain(
    db: Session,
    *,
    account_id: Any,
    start_seq: Optional[int] = None,
    end_seq: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> dict[str, Any]:
    """Walk the chain and report the first break, if any.

    The walk is over a range because that is the only claim the chain can
    honestly support: rows below ``pruned_below_seq`` were removed by the
    retention purge under a stated policy and cannot be recovered or verified,
    and rows written since the last seal are not in the chain yet.
    """
    state = get_state(db, account_id=account_id, create=False)
    head = int(state.last_seq) if state else 0
    floor = int(state.pruned_below_seq) if state else 0
    begin = max(int(start_seq) if start_seq else 1, floor + 1)
    finish = min(int(end_seq) if end_seq else head, head)
    limit = max(1, int(max_rows or settings.audit_chain_verify_max_rows))

    report: dict[str, Any] = {
        "account_id": str(account_id),
        "status": STATUS_EMPTY,
        "checked_rows": 0,
        "start_seq": begin,
        "end_seq": finish,
        "head_seq": head,
        "pruned_below_seq": floor,
        "unsealed_rows": int(
            db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.account_id == account_id, AuditLog.chain_seq.is_(None))
            ).scalar_one()
            or 0
        ),
        "truncated": False,
        "out_of_order_rows": 0,
        "first_break": None,
        "checkpoints_verified": 0,
        "checkpoint_failures": [],
        "redacted_rows": 0,
    }
    if head == 0 or finish < begin:
        return report

    if finish - begin + 1 > limit:
        finish = begin + limit - 1
        report["truncated"] = True
        report["end_seq"] = finish

    rows = (
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.account_id == account_id,
                AuditLog.chain_seq >= begin,
                AuditLog.chain_seq <= finish,
            )
            .order_by(AuditLog.chain_seq)
            # Verify against persisted values, for the same reason the sealer
            # hashes them: a stale in-memory row would report a break that is
            # not in the database, or hide one that is.
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )

    # The expected prev_hash for the first row in the range: the row before
    # it when there is one, the genesis hash when the range starts at 1.
    if begin <= 1:
        expected_prev = GENESIS_HASH
    else:
        anchor = db.execute(
            select(AuditLog.row_hash).where(
                AuditLog.account_id == account_id, AuditLog.chain_seq == begin - 1
            )
        ).scalar_one_or_none()
        expected_prev = str(anchor) if anchor else None

    checked = 0
    previous_timestamp: Optional[str] = None
    expected_seq = begin
    tombstones: dict[str, dict[str, str]] = {}
    for row in rows:
        seq = int(row.chain_seq)
        if seq < expected_seq:
            report["status"] = STATUS_BROKEN
            report["first_break"] = {
                "kind": BREAK_DUPLICATE_SEQ,
                "seq": seq,
                "row_id": str(row.id),
                "detail": "two rows share one chain sequence",
            }
            break
        if seq > expected_seq:
            report["status"] = STATUS_BROKEN
            report["first_break"] = {
                "kind": BREAK_MISSING_ROW,
                "seq": expected_seq,
                "row_id": None,
                "detail": (
                    f"sequence {expected_seq} is not in the log; the chain "
                    f"jumps to {seq}. A row was deleted after it was sealed, "
                    "or it is outside this account."
                ),
            }
            break
        stored_prev = str(row.prev_hash or "")
        if expected_prev is not None and stored_prev != expected_prev:
            report["status"] = STATUS_BROKEN
            report["first_break"] = {
                "kind": BREAK_PREV_HASH,
                "seq": seq,
                "row_id": str(row.id),
                "detail": (
                    "prev_hash does not match the row_hash of the row before "
                    "it: the previous row was altered or replaced"
                ),
                "expected": expected_prev,
                "found": stored_prev,
            }
            break
        recomputed = hash_row(canonical_row(row, seq=seq, prev_hash=stored_prev))
        if recomputed != str(row.row_hash or "") and row.redacted_row_hash:
            if redaction_anchored(
                db,
                account_id=account_id,
                row=row,
                recomputed=recomputed,
                cache=tombstones,
            ):
                report["redacted_rows"] = int(report["redacted_rows"]) + 1
            else:
                report["status"] = STATUS_BROKEN
                report["first_break"] = {
                    "kind": BREAK_REDACTION_UNANCHORED,
                    "seq": seq,
                    "row_id": str(row.id),
                    "detail": (
                        "the row was rewritten after sealing, but no "
                        "redaction_applied row in this account lists this "
                        "row with the hash it has now"
                    ),
                    "expected": str(row.redacted_row_hash or ""),
                    "found": recomputed,
                }
                break
        elif recomputed != str(row.row_hash or ""):
            report["status"] = STATUS_BROKEN
            report["first_break"] = {
                "kind": BREAK_ROW_HASH,
                "seq": seq,
                "row_id": str(row.id),
                "detail": (
                    "the row no longer hashes to its stored row_hash: a "
                    "field of this row was changed after it was sealed"
                ),
                "expected": str(row.row_hash or ""),
                "found": recomputed,
            }
            break
        stamp = _canonical_timestamp(row.timestamp)
        if previous_timestamp is not None and stamp is not None:
            if stamp < previous_timestamp:
                report["out_of_order_rows"] = int(report["out_of_order_rows"]) + 1
        previous_timestamp = stamp
        expected_prev = str(row.row_hash or "")
        expected_seq = seq + 1
        checked += 1

    report["checked_rows"] = checked
    if report["first_break"] is None:
        if checked < finish - begin + 1:
            report["status"] = STATUS_BROKEN
            report["first_break"] = {
                "kind": BREAK_MISSING_ROW,
                "seq": expected_seq,
                "row_id": None,
                "detail": (
                    f"the log ends at sequence {expected_seq - 1} but the "
                    f"chain head is {head}: rows were deleted from the tail"
                ),
            }
        elif checked:
            report["status"] = STATUS_OK

    _verify_checkpoints(
        db, account_id=account_id, report=report, begin=begin, end=finish
    )
    return report


def redaction_listing(details: Any) -> dict[str, str]:
    """``{row_id: redacted_row_hash}`` from a ``redaction_applied`` row."""
    listing: dict[str, str] = {}
    entries = details.get("rows") if isinstance(details, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        row_id = entry.get("id")
        redacted = entry.get("redacted_row_hash")
        if row_id and redacted:
            listing[str(row_id)] = str(redacted)
    return listing


def redaction_anchored(
    db: Session,
    *,
    account_id: Any,
    row: Any,
    recomputed: str,
    cache: Optional[dict[str, dict[str, str]]] = None,
) -> bool:
    """True when a rewritten row is vouched for by its redaction event.

    The row must hash to its recorded ``redacted_row_hash``, and the
    ``redaction_applied`` row it names must belong to the same account and
    list this row id with exactly that hash. The event row is an ordinary
    audit row: once sealed, editing it breaks the chain like any other row.
    """
    if not row.redacted_row_hash or recomputed != str(row.redacted_row_hash):
        return False
    event_id = getattr(row, "redaction_event_id", None)
    if event_id is None:
        return False
    key = str(event_id)
    listing = cache.get(key) if cache is not None else None
    if listing is None:
        event = crud_audit_chain.get_redaction_event(
            db,
            account_id=account_id,
            event_id=event_id,
            action=ACTION_REDACTION_APPLIED,
        )
        listing = redaction_listing(event.details) if event is not None else {}
        if cache is not None:
            cache[key] = listing
    return listing.get(str(row.id)) == recomputed


def _verify_checkpoints(
    db: Session, *, account_id: Any, report: dict[str, Any], begin: int, end: int
) -> None:
    """Check every checkpoint in range against the chain and its signature."""
    checkpoints = (
        db.execute(
            select(AuditChainCheckpoint)
            .where(
                AuditChainCheckpoint.account_id == account_id,
                AuditChainCheckpoint.seq >= begin,
                AuditChainCheckpoint.seq <= end,
            )
            .order_by(AuditChainCheckpoint.seq)
        )
        .scalars()
        .all()
    )
    failures: list[dict[str, Any]] = []
    verified = 0
    for checkpoint in checkpoints:
        stored = db.execute(
            select(AuditLog.row_hash).where(
                AuditLog.account_id == account_id,
                AuditLog.chain_seq == checkpoint.seq,
            )
        ).scalar_one_or_none()
        if stored != checkpoint.chain_hash:
            failures.append(
                {
                    "seq": int(checkpoint.seq),
                    "kind": "checkpoint_hash_mismatch",
                    "detail": (
                        "the chain no longer hashes to the value this "
                        "checkpoint anchored"
                    ),
                }
            )
            continue
        if checkpoint.signature and checkpoint.signing_key_id:
            key = record_signing.get_key_by_id(
                db, account_id=account_id, key_id=checkpoint.signing_key_id
            )
            document = checkpoint_summary(checkpoint)["signature_document"]
            if key is None or not record_signing.verify_signature_document(
                document, public_key=key.public_key
            ):
                failures.append(
                    {
                        "seq": int(checkpoint.seq),
                        "kind": "checkpoint_signature_invalid",
                        "detail": "the checkpoint signature does not verify",
                    }
                )
                continue
        verified += 1
    report["checkpoints_verified"] = verified
    report["checkpoint_failures"] = failures
    if failures and report["status"] != STATUS_BROKEN:
        report["status"] = STATUS_BROKEN
        report["first_break"] = {
            "kind": failures[0]["kind"],
            "seq": failures[0]["seq"],
            "row_id": None,
            "detail": failures[0]["detail"],
        }


class AuditChainSealer:
    """Periodic asyncio sealing pass, modeled on the retention purge sweeper.

    Started from the app lifespan on the API role only. The DB work is
    synchronous, so each pass runs in a thread and the event loop stays free.
    """

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        self.check_interval = int(
            check_interval_seconds
            if check_interval_seconds is not None
            else settings.audit_chain_seal_interval_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the sealing background task."""
        if self._running:
            logger.warning("Audit chain sealer is already running")
            return
        if not background_passes_allowed():
            # A pass walks every account and competes for memory with the
            # request bodies a gateway replica is already holding. See
            # preloop.services.service_roles.
            logger.info(
                "Audit chain sealer not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._seal_loop())
        logger.info(
            "Audit chain sealer started (check_interval=%ss, checkpoint=%s rows)",
            self.check_interval,
            checkpoint_interval(),
        )

    async def stop(self) -> None:
        """Stop the sealing background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the loop task.
                pass

    async def _seal_loop(self) -> None:
        """Seal on every tick, after waiting one interval first."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._seal_once)
            except Exception:
                logger.error("Error in audit chain seal pass", exc_info=True)

    @staticmethod
    def _seal_once() -> None:
        """One synchronous pass with its own session."""
        db = next(get_db_session())
        try:
            run_seal_pass(db)
        finally:
            db.close()


_sealer_instance: Optional[AuditChainSealer] = None


def get_audit_chain_sealer() -> AuditChainSealer:
    """Get or create the global audit chain sealer."""
    global _sealer_instance
    if _sealer_instance is None:
        _sealer_instance = AuditChainSealer()
    return _sealer_instance
