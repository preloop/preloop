"""Bounded encrypted artifact transport shared by workspace and session recovery."""

import hashlib
import io
import json
import logging
import tarfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import flow_artifact as crud
from preloop.models.schemas.flow_artifact import ArtifactManifest, ArtifactReference
from preloop.utils.encryption import _get_fernet

logger = logging.getLogger(__name__)

MAX_MEMBERS = 100_000
RESERVED_RESULT_FIELDS = (
    "trusted_publication",
    "_private_publication",
    "evidence_upload",
    "product_provenance",
    "dossier_manifest",
)
EVIDENCE_UPLOAD_OUTCOMES = frozenset({"uploaded", "failed", "absent"})
TERMINAL_EXECUTION_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED", "TIMED_OUT"}
)
ArtifactKind = Literal["workspace", "native_session", "evidence"]
EVIDENCE_UNAVAILABLE_HTTP = {
    "missing": 404,
    "expired": 410,
    "failed": 409,
}


def artifact_thread_id(trigger: Any, execution_id: Any) -> str:
    """Resolve the durable thread id that mint and verify both use.

    Orchestrator context may carry a live ``thread_id``; capability checks only
    see persisted trigger fields, so mint must not prefer a different value.
    """
    details = trigger if isinstance(trigger, dict) else {}
    resume = details.get("_resume")
    resume = resume if isinstance(resume, dict) else {}
    return str(
        details.get("_session_thread_id")
        or resume.get("thread_id")
        or resume.get("execution_id")
        or execution_id
    )


