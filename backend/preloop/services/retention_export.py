"""Period export: audit rows, approvals and evidence receipts as one archive.

The purpose is narrow and worth stating. Retention plus a purge means records
eventually go. A customer whose obligation outlives our retention, or who
simply wants their compliance evidence somewhere we cannot reach, needs to be
able to take the period away with them and prove later that what they kept is
what they were given. That is the whole feature: a tar with the rows, a
manifest that names and digests every member, and a digest over the manifest's
member list.

The manifest deliberately reuses the shape #511 gave evidence packs
(:func:`preloop.cra.evidence_pack.build_pack_manifest`): same ``members``
entries, same ``members_digest`` computed over the same canonical JSON. Two
manifest formats for the same idea would be one format too many, and #558
signs exactly one thing because of it.

Since #558 the archive also carries ``signature.json``, a detached Ed25519
signature over the sha256 of ``manifest.json`` as packed, made with the
account's signing key. Verification is: recompute each member's digest,
recompute ``members_digest`` from the manifest, then check the signature over
the manifest bytes with the account's published public key.

What this is not: it is not proof that the rows were true when they were
written. The digests show the archive was not altered after we built it and
the signature shows we built it, both of which are claims about bytes, not
about the world. The signing key lives on the same platform that wrote the
records. Saying so here is cheaper than having someone infer it.

Bounded on purpose: ``RETENTION_EXPORT_MAX_ROWS`` per record class, and going
over is an error telling the caller to narrow the period. A compliance export
that silently drops the rows past a limit is worse than no export.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models.crud import crud_audit_log
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.flow_artifact import FlowArtifact
from preloop.models.models.legal_hold import LegalHold
from preloop.services.legal_hold import hold_summary
from preloop.services.record_signing import (
    PAYLOAD_PERIOD_EXPORT,
    SIGNATURE_MEMBER_NAME,
    sign_manifest,
)
from preloop.services.retention_policy import RECORD_CLASSES, resolve_retention

logger = logging.getLogger(__name__)

EXPORT_MANIFEST_SCHEMA = "preloop.retention.period_export_manifest/v1"
EXPORT_MANIFEST_NAME = "manifest.json"

MEMBER_AUDIT = "audit/audit_log.jsonl"
MEMBER_APPROVALS = "approvals/approval_request.jsonl"
MEMBER_EVIDENCE = "evidence/receipts.jsonl"
MEMBER_HOLDS = "holds/legal_hold.jsonl"

AUDIT_ACTION_EXPORT = "retention_period_export"


class PeriodExportError(ValueError):
    """The requested period cannot be exported as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PeriodExport:
    """One built archive and the manifest that describes it."""

    archive: bytes
    manifest: dict[str, Any]
    counts: dict[str, int]
    #: Detached signature over the manifest digest, or None when the account
    #: has no usable signing key. An unsigned export is still an export.
    signature: Optional[dict[str, Any]] = None

    @property
    def sha256(self) -> str:
        """Digest of the archive bytes as served."""
        return hashlib.sha256(self.archive).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        """Digest of the manifest as packed, which is what gets signed."""
        return hashlib.sha256(canonical_manifest_json(self.manifest)).hexdigest()

    @property
    def key_id(self) -> Optional[str]:
        """Identifier of the key that signed this export, if any."""
        return (self.signature or {}).get("key_id")

    @property
    def filename(self) -> str:
        """Stable, sortable download name."""
        period = self.manifest.get("period") or {}
        start = str(period.get("start") or "")[:10]
        end = str(period.get("end") or "")[:10]
        return f"preloop-period-export-{start}-to-{end}.tar.gz"


