"""Bounded encrypted artifact transport shared by workspace and session recovery."""

import hashlib
import io
import json
import tarfile
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

MAX_MEMBERS = 100_000


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


def put_artifact(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    kind: Literal["workspace", "native_session"],
    archive: bytes,
    metadata: dict[str, Any],
) -> ArtifactReference:
    """Validate and atomically commit encrypted bytes and metadata."""
    expanded = validate_archive(
        archive,
        max_bytes=settings.workspace_snapshot_max_bytes,
        max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
    )
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
                metadata = dict(metadata)
    now = datetime.now(UTC)
    ttl = (
        settings.flow_native_session_retention_hours
        if kind == "native_session"
        else settings.workspace_snapshot_ttl_hours
    )
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
            "expires_at": datetime.fromisoformat(
                manifest["expires_at"].replace("Z", "+00:00")
            ),
        },
        quota_bytes=settings.flow_artifact_account_quota_bytes,
    )
    return artifact_reference(artifact)


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
        max_bytes=settings.workspace_snapshot_max_bytes,
        max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
    )
    return archive