def validate_archive(archive: bytes, *, max_bytes: int, max_expanded_bytes: int) -> int:
    """Validate before storage/extraction, rejecting links and special files.

    Expanded-size and member limits are independent of compressed size. Tar
    streams are read through EOF so corrupt/truncated bodies cannot commit.
    """
    if not archive or len(archive) > max_bytes:
        raise ValueError("artifact_oversized" if archive else "artifact_empty")
    total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as stream:
            for member in stream:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or not path.parts
                ):
                    raise ValueError("artifact_unsafe_path")
                if member.name in seen or len(seen) >= MAX_MEMBERS:
                    raise ValueError("artifact_invalid_members")
                seen.add(member.name)
                if not (member.isfile() or member.isdir()) or member.size < 0:
                    raise ValueError("artifact_unsafe_member")
                total += member.size
                if total > max_expanded_bytes:
                    raise ValueError("artifact_expansion_limit")
                if member.isfile():
                    body = stream.extractfile(member)
                    if body is None:
                        raise ValueError("artifact_corrupt")
                    remaining = member.size
                    while remaining:
                        chunk = body.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("artifact_corrupt")
                        remaining -= len(chunk)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("artifact_corrupt") from exc
    return total


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Stable identity for an immutable manifest."""
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def artifact_reference(artifact: Any) -> ArtifactReference:
    """Build the shared reference without exposing storage credentials."""
    return ArtifactReference(
        artifact_id=artifact.id,
        execution_id=artifact.execution_id,
        manifest_sha256=artifact.manifest_sha256,
    )


def artifact_max_bytes(kind: str) -> int:
    """Compressed upload cap for one artifact kind."""
    if kind == "evidence":
        return int(settings.flow_evidence_max_bytes)
    return int(settings.workspace_snapshot_max_bytes)


def artifact_retention_hours(kind: str) -> int:
    """Operational retention for one artifact kind. Not a legal hold."""
    if kind == "evidence":
        return int(settings.flow_evidence_retention_hours)
    if kind == "native_session":
        return int(settings.flow_native_session_retention_hours)
    return int(settings.workspace_snapshot_ttl_hours)


#: What the integrity field of a receipt actually asserts. "false" used to
#: cover two very different situations: nobody looked, and somebody looked and
#: the bytes were wrong. On a pack whose digest verifies three ways, an
#: operator reads the first as the second (round 2 CRA rerun, P8).
INTEGRITY_VERIFIED = "verified"
INTEGRITY_NOT_CHECKED = "not_checked"
INTEGRITY_FAILED = "failed"
INTEGRITY_NOTES: Mapping[str, str] = {
    INTEGRITY_VERIFIED: (
        "The archive was read and its sha256 matches the recorded digest."
    ),
    INTEGRITY_NOT_CHECKED: (
        "Availability only. This endpoint does not read the archive; the "
        "digest is verified on download, which returns "
        "x-preloop-evidence-integrity and x-preloop-evidence-sha256."
    ),
    INTEGRITY_FAILED: "The archive was read and its sha256 did not match.",
}


def integrity_state(*, verified: bool, error: str | None = None) -> str:
    """Name the three cases the boolean could not tell apart."""
    if error and "digest" in str(error):
        return INTEGRITY_FAILED
    return INTEGRITY_VERIFIED if verified else INTEGRITY_NOT_CHECKED


class EvidenceUnavailableError(Exception):
    """Evidence cannot be served; ``code`` is missing, expired, or failed."""

    def __init__(self, code: str, receipt: dict[str, Any]) -> None:
        if code not in EVIDENCE_UNAVAILABLE_HTTP:
            raise ValueError("evidence_status_invalid")
        super().__init__(f"evidence_{code}")
        self.code = code
        self.receipt = receipt
        self.status_code = EVIDENCE_UNAVAILABLE_HTTP[code]


def evidence_receipt(
    *,
    status: str,
    execution_id: Any,
    transport: str,
    artifact: Any = None,
    archive: bytes | None = None,
    error: str | None = None,
    integrity_verified: bool = False,
) -> dict[str, Any]:
    """Build a public receipt with no payload bytes or credentials.

    ``sha256`` / ``digest`` are identity metadata from the stored manifest.
    They are not proof of a verified download unless ``integrity_verified``.
    """
    manifest = dict(getattr(artifact, "manifest", None) or {})
    digest = manifest.get("sha256")
    if not digest and archive:
        digest = hashlib.sha256(archive).hexdigest()
    expires = getattr(artifact, "expires_at", None) or manifest.get("expires_at")
    if hasattr(expires, "isoformat"):
        expires = expires.isoformat()
    created = manifest.get("created_at") or getattr(artifact, "created_at", None)
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    state = integrity_state(verified=integrity_verified, error=error)
    return {
        "version": 1,
        "kind": "evidence",
        "status": status,
        "transport": transport,
        "execution_id": str(execution_id) if execution_id is not None else None,
        "artifact_id": str(artifact.id) if artifact is not None else None,
        "manifest_sha256": getattr(artifact, "manifest_sha256", None),
        "sha256": digest,
        "digest": digest,
        "size_bytes": (
            manifest.get("size_bytes")
            if artifact is not None
            else (len(archive) if archive else None)
        ),
        "expanded_bytes": manifest.get("expanded_bytes"),
        "created_at": created,
        "expires_at": expires,
        "retention_hours": artifact_retention_hours("evidence"),
        # object_lock stays false because Preloop does not assert it. It is a
        # storage-layer property the operator configures (S3 Object Lock, a
        # WORM volume) and the control plane has no way to verify it.
        "object_lock": False,
        # legal_hold is now a real fact about this row rather than a constant:
        # true means a legal_hold record freezes the pack, so the janitor
        # leaves the ciphertext alone past expires_at and the retention purge
        # leaves the row alone.
        "legal_hold": bool(getattr(artifact, "legal_hold", False)),
        "integrity_verified": integrity_verified,
        "integrity": state,
        "integrity_note": INTEGRITY_NOTES[state],
        "error": error,
    }


def sanitize_captured_result(result: Any) -> dict[str, Any] | None:
    """Strip reserved control-plane keys from captured agent JSON.

    Getter capture and tar ``result.json`` extraction both use this helper so
    reserved keys cannot enter the persisted execution result. Agent JSON
    cannot author evidence upload outcome or publication receipts.
    """
    if not isinstance(result, dict):
        return None
    cleaned = dict(result)
    for key in RESERVED_RESULT_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def extract_result_json(archive: bytes) -> dict[str, Any] | None:
    """Read result.json packed next to evidence members, if present."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for name in ("result.json", "workspace/result.json"):
                try:
                    member = tar.getmember(name)
                except KeyError:
                    continue
                if not member.isfile() or member.size > 256 * 1024:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                parsed = json.loads(source.read())
                cleaned = sanitize_captured_result(parsed)
                if cleaned is not None:
                    return cleaned
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def _as_receipt_dict(value: Any) -> dict[str, Any]:
    """Copy a persisted receipt mapping; ignore ORM mocks and other objects."""
    return dict(value) if isinstance(value, dict) else {}


