"""Account-scoped persistence for encrypted runtime-session artifacts."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.services.artifact_media import ARTIFACT_KINDS, check_content
from preloop.utils.encryption import _get_fernet

_KIND_LIMITS: dict[str, str] = {
    kind: f"runtime_session_{kind}_max_bytes" for kind in ARTIFACT_KINDS
}
_UNAVAILABLE: frozenset[str] = frozenset({"evicted", "expired"})

PRODUCERS: frozenset[str] = frozenset(
    {
        "gateway",
        "firewall",
        "deposit_api",
        "deposit_mcp",
        "cli",
        "hook",
        "runner",
        "browser_steps",
    }
)
TEXT_STATUSES: frozenset[str] = frozenset(
    {"none", "extracted", "ocr", "captioned", "failed"}
)

# Label keys with a documented meaning. Any other key that matches the key
# pattern is accepted as account-defined.
#   site: physical or logical site the artifact belongs to (e.g. a plant).
#   tenant_ref: the caller's own customer or tenant reference.
#   consent_basis: why recording this person is allowed (e.g. contract).
#   retention_class: retention bucket name; enforced by EE records policy.
#   tags: free-form list of short strings.
RESERVED_LABEL_KEYS: frozenset[str] = frozenset(
    {"site", "tenant_ref", "consent_basis", "retention_class", "tags"}
)
LABEL_MAX_KEYS = 16
LABEL_VALUE_MAX_CHARS = 128
LABEL_TAGS_MAX = 16
_LABEL_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


def _label_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= LABEL_VALUE_MAX_CHARS


def validate_labels(labels: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``labels`` after checking keys and values.

    Rules: at most 16 keys; each key matches ``^[a-z][a-z0-9_.-]{0,62}$``;
    each value is a string of up to 128 characters, except ``tags``, which is
    a list of up to 16 such strings.

    Args:
        labels: Label mapping, or None for no labels.

    Returns:
        The validated labels (an empty dict for None).

    Raises:
        ValueError: ``artifact_labels_invalid`` when any rule is broken.
    """
    if labels is None:
        return {}
    if not isinstance(labels, dict) or len(labels) > LABEL_MAX_KEYS:
        raise ValueError("artifact_labels_invalid")
    clean: dict[str, Any] = {}
    for key, value in labels.items():
        if not isinstance(key, str) or not _LABEL_KEY.match(key):
            raise ValueError("artifact_labels_invalid")
        if key == "tags":
            if (
                not isinstance(value, list)
                or len(value) > LABEL_TAGS_MAX
                or not all(_label_string(tag) for tag in value)
            ):
                raise ValueError("artifact_labels_invalid")
            clean[key] = list(value)
        elif _label_string(value):
            clean[key] = value
        else:
            raise ValueError("artifact_labels_invalid")
    return clean


def _max_bytes(kind: str) -> int:
    """Return the plaintext cap for an artifact kind.

    Args:
        kind: Artifact kind, one of
            :data:`preloop.services.artifact_media.ARTIFACT_KINDS`.

    Returns:
        The configured maximum plaintext size in bytes.

    Raises:
        ValueError: ``artifact_kind_invalid`` when ``kind`` has no cap.
    """
    attr = _KIND_LIMITS.get(kind)
    if attr is None:
        raise ValueError("artifact_kind_invalid")
    return int(getattr(settings, attr))


def _existing_source_row(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
    kind: str,
    source: str,
    source_ref: str,
) -> models.RuntimeSessionArtifact | None:
    """Return the row already stored for this source reference, if any."""
    return (
        db.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.account_id == account_id,
            models.RuntimeSessionArtifact.runtime_session_id == runtime_session_id,
            models.RuntimeSessionArtifact.kind == kind,
            models.RuntimeSessionArtifact.source == source,
            models.RuntimeSessionArtifact.source_ref == source_ref,
        )
        .one_or_none()
    )