def _iso(value: Any) -> Optional[str]:
    """ISO 8601 for a datetime, passthrough for anything already a string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _max_rows() -> int:
    return max(1, int(settings.retention_export_max_rows))


def _fetch(db: Session, stmt) -> list[Any]:
    """Read one class, refusing to truncate.

    One extra row is asked for so "exactly at the limit" and "over the limit"
    are distinguishable without a second COUNT.
    """
    limit = _max_rows()
    rows = list(db.execute(stmt.limit(limit + 1)).scalars().all())
    if len(rows) > limit:
        raise PeriodExportError(
            "period_too_large",
            f"the period holds more than {limit} rows for one record class; "
            "narrow the date range and export it in parts",
        )
    return rows


def _audit_rows(
    db: Session, *, account_id: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.account_id == account_id,
            AuditLog.timestamp >= start,
            AuditLog.timestamp < end,
        )
        .order_by(AuditLog.timestamp, AuditLog.id)
    )
    return [
        {
            "id": str(row.id),
            "timestamp": _iso(row.timestamp),
            "user_id": str(row.user_id) if row.user_id else None,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "status": row.status,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "details": row.details,
            # Chain position travels with the row (#558), so a bundle taken
            # today can be checked against a checkpoint years later without
            # asking the platform for anything.
            "chain_seq": row.chain_seq,
            "prev_hash": row.prev_hash,
            "row_hash": row.row_hash,
        }
        for row in _fetch(db, stmt)
    ]


def _approval_rows(
    db: Session, *, account_id: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.account_id == account_id,
            ApprovalRequest.requested_at >= start,
            ApprovalRequest.requested_at < end,
        )
        .order_by(ApprovalRequest.requested_at, ApprovalRequest.id)
    )
    rows = []
    for row in _fetch(db, stmt):
        rows.append(
            {
                "id": str(row.id),
                "tool_name": row.tool_name,
                "status": row.status,
                "summary": row.summary,
                "requested_at": _iso(row.requested_at),
                "resolved_at": _iso(row.resolved_at),
                "expires_at": _iso(row.expires_at),
                "execution_id": row.execution_id,
                "managed_agent_id": (
                    str(row.managed_agent_id) if row.managed_agent_id else None
                ),
                "managed_agent_name": row.managed_agent_name,
                "runtime_session_id": (
                    str(row.runtime_session_id) if row.runtime_session_id else None
                ),
                "api_key_id": str(row.api_key_id) if row.api_key_id else None,
                "approver_comment": row.approver_comment,
                "structured_answer": row.structured_answer,
                "responses": row.responses,
                "decided_by_ai": bool(row.decided_by_ai),
                "ai_model": row.ai_model,
                "ai_reasoning": row.ai_reasoning,
                "auto_approved_reason": row.auto_approved_reason,
                "rule_context": row.rule_context,
                "legal_hold": bool(row.legal_hold),
                # tool_args and tool_result are deliberately excluded: they
                # carry the payload of whatever the agent was about to do,
                # which is the least predictable content in the record and not
                # what a retention export is for. The decision, the reason it
                # was asked for, and who decided are.
            }
        )
    return rows


def _evidence_rows(
    db: Session, *, account_id: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Evidence *receipts*, never the packs.

    A period bundle that inlined evidence payloads would be unbounded and
    would duplicate bytes the evidence download already serves under its own
    digest check. The receipt is the durable claim: which pack, what digest,
    when it expires, whether it is held.
    """
    stmt = (
        select(FlowArtifact)
        .where(
            FlowArtifact.account_id == account_id,
            FlowArtifact.kind == "evidence",
            FlowArtifact.created_at >= start,
            FlowArtifact.created_at < end,
        )
        .order_by(FlowArtifact.created_at, FlowArtifact.id)
    )
    rows = []
    for row in _fetch(db, stmt):
        manifest = row.manifest if isinstance(row.manifest, dict) else {}
        rows.append(
            {
                "artifact_id": str(row.id),
                "execution_id": str(row.execution_id),
                "flow_id": str(row.flow_id),
                "thread_id": row.thread_id,
                "kind": row.kind,
                "sha256": manifest.get("sha256"),
                "manifest_sha256": row.manifest_sha256,
                "size_bytes": manifest.get("size_bytes"),
                "expanded_bytes": manifest.get("expanded_bytes"),
                "created_at": _iso(row.created_at),
                "expires_at": _iso(row.expires_at),
                "availability": row.availability,
                "payload_present": row.ciphertext is not None,
                "legal_hold": bool(row.legal_hold),
                # object_lock is not in here at all. Preloop does not assert
                # it, and an absent field is more honest than a false one.
            }
        )
    return rows