def execution_is_terminal(execution: Any) -> bool:
    """True once the execution can no longer accept a live evidence refresh."""
    return str(getattr(execution, "status", "") or "") in TERMINAL_EXECUTION_STATUSES


def trusted_evidence_upload(message: Mapping[str, Any] | None) -> str | None:
    """Final evidence PUT outcome from runner completion metadata.

    Only a top-level string field is trusted. Agent ``result`` JSON cannot
    author this value. Non-string JSON is ``failed``, never an exception.
    """
    if not isinstance(message, Mapping):
        return None
    if "evidence_upload" not in message:
        return None
    raw = message.get("evidence_upload")
    if isinstance(raw, str) and raw in EVIDENCE_UPLOAD_OUTCOMES:
        return raw
    if raw is None or raw == "":
        return None
    return "failed"


def job_requires_evidence_upload(pending_job: Mapping[str, Any] | None) -> bool:
    """True when this lease was configured for direct evidence upload."""
    if not isinstance(pending_job, Mapping):
        return False
    if pending_job.get("completion_protocol") == "host_exec":
        return False
    return pending_job.get("evidence_direct_upload") is True


def _bound_evidence_artifact(
    db: Session, *, account_id: UUID, execution: Any, receipt: Mapping[str, Any]
) -> Any | None:
    """Load the immutable artifact named by a terminal receipt, or None."""
    raw_id = receipt.get("artifact_id")
    if not raw_id:
        return None
    try:
        artifact_id = UUID(str(raw_id))
    except ValueError:
        return None
    thread_id = artifact_thread_id(execution.trigger_event_details, execution.id)
    artifact = crud.get(
        db,
        artifact_id=artifact_id,
        account_id=account_id,
        flow_id=execution.flow_id,
        thread_id=thread_id,
    )
    if (
        artifact is None
        or artifact.execution_id != execution.id
        or artifact.kind != "evidence"
    ):
        return None
    expected = receipt.get("sha256") or receipt.get("digest")
    manifest = dict(getattr(artifact, "manifest", None) or {})
    digest = manifest.get("sha256")
    if expected and digest and expected != digest:
        return None
    return artifact


def bind_terminal_evidence(
    db: Session,
    *,
    account_id: UUID,
    execution: Any,
    evidence_upload: str | None,
) -> dict[str, Any] | None:
    """Persist the trusted final upload outcome on a terminal execution.

    ``uploaded`` freezes the latest committed evidence row. ``failed`` and
    ``absent`` persist those statuses so a later ``latest()`` cannot revive an
    earlier trap artifact. A missing field leaves live capture in charge.
    """
    from preloop.models.crud import crud_flow_execution

    if evidence_upload is None:
        return None
    transport = "direct"
    if evidence_upload == "failed":
        receipt = evidence_receipt(
            status="failed",
            execution_id=execution.id,
            transport=transport,
            error="evidence_upload_failed",
        )
    elif evidence_upload == "absent":
        receipt = evidence_receipt(
            status="missing",
            execution_id=execution.id,
            transport=transport,
            error="evidence_absent",
        )
    elif evidence_upload == "uploaded":
        thread_id = artifact_thread_id(execution.trigger_event_details, execution.id)
        artifact = crud.latest(
            db,
            account_id=account_id,
            flow_id=execution.flow_id,
            thread_id=thread_id,
            execution_id=execution.id,
            kind="evidence",
        )
        if artifact is None or artifact.ciphertext is None:
            receipt = evidence_receipt(
                status="failed",
                execution_id=execution.id,
                transport=transport,
                error="evidence_upload_missing",
            )
        else:
            receipt = evidence_receipt(
                status="available",
                execution_id=execution.id,
                transport=transport,
                artifact=artifact,
            )
    else:
        receipt = evidence_receipt(
            status="failed",
            execution_id=execution.id,
            transport=transport,
            error="evidence_upload_invalid",
        )
    crud_flow_execution.set_evidence_receipt(db, db_obj=execution, receipt=receipt)
    return receipt