def store(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
    kind: str,
    source: str,
    source_ref: str | None,
    content_type: str,
    plaintext: bytes,
    manifest: dict[str, Any],
    activity_id: UUID | None = None,
    expires_at: datetime | None = None,
    name: str | None = None,
    labels: dict[str, Any] | None = None,
    producer: str | None = None,
    agent_id: UUID | None = None,
    tool_name: str | None = None,
    text_status: str = "none",
    parent_artifact_id: UUID | None = None,
    commit: bool = True,
) -> models.RuntimeSessionArtifact:
    """Encrypt and insert an artifact, or return the existing source row.

    The plaintext is encrypted with the process Fernet key. ``sha256`` and
    ``size_bytes`` are taken from the plaintext. A second call with the same
    ``(runtime_session_id, kind, source, source_ref)`` returns the stored row
    and does not replace its ciphertext. Rows with a null ``source_ref`` are
    not idempotent. The row copies ``legal_hold`` from its session, so an
    artifact stored after a hold is placed is frozen immediately.

    Args:
        db: Database session.
        account_id: Owning account. Reads always filter on this.
        runtime_session_id: Session the bytes belong to.
        kind: One of :data:`preloop.services.artifact_media.ARTIFACT_KINDS`.
        source: Producer name, for example ``browser_use``.
        source_ref: Source-native id. Null skips the idempotency key.
        content_type: Media type of the plaintext.
        plaintext: Unencrypted bytes. Not stored.
        manifest: Free-form metadata such as ``step_index`` or ``duration_ms``.
        activity_id: Optional activity the artifact illustrates.
        expires_at: Optional retention deadline. Not purged here.
        name: Optional display name, for example a file name.
        labels: Account-defined labels, checked by :func:`validate_labels`.
        producer: Ingest path, one of :data:`PRODUCERS`.
        agent_id: Agent that produced the bytes, when known.
        tool_name: Tool that produced the bytes, when known.
        text_status: One of :data:`TEXT_STATUSES`.
        parent_artifact_id: Artifact in the same account this one derives
            from, for example the transcript a summary was written from.
        commit: When True, commit the insert. When False, only flush.

    Returns:
        The new row, or the unchanged row when the source key already exists.

    Raises:
        ValueError: ``artifact_kind_invalid`` for an unknown kind,
            ``artifact_media_type_invalid`` when the media type is not
            allowed for the kind, ``artifact_content_mismatch`` when the
            bytes do not match the media type (or a generated file is an
            executable), ``artifact_labels_invalid`` for bad labels,
            ``artifact_producer_invalid``, ``artifact_text_status_invalid``,
            ``artifact_name_invalid``, ``artifact_parent_invalid`` when the
            parent is not an artifact of this account,
            ``artifact_too_large`` when the plaintext exceeds that kind's cap,
            or ``storage_budget_exhausted`` when the account budget cannot fit
            the plaintext even after evicting every unheld artifact. Nothing
            is inserted in those cases. Callers map
            ``storage_budget_exhausted`` to HTTP 507 for recordings and to a
            rejected row for screenshots.
    """
    if len(plaintext) > _max_bytes(kind):
        raise ValueError("artifact_too_large")
    content_type = check_content(kind, content_type, plaintext)
    clean_labels = validate_labels(labels)
    if producer is not None and producer not in PRODUCERS:
        raise ValueError("artifact_producer_invalid")
    if text_status not in TEXT_STATUSES:
        raise ValueError("artifact_text_status_invalid")
    if name is not None and (not name or len(name) > 255):
        raise ValueError("artifact_name_invalid")
    if tool_name is not None and (not tool_name or len(tool_name) > 255):
        raise ValueError("artifact_tool_name_invalid")
    if (
        parent_artifact_id is not None
        and get(db, account_id=account_id, artifact_id=parent_artifact_id) is None
    ):
        raise ValueError("artifact_parent_invalid")

    if source_ref is not None:
        existing = _existing_source_row(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            kind=kind,
            source=source,
            source_ref=source_ref,
        )
        if existing is not None:
            return existing

    # Serialize budget checks the way flow-artifact quota checks do.
    # Account identity does not change, so NO KEY UPDATE is sufficient.
    db.query(models.Account).filter(models.Account.id == account_id).with_for_update(
        key_share=True
    ).one()
    if source_ref is not None:
        existing = _existing_source_row(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            kind=kind,
            source=source,
            source_ref=source_ref,
        )
        if existing is not None:
            return existing

    from preloop.services.session_artifact_budget import (
        enforce_account_budget,
        notify_evicted,
    )

    evicted = enforce_account_budget(
        db, account_id=account_id, incoming_bytes=len(plaintext)
    )

    session_held = (
        db.query(models.RuntimeSession.legal_hold)
        .filter(
            models.RuntimeSession.id == runtime_session_id,
            models.RuntimeSession.account_id == account_id,
        )
        .scalar()
    )
    artifact = models.RuntimeSessionArtifact(
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        activity_id=activity_id,
        kind=kind,
        source=source,
        source_ref=source_ref,
        content_type=content_type,
        size_bytes=len(plaintext),
        sha256=hashlib.sha256(plaintext).hexdigest(),
        manifest=dict(manifest),
        ciphertext=_get_fernet().encrypt(plaintext),
        availability="available",
        expires_at=expires_at,
        legal_hold=bool(session_held),
        name=name,
        labels=clean_labels,
        producer=producer,
        agent_id=agent_id,
        tool_name=tool_name,
        text_status=text_status,
        parent_artifact_id=parent_artifact_id,
        # Client clock, not transaction_timestamp(): two inserts in one
        # transaction would otherwise share created_at and sort by uuid.
        created_at=datetime.now(UTC),
    )
    try:
        with db.begin_nested():
            db.add(artifact)
            db.flush()
    except IntegrityError:
        db.expunge(artifact)
        if source_ref is None:
            raise
        existing = _existing_source_row(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            kind=kind,
            source=source,
            source_ref=source_ref,
        )
        if existing is None:
            raise
        return existing
    if commit:
        db.commit()
        db.refresh(artifact)
        if evicted:
            notify_evicted(db, account_id=account_id, artifacts=evicted)
    return artifact