def _hold_rows(
    db: Session, *, account_id: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Holds placed in the period, so a frozen record is explained in place."""
    stmt = (
        select(LegalHold)
        .where(
            LegalHold.account_id == account_id,
            LegalHold.placed_at >= start,
            LegalHold.placed_at < end,
        )
        .order_by(LegalHold.placed_at, LegalHold.id)
    )
    return [hold_summary(row) for row in _fetch(db, stmt)]


def _jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    """One JSON object per line, sorted keys, so a diff is readable."""
    buffer = io.BytesIO()
    for row in rows:
        buffer.write(
            json.dumps(row, sort_keys=True, default=str, ensure_ascii=False).encode(
                "utf-8"
            )
        )
        buffer.write(b"\n")
    return buffer.getvalue()


def _member_entry(name: str, body: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _add(tar: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = 0o600
    # Fixed mtime so the same rows produce the same archive bytes twice.
    info.mtime = 0
    tar.addfile(info, io.BytesIO(body))


def build_period_export(
    db: Session,
    *,
    account: Any,
    start: datetime,
    end: datetime,
    generated_at: Optional[datetime] = None,
    record_classes: Sequence[str] = RECORD_CLASSES,
    sign: bool = True,
) -> PeriodExport:
    """Build the archive for one account and one half-open period.

    ``start`` is inclusive and ``end`` exclusive, so consecutive periods tile
    without a row landing in both bundles or in neither.
    """
    if end <= start:
        raise PeriodExportError("invalid_period", "end must be after start")
    account_id = account.id
    bodies: dict[str, bytes] = {}
    counts: dict[str, int] = {}

    audit = _audit_rows(db, account_id=account_id, start=start, end=end)
    bodies[MEMBER_AUDIT] = _jsonl(audit)
    counts["audit"] = len(audit)

    approvals = _approval_rows(db, account_id=account_id, start=start, end=end)
    bodies[MEMBER_APPROVALS] = _jsonl(approvals)
    counts["approvals"] = len(approvals)

    evidence = _evidence_rows(db, account_id=account_id, start=start, end=end)
    bodies[MEMBER_EVIDENCE] = _jsonl(evidence)
    counts["evidence"] = len(evidence)

    holds = _hold_rows(db, account_id=account_id, start=start, end=end)
    bodies[MEMBER_HOLDS] = _jsonl(holds)
    counts["legal_holds"] = len(holds)

    members = [_member_entry(name, body) for name, body in sorted(bodies.items())]
    stamp = generated_at or datetime.now(UTC)
    manifest = {
        "schema": EXPORT_MANIFEST_SCHEMA,
        "account_id": str(account_id),
        "generated_at": stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": {
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "boundary": "start inclusive, end exclusive",
        },
        "members": members,
        # Same computation as the evidence pack manifest, so one verifier
        # covers both and #558 has one thing to sign.
        "members_digest": hashlib.sha256(canonical_manifest_json(members)).hexdigest(),
        "counts": counts,
        "retention": {
            record_class: resolve_retention(
                account.meta_data, record_class=record_class
            ).days
            for record_class in record_classes
        },
        # Named before it exists, and deliberately without the key id: the
        # signature covers this manifest, so nothing the signature says can
        # also be asserted here without the two being able to disagree.
        "signature": {
            "member": SIGNATURE_MEMBER_NAME,
            "payload_type": PAYLOAD_PERIOD_EXPORT,
            "covers": (
                "sha256 of this file as packed, byte for byte, including this "
                "declaration"
            ),
        },
        "note": (
            "sha256 values cover the members of this archive as packed. The "
            "detached signature shows the bundle is the one Preloop built and "
            "has not been altered since. It does not show the records were "
            "true when they were written: the signing key lives on the same "
            "platform that wrote them. Evidence packs are referenced by "
            "receipt (artifact id and digest), not inlined."
        ),
    }
    manifest_body = canonical_manifest_json(manifest)

    signature: Optional[dict[str, Any]] = None
    if sign:
        signature = sign_manifest(
            db,
            account_id=account_id,
            payload_type=PAYLOAD_PERIOD_EXPORT,
            manifest=manifest,
            signed_at=stamp,
        )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, EXPORT_MANIFEST_NAME, manifest_body)
        for name, body in sorted(bodies.items()):
            _add(tar, name, body)
        if signature is not None:
            _add(tar, SIGNATURE_MEMBER_NAME, canonical_manifest_json(signature))
    return PeriodExport(
        archive=buffer.getvalue(),
        manifest=manifest,
        counts=counts,
        signature=signature,
    )


def audit_period_export(
    db: Session,
    *,
    account_id: Any,
    user_id: Any,
    export: PeriodExport,
) -> None:
    """Record that the period left the platform, and with what digest.

    An export is a bulk read of an account's compliance record. Who took it,
    when, for which period, and the digest of what they got are exactly the
    questions asked afterwards.
    """
    period = export.manifest.get("period") or {}
    try:
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user_id,
            action=AUDIT_ACTION_EXPORT,
            resource_type="retention",
            resource_id="period_export",
            status="success",
            details={
                "period_start": period.get("start"),
                "period_end": period.get("end"),
                "counts": export.counts,
                "archive_sha256": export.sha256,
                "members_digest": export.manifest.get("members_digest"),
                "manifest_sha256": export.manifest_sha256,
                "signing_key_id": export.key_id,
                "size_bytes": len(export.archive),
            },
        )
    except Exception:
        db.rollback()
        logger.error("Failed to audit period export", exc_info=True)