def _legacy_archive_bytes(execution: Any) -> bytes | None:
    """The legacy in-row archive, when this execution still has one."""
    archive = getattr(execution, "evidence_archive", None)
    if isinstance(archive, (bytes, bytearray, memoryview)):
        raw = bytes(archive)
        return raw or None
    return None


def _mark_status_receipt(
    receipt: dict[str, Any], *, archive: bytes | None = None
) -> dict[str, Any]:
    """Availability metadata for polls, and what was and was not checked.

    ``integrity_verified: false`` used to be hard-set here regardless of what
    the platform knew, so a legacy pack whose bytes sit in the row next to the
    receipt, whose manifest verifies and whose download returns
    ``x-preloop-evidence-integrity: verified`` was reported to the operator as
    unverified (round 2 CRA rerun, P8). The boolean is kept for compatibility
    and joined by ``integrity``, which distinguishes "nobody looked" from
    "somebody looked and the bytes were wrong". When the archive is in hand,
    this looks.
    """
    status = str(receipt.get("status") or "missing")
    expires = receipt.get("expires_at")
    # A held pack does not expire on the poll path either. The hold service
    # stamps this key on the persisted receipt when it freezes a pack, so the
    # cheap status poll and the live inspect agree.
    if receipt.get("legal_hold"):
        expires = None
    if status == "available" and expires:
        try:
            exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= datetime.now(UTC):
                status = "expired"
        except ValueError:
            # Receipt expiry is advisory for polls. A malformed timestamp
            # must not flip available to expired or raise into the status
            # endpoint; download still verifies digest.
            logger.debug("Ignoring unparseable evidence receipt expires_at")
    out = dict(receipt)
    out["status"] = status
    out["kind"] = "evidence"
    verified = False
    digest = out.get("digest") or out.get("sha256")
    if archive is not None and status == "available":
        observed = hashlib.sha256(archive).hexdigest()
        if digest and digest != observed:
            # The row disagrees with itself. Say so here rather than letting
            # the download be the first place anyone finds out.
            out["status"] = "failed"
            out["error"] = out.get("error") or "artifact_digest_mismatch"
            out["observed_sha256"] = observed
        else:
            digest = observed
            verified = True
            out.setdefault("size_bytes", len(archive))
    out["integrity_verified"] = verified
    state = integrity_state(verified=verified, error=out.get("error"))
    out["integrity"] = state
    out["integrity_note"] = INTEGRITY_NOTES[state]
    if digest:
        out["digest"] = digest
        out["sha256"] = digest
    out.setdefault("object_lock", False)
    out.setdefault("legal_hold", False)
    return out


def public_evidence_status(execution: Any) -> dict[str, Any]:
    """Account-scoped availability from the execution row. No decrypt or hash.

    CI polls ``GET /result`` frequently. Use the persisted receipt (or the
    presence of the legacy column) rather than querying ciphertext.
    Availability is not integrity proof.
    """
    raw = getattr(execution, "evidence_receipt", None)
    transport = raw.get("transport") if isinstance(raw, dict) else None
    # Only the legacy path keeps the bytes next to the receipt. A direct
    # transport receipt says nothing about integrity here, by design: the
    # ciphertext is not decrypted on a poll.
    archive = (
        _legacy_archive_bytes(execution)
        if str(transport or "") in ("", "legacy")
        else None
    )
    if isinstance(raw, dict) and raw.get("status"):
        return _mark_status_receipt(raw, archive=archive)
    if archive is not None:
        # The bytes are in the row we already loaded. Hashing them is cheaper
        # than an operator opening a support thread about a pack that is fine.
        return evidence_receipt(
            status="available",
            execution_id=getattr(execution, "id", None),
            transport="legacy",
            archive=archive,
            integrity_verified=True,
        )
    return evidence_receipt(
        status="missing",
        execution_id=getattr(execution, "id", None),
        transport="none",
    )