def get(
    db: Session, *, account_id: UUID, artifact_id: UUID
) -> models.RuntimeSessionArtifact | None:
    """Return one artifact, or None when it is not in this account.

    Args:
        db: Database session.
        account_id: Account the caller is allowed to read.
        artifact_id: Primary key of the artifact.

    Returns:
        The row, or None when the id is missing or belongs to another account.
    """
    return (
        db.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.id == artifact_id,
            models.RuntimeSessionArtifact.account_id == account_id,
        )
        .one_or_none()
    )


def list_for_session(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
    kind: str | None = None,
    labels: dict[str, Any] | None = None,
    producer: str | None = None,
) -> list[models.RuntimeSessionArtifact]:
    """List an account's artifacts for one session, oldest first.

    Args:
        db: Database session.
        account_id: Account the caller is allowed to read.
        runtime_session_id: Session to list.
        kind: When set, only rows of this kind.
        labels: When set, only rows whose labels contain these (JSONB
            ``@>``). ``{"tags": ["a"]}`` matches rows tagged ``a``.
        producer: When set, only rows from this producer.

    Returns:
        Matching rows ordered by ``created_at``.
    """
    query = db.query(models.RuntimeSessionArtifact).filter(
        models.RuntimeSessionArtifact.account_id == account_id,
        models.RuntimeSessionArtifact.runtime_session_id == runtime_session_id,
    )
    if kind is not None:
        query = query.filter(models.RuntimeSessionArtifact.kind == kind)
    if labels:
        query = query.filter(models.RuntimeSessionArtifact.labels.contains(labels))
    if producer is not None:
        query = query.filter(models.RuntimeSessionArtifact.producer == producer)
    return list(
        query.order_by(
            models.RuntimeSessionArtifact.created_at.asc(),
            models.RuntimeSessionArtifact.id.asc(),
        ).all()
    )