def _receipt_for_artifact(execution: Any, artifact: Any) -> dict[str, Any]:
    """Build a receipt from a scoped evidence row's current availability."""
    now = datetime.now(UTC)
    # A held pack whose bytes are still there is available, whatever the
    # operational expiry says: the hold is what stopped the janitor from
    # taking them, so reporting "expired" would contradict the download.
    held = bool(getattr(artifact, "legal_hold", False))
    if held and artifact.ciphertext is not None:
        expired = False
    else:
        expired = artifact.expires_at <= now or artifact.ciphertext is None
    status = "expired" if expired else str(artifact.availability or "available")
    if status not in {"available", "expired", "failed"}:
        status = "expired" if expired else "available"
    return evidence_receipt(
        status=status,
        execution_id=execution.id,
        transport="direct",
        artifact=artifact,
    )


def inspect_evidence(
    db: Session, *, account_id: UUID, execution: Any
) -> dict[str, Any]:
    """Live availability for download/finalize.

    Before terminal close, refresh from ``latest()`` so a postprocessing PUT
    replaces an EXIT-trap artifact. Once a terminal receipt is bound, serve
    that immutable ``artifact_id`` (and verify scope/digest) instead of a
    newer unrelated row.
    """
    stored = _as_receipt_dict(getattr(execution, "evidence_receipt", None))
    if execution_is_terminal(execution):
        if stored.get("status") in {"failed", "missing", "expired"}:
            return _mark_status_receipt(stored)
        if stored.get("artifact_id"):
            artifact = _bound_evidence_artifact(
                db, account_id=account_id, execution=execution, receipt=stored
            )
            if artifact is None:
                return evidence_receipt(
                    status="failed",
                    execution_id=execution.id,
                    transport=str(stored.get("transport") or "direct"),
                    error="artifact_scope_mismatch",
                )
            return _receipt_for_artifact(execution, artifact)
        if stored.get("status") == "available" and stored.get("transport") == "legacy":
            archive = getattr(execution, "evidence_archive", None)
            if isinstance(archive, (bytes, bytearray, memoryview)) and bytes(archive):
                return _mark_status_receipt(stored)
    thread_id = artifact_thread_id(execution.trigger_event_details, execution.id)
    artifact = crud.latest(
        db,
        account_id=account_id,
        flow_id=execution.flow_id,
        thread_id=thread_id,
        execution_id=execution.id,
        kind="evidence",
    )
    if artifact is not None:
        return _receipt_for_artifact(execution, artifact)
    archive = getattr(execution, "evidence_archive", None)
    if isinstance(archive, (bytes, bytearray, memoryview)) and bytes(archive):
        return evidence_receipt(
            status="available",
            execution_id=execution.id,
            transport="legacy",
        )
    if stored.get("status") in {"failed", "missing", "expired"}:
        return _mark_status_receipt(stored)
    return evidence_receipt(
        status="missing",
        execution_id=execution.id,
        transport=str(stored.get("transport") or "none"),
        error=stored.get("error"),
    )


def load_evidence(
    db: Session, *, account_id: UUID, execution: Any
) -> tuple[bytes, dict[str, Any]]:
    """Return verified evidence bytes or raise ``EvidenceUnavailableError``."""
    receipt = inspect_evidence(db, account_id=account_id, execution=execution)
    status = str(receipt.get("status") or "missing")
    if status == "missing":
        raise EvidenceUnavailableError("missing", receipt)
    if status == "failed":
        raise EvidenceUnavailableError("failed", receipt)
    if status == "expired":
        raise EvidenceUnavailableError("expired", receipt)
    if receipt.get("transport") == "legacy":
        archive = bytes(execution.evidence_archive or b"")
        if not archive:
            raise EvidenceUnavailableError("missing", receipt)
        digest = hashlib.sha256(archive).hexdigest()
        expected = receipt.get("sha256") or receipt.get("digest")
        if expected and expected != digest:
            failed = evidence_receipt(
                status="failed",
                execution_id=execution.id,
                transport="legacy",
                archive=archive,
                error="artifact_digest_mismatch",
            )
            raise EvidenceUnavailableError("failed", failed)
        verified = evidence_receipt(
            status="available",
            execution_id=execution.id,
            transport="legacy",
            archive=archive,
            integrity_verified=True,
        )
        return archive, verified
    thread_id = artifact_thread_id(execution.trigger_event_details, execution.id)
    artifact = None
    if execution_is_terminal(execution) and receipt.get("artifact_id"):
        artifact = _bound_evidence_artifact(
            db, account_id=account_id, execution=execution, receipt=receipt
        )
        if artifact is None:
            failed = evidence_receipt(
                status="failed",
                execution_id=execution.id,
                transport="direct",
                error="artifact_scope_mismatch",
            )
            raise EvidenceUnavailableError("failed", failed)
    else:
        artifact = crud.latest(
            db,
            account_id=account_id,
            flow_id=execution.flow_id,
            thread_id=thread_id,
            execution_id=execution.id,
            kind="evidence",
        )
    if artifact is None:
        raise EvidenceUnavailableError("missing", receipt)
    try:
        archive = get_artifact(
            db,
            account_id=account_id,
            flow_id=execution.flow_id,
            thread_id=thread_id,
            reference=artifact_reference(artifact),
        )
    except ValueError as exc:
        code = str(exc)
        if code == "artifact_expired":
            raise EvidenceUnavailableError(
                "expired",
                evidence_receipt(
                    status="expired",
                    execution_id=execution.id,
                    transport="direct",
                    artifact=artifact,
                    error=code,
                ),
            ) from exc
        raise EvidenceUnavailableError(
            "failed",
            evidence_receipt(
                status="failed",
                execution_id=execution.id,
                transport="direct",
                artifact=artifact,
                error=code,
            ),
        ) from exc
    return archive, evidence_receipt(
        status="available",
        execution_id=execution.id,
        transport="direct",
        artifact=artifact,
        archive=archive,
        integrity_verified=True,
    )


def put_artifact(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    kind: ArtifactKind,
    archive: bytes,
    require_execution_open: bool = True,
) -> ArtifactReference:
    """Validate and atomically commit encrypted bytes and metadata.

    Capability PUTs keep ``require_execution_open=True``. Controller-owned
    retention after a failed run passes False.
    """
    expanded = validate_archive(
        archive,
        max_bytes=artifact_max_bytes(kind),
        max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
    )
    metadata: dict[str, Any] = {}
    native_expiry = None
    if kind in {"workspace", "native_session"}:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            try:
                member = tar.getmember(
                    "workspace/.preloop-checkpoint.json"
                    if kind == "workspace"
                    else "manifest.json"
                )
                if member.size > 65536:
                    raise ValueError("artifact_metadata_oversized")
                source = tar.extractfile(member)
                metadata = json.loads(source.read()) if source else {}
                if kind == "native_session":
                    if metadata.get("thread_id") != thread_id:
                        raise ValueError("artifact_thread_mismatch")
                    native_expiry = datetime.fromisoformat(
                        metadata["expires_at"].replace("Z", "+00:00")
                    )
                    if native_expiry.tzinfo is None:
                        raise ValueError("artifact_invalid_expiry")
            except KeyError:
                if kind == "native_session":
                    raise ValueError("artifact_native_manifest_missing") from None
    now = datetime.now(UTC)
    ttl = artifact_retention_hours(kind)
    expires_at = now + timedelta(hours=max(0, ttl))
    if native_expiry is not None:
        expires_at = min(expires_at, native_expiry)
    manifest = ArtifactManifest(
        kind=kind,
        execution_id=execution_id,
        thread_id=thread_id,
        sha256=hashlib.sha256(archive).hexdigest(),
        size_bytes=len(archive),
        expanded_bytes=expanded,
        created_at=now,
        expires_at=expires_at,
        metadata=metadata,
    ).model_dump(mode="json")
    artifact = crud.store(
        db,
        values={
            "account_id": account_id,
            "flow_id": flow_id,
            "thread_id": thread_id,
            "execution_id": execution_id,
            "kind": kind,
            "manifest": manifest,
            "manifest_sha256": manifest_digest(manifest),
            "ciphertext": _get_fernet().encrypt(archive),
            "availability": "available",
            "expires_at": datetime.fromisoformat(
                manifest["expires_at"].replace("Z", "+00:00")
            ),
        },
        quota_bytes=settings.flow_artifact_account_quota_bytes,
        require_execution_open=require_execution_open,
    )
    if kind == "evidence":
        # Imported here, not at module scope: signing canonicalises through
        # preloop.cra.evidence_pack, which imports this module.
        from preloop.services import record_signing

        # Sign at mint, not at download. A signature made when the pack is
        # served would only ever say "this is what we hold now"; made here it
        # is dated to the capture, and re-serving cannot change it (#558).
        record_signing.sign_evidence_pack(
            db,
            account_id=account_id,
            artifact_id=artifact.id,
            execution_id=execution_id,
            archive_sha256=manifest["sha256"],
            size_bytes=manifest.get("size_bytes"),
            created_at=now,
            # store() already committed the artifact; this is the boundary
            # that persists the signature (and a first-use key) beside it.
            commit=True,
        )
    return artifact_reference(artifact)