def decrypt(artifact: models.RuntimeSessionArtifact) -> bytes:
    """Decrypt an artifact's ciphertext.

    Args:
        artifact: Row previously returned by :func:`store` or :func:`get`.

    Returns:
        The original plaintext.

    Raises:
        ValueError: ``artifact_unavailable`` when the ciphertext has been
            cleared, or ``artifact_undecryptable`` when the stored token
            cannot be decrypted.
    """
    if artifact.ciphertext is None:
        raise ValueError("artifact_unavailable")
    try:
        return bytes(_get_fernet().decrypt(bytes(artifact.ciphertext)))
    except InvalidToken as exc:
        raise ValueError("artifact_undecryptable") from exc


def cleanup(db: Session, *, now: datetime) -> int:
    """Clear ciphertext on expired artifacts that are not under legal hold.

    A row is skipped when its own flag is set or its session is held, whatever
    its ``expires_at`` says. The session check covers an artifact written
    while a hold is already in force if the copied flag was missed. The hold
    has to block payload expiry, not only deletion of the session: a
    screenshot a regulator may ask for has to still be downloadable.

    Args:
        db: Database session.
        now: Instant compared with ``expires_at``. Rows expiring at ``now``
            stay until a later pass.

    Returns:
        Rows whose ciphertext was cleared.
    """
    session_held = (
        select(models.RuntimeSession.id)
        .where(
            models.RuntimeSession.id
            == models.RuntimeSessionArtifact.runtime_session_id,
            models.RuntimeSession.legal_hold.is_(True),
        )
        .exists()
    )
    count = (
        db.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.expires_at < now,
            models.RuntimeSessionArtifact.legal_hold.is_(False),
            ~session_held,
            models.RuntimeSessionArtifact.availability == "available",
        )
        .update(
            {
                models.RuntimeSessionArtifact.ciphertext: None,
                models.RuntimeSessionArtifact.availability: "expired",
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(count or 0)


def mark_unavailable(
    db: Session,
    *,
    account_id: UUID,
    artifact_id: UUID,
    availability: Literal["evicted", "expired"],
    commit: bool = True,
) -> bool:
    """Clear ciphertext and record why the bytes are gone.

    Args:
        db: Database session.
        account_id: Account the caller is allowed to update.
        artifact_id: Primary key of the artifact.
        availability: ``evicted`` or ``expired``.
        commit: When True, commit the update. When False, only flush.

    Returns:
        True when a row in this account was updated, False when none matched.

    Raises:
        ValueError: ``artifact_availability_invalid`` for any other status.
    """
    if availability not in _UNAVAILABLE:
        raise ValueError("artifact_availability_invalid")
    row = get(db, account_id=account_id, artifact_id=artifact_id)
    if row is None:
        return False
    row.ciphertext = None
    row.availability = availability
    if commit:
        db.commit()
    else:
        db.flush()
    return True


def _available_bytes(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID | None = None,
    kind: str | None = None,
) -> int:
    """Sum plaintext sizes of artifacts that still have ciphertext."""
    query = db.query(
        func.coalesce(func.sum(models.RuntimeSessionArtifact.size_bytes), 0)
    ).filter(
        models.RuntimeSessionArtifact.account_id == account_id,
        models.RuntimeSessionArtifact.availability == "available",
    )
    if runtime_session_id is not None:
        query = query.filter(
            models.RuntimeSessionArtifact.runtime_session_id == runtime_session_id
        )
    if kind is not None:
        query = query.filter(models.RuntimeSessionArtifact.kind == kind)
    total = query.scalar()
    return int(total or 0)


def session_bytes(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
    kind: str | None = None,
) -> int:
    """Sum available plaintext bytes for one session.

    Args:
        db: Database session.
        account_id: Account the caller is allowed to read.
        runtime_session_id: Session to total.
        kind: When set, only rows of this kind.

    Returns:
        Sum of ``size_bytes`` where ``availability`` is ``available``.
    """
    return _available_bytes(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        kind=kind,
    )


def account_bytes(
    db: Session,
    *,
    account_id: UUID,
    kind: str | None = None,
) -> int:
    """Sum available plaintext bytes for one account.

    Args:
        db: Database session.
        account_id: Account to total.
        kind: When set, only rows of this kind.

    Returns:
        Sum of ``size_bytes`` where ``availability`` is ``available``.
    """
    return _available_bytes(db, account_id=account_id, kind=kind)