def evidence_signature(
    db: Session, *, account_id: UUID, artifact_id: Any
) -> dict[str, Any] | None:
    """The detached signature over one evidence pack, or None.

    None is a normal answer: packs captured before #558, and packs from an
    account whose key could not be minted, have no signature and the receipt
    says so rather than pretending.
    """
    if not artifact_id:
        return None
    from preloop.services import record_signing

    try:
        record = record_signing.get_record_signature(
            db,
            account_id=account_id,
            payload_type=record_signing.PAYLOAD_EVIDENCE_PACK,
            subject_id=artifact_id,
        )
    except Exception:
        logger.warning("Could not read the evidence signature", exc_info=True)
        return None
    if record is None:
        return None
    return record_signing.record_signature_document(record)


def attach_evidence_signature(
    db: Session, *, account_id: UUID, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Return the receipt with its signature, when the pack has one.

    Kept out of :func:`evidence_receipt` on purpose: that function is called
    on paths that hold no session, and a receipt builder that needs a database
    round trip would put one on every status poll.
    """
    document = evidence_signature(
        db, account_id=account_id, artifact_id=receipt.get("artifact_id")
    )
    out = dict(receipt)
    out["signature"] = document
    out["signing_key_id"] = (document or {}).get("key_id")
    return out


def get_artifact(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    reference: ArtifactReference,
) -> bytes:
    """Authorize, lease, decrypt and revalidate a checkpoint before restore."""
    if reference.storage_kind != "hosted":
        raise ValueError("artifact_runner_local")
    artifact = crud.get(
        db,
        artifact_id=reference.artifact_id,
        account_id=account_id,
        flow_id=flow_id,
        thread_id=thread_id,
    )
    if artifact is None or artifact.execution_id != reference.execution_id:
        raise ValueError("artifact_missing")
    now = datetime.now(UTC)
    if artifact.expires_at <= now or artifact.ciphertext is None:
        raise ValueError("artifact_expired")
    if (
        artifact.manifest_sha256 != reference.manifest_sha256
        or manifest_digest(artifact.manifest) != reference.manifest_sha256
    ):
        raise ValueError("artifact_manifest_mismatch")
    artifact = crud.lease(db, artifact=artifact, until=now + timedelta(minutes=10))
    try:
        archive = _get_fernet().decrypt(bytes(artifact.ciphertext))
    except InvalidToken as exc:
        raise ValueError("artifact_corrupt") from exc
    if hashlib.sha256(archive).hexdigest() != artifact.manifest["sha256"]:
        raise ValueError("artifact_digest_mismatch")
    validate_archive(
        archive,
        max_bytes=artifact_max_bytes(str(artifact.kind)),
        max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
    )
    return archive
