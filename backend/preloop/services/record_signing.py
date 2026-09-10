"""Ed25519 signing for records that leave the platform.

Two ideas, kept separate on purpose.

**The key.** One active Ed25519 key per account. The private half is stored
Fernet-encrypted through :mod:`preloop.utils.encryption`, the same at-rest
treatment as model credentials and webhook secrets (#504). The public half is
served in the clear from an endpoint, because a signature nobody can check is
decoration. Rotation retires the old key and mints a new one; retired keys
keep their public half so signatures already issued stay verifiable. Every
signature carries its ``key_id``, so a verifier never has to guess.

**The signature.** Always detached, always over a digest, never over a blob.
The signed bytes are domain separated:

    preloop.signature/v1\\n<payload_type>\\n<digest>\\n<signed_at>

so a signature over an evidence pack manifest can never be replayed as a
signature over a period export manifest or an audit checkpoint, and the time
of signing is covered rather than merely claimed.

What this proves is narrow and the docs say so at length: it proves the bytes
in front of you are the bytes the holder of that key signed. It does not prove
the records were true when they were written. The key lives in the same
database as the records, so a platform administrator who can forge a record
can also sign the forgery. The value is for evidence that has already left:
a bundle a customer downloaded last year can be checked today by someone who
does not trust the platform's copy of it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models.models.account_signing_key import (
    KEY_ID_PREFIX,
    SIGNING_ALGORITHM_ED25519,
    AccountSigningKey,
)
from preloop.models.models.record_signature import (
    SUBJECT_EVIDENCE_PACK,
    RecordSignature,
)
from preloop.utils.encryption import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

SIGNATURE_SCHEMA = "preloop.signature/v1"
SIGNATURE_DOMAIN = b"preloop.signature/v1\n"

#: Payload types. The type is inside the signed bytes, so one of these
#: signatures can never be presented as another.
PAYLOAD_PERIOD_EXPORT = "preloop.retention.period_export_manifest/v1"
PAYLOAD_EVIDENCE_PACK = "preloop.cra.evidence_manifest/v1"
PAYLOAD_AUDIT_CHECKPOINT = "preloop.audit.chain_checkpoint/v1"

#: Name of the signature member inside a period export archive. It is not in
#: the manifest's ``members`` list, and cannot be: it covers the manifest.
SIGNATURE_MEMBER_NAME = "signature.json"


class SigningError(RuntimeError):
    """Signing was asked for and could not be done."""


@dataclass(frozen=True)
class KeyMaterial:
    """A usable key pair, decrypted, with the row it came from."""

    key_id: str
    algorithm: str
    public_key: str
    private_key: Ed25519PrivateKey
    record: AccountSigningKey


def new_key_id() -> str:
    """Return a fresh public key identifier."""
    return f"{KEY_ID_PREFIX}{secrets.token_hex(8)}"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def public_key_from_b64(value: str) -> Ed25519PublicKey:
    """Load a public key from its base64 raw form."""
    return Ed25519PublicKey.from_public_bytes(_unb64(value))


def digest_of(payload: Any) -> str:
    """Digest a manifest (or any JSON value) the way this product does.

    ``canonical_manifest_json`` is the evidence pack serialisation from #511,
    reused so the product has one canonical form rather than a second one
    invented for signing.
    """
    return hashlib.sha256(canonical_manifest_json(payload)).hexdigest()


def signed_bytes(*, payload_type: str, digest: str, signed_at: str) -> bytes:
    """Return the exact bytes signed. Shipped so verifiers can copy it."""
    return (
        SIGNATURE_DOMAIN
        + payload_type.encode("utf-8")
        + b"\n"
        + digest.encode("ascii")
        + b"\n"
        + signed_at.encode("ascii")
    )


def format_signed_at(value: Optional[datetime] = None) -> str:
    """The one spelling of a signing time, second resolution, always UTC.

    Public because it is part of the wire format: a verifier rebuilding the
    signed bytes has to produce this string exactly, and a naive datetime
    read back from a column has to land on the same value as the aware one
    that was signed.
    """
    stamp = value or datetime.now(UTC)
    if getattr(stamp, "tzinfo", None) is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_active_key(db: Session, *, account_id: Any) -> Optional[AccountSigningKey]:
    """Return the account's active signing key, or None when it has none."""
    return db.execute(
        select(AccountSigningKey).where(
            AccountSigningKey.account_id == account_id,
            AccountSigningKey.retired_at.is_(None),
        )
    ).scalar_one_or_none()


def list_keys(db: Session, *, account_id: Any) -> list[AccountSigningKey]:
    """Every key this account has held, newest first. Public parts only."""
    return list(
        db.execute(
            select(AccountSigningKey)
            .where(AccountSigningKey.account_id == account_id)
            .order_by(AccountSigningKey.created_at.desc())
        )
        .scalars()
        .all()
    )


def get_key_by_id(
    db: Session, *, account_id: Any, key_id: str
) -> Optional[AccountSigningKey]:
    """Look one key up by its public identifier, inside the account."""
    return db.execute(
        select(AccountSigningKey).where(
            AccountSigningKey.account_id == account_id,
            AccountSigningKey.key_id == key_id,
        )
    ).scalar_one_or_none()


def create_key(
    db: Session,
    *,
    account_id: Any,
    user_id: Optional[Any] = None,
    commit: bool = True,
) -> AccountSigningKey:
    """Mint a signing key for an account that has none.

    The caller is responsible for having retired any previous active key; the
    partial unique index refuses a second active key rather than trusting that.
    """
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes_raw()
    raw_public = private.public_key().public_bytes_raw()
    record = AccountSigningKey(
        account_id=account_id,
        key_id=new_key_id(),
        algorithm=SIGNING_ALGORITHM_ED25519,
        public_key=_b64(raw_public),
        private_key_encrypted=encrypt_value(_b64(raw_private)),
        rotated_by_user_id=user_id,
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def ensure_key(
    db: Session, *, account_id: Any, commit: bool = True
) -> Optional[AccountSigningKey]:
    """Return the active key, minting one on first use.

    Returns None rather than raising when minting fails. Signing is an
    addition to an export, never a precondition for it: a customer asking for
    their compliance period must still get it if key generation broke.
    """
    existing = get_active_key(db, account_id=account_id)
    if existing is not None:
        return existing
    try:
        return create_key(db, account_id=account_id, commit=commit)
    except Exception:
        db.rollback()
        logger.error("Failed to mint an account signing key", exc_info=True)
        return get_active_key(db, account_id=account_id)


def rotate_key(
    db: Session,
    *,
    account_id: Any,
    user_id: Optional[Any] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> tuple[Optional[AccountSigningKey], AccountSigningKey]:
    """Retire the active key, if any, and mint its replacement.

    Returns ``(retired, active)``. The retired key keeps its public half, so
    every signature it ever made still verifies. Rotation that invalidated
    past signatures would be revocation of the customer's own evidence.
    """
    retired = get_active_key(db, account_id=account_id)
    if retired is not None:
        retired.retired_at = now or datetime.now(UTC)
        retired.rotated_by_user_id = user_id or retired.rotated_by_user_id
        db.add(retired)
        db.flush()
    created = create_key(db, account_id=account_id, user_id=user_id, commit=False)
    if commit:
        db.commit()
        db.refresh(created)
        if retired is not None:
            db.refresh(retired)
    return retired, created


def load_material(record: AccountSigningKey) -> KeyMaterial:
    """Decrypt one key row into usable material."""
    try:
        raw = _unb64(decrypt_value(record.private_key_encrypted))
    except Exception as exc:
        raise SigningError(
            f"signing key {record.key_id} cannot be decrypted with the "
            "configured encryption key"
        ) from exc
    return KeyMaterial(
        key_id=record.key_id,
        algorithm=record.algorithm,
        public_key=record.public_key,
        private_key=Ed25519PrivateKey.from_private_bytes(raw),
        record=record,
    )


def sign_digest(
    material: KeyMaterial,
    *,
    payload_type: str,
    digest: str,
    signed_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return a detached signature document over one digest."""
    stamp = format_signed_at(signed_at)
    signature = material.private_key.sign(
        signed_bytes(payload_type=payload_type, digest=digest, signed_at=stamp)
    )
    return {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": material.algorithm,
        "key_id": material.key_id,
        "payload_type": payload_type,
        "digest": digest,
        "signed_at": stamp,
        "signature": _b64(signature),
        "note": (
            "Detached Ed25519 signature over the digest above, which is the "
            "sha256 of the canonical JSON of the named payload. It proves "
            "these bytes are the bytes this key signed. It does not prove "
            "the records were true when they were written."
        ),
    }


def sign_manifest(
    db: Session,
    *,
    account_id: Any,
    payload_type: str,
    manifest: Any,
    signed_at: Optional[datetime] = None,
    commit: bool = True,
) -> Optional[dict[str, Any]]:
    """Sign a manifest for an account, minting a key on first use.

    Returns None when the account has no usable key and one could not be
    made. Callers degrade to an unsigned export rather than failing it.
    """
    record = ensure_key(db, account_id=account_id, commit=commit)
    if record is None:
        return None
    try:
        material = load_material(record)
    except SigningError:
        logger.error("Signing key is unusable; serving unsigned", exc_info=True)
        return None
    return sign_digest(
        material,
        payload_type=payload_type,
        digest=digest_of(manifest),
        signed_at=signed_at,
    )


def verify_signature_document(
    document: Any,
    *,
    public_key: str,
    payload_type: Optional[str] = None,
    digest: Optional[str] = None,
) -> bool:
    """Verify a detached signature document against a public key.

    Shipped in the product so the documented verification steps are the code
    the tests exercise, the same reason #504 shipped its webhook verifier.

    Args:
        document: The parsed signature document.
        public_key: Base64 raw Ed25519 public key to check against.
        payload_type: Required payload type, when the caller knows it.
        digest: Digest the caller computed itself, when it has the payload.

    Returns:
        True only when the signature verifies and every stated expectation
        holds.
    """
    if not isinstance(document, dict):
        return False
    if document.get("algorithm") != SIGNING_ALGORITHM_ED25519:
        return False
    claimed_type = document.get("payload_type")
    claimed_digest = document.get("digest")
    stamp = document.get("signed_at")
    raw_signature = document.get("signature")
    if not (
        isinstance(claimed_type, str)
        and isinstance(claimed_digest, str)
        and isinstance(stamp, str)
        and isinstance(raw_signature, str)
    ):
        return False
    if payload_type is not None and claimed_type != payload_type:
        return False
    if digest is not None and claimed_digest != digest:
        return False
    try:
        key = public_key_from_b64(public_key)
        key.verify(
            _unb64(raw_signature),
            signed_bytes(
                payload_type=claimed_type, digest=claimed_digest, signed_at=stamp
            ),
        )
    except (InvalidSignature, ValueError, TypeError, base64.binascii.Error):
        return False
    return True


def public_key_summary(record: AccountSigningKey) -> dict[str, Any]:
    """The public view of one key. Never includes private material."""
    return {
        "key_id": record.key_id,
        "algorithm": record.algorithm,
        "public_key": record.public_key,
        "active": record.retired_at is None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "retired_at": record.retired_at.isoformat() if record.retired_at else None,
    }


def evidence_pack_payload(
    *,
    account_id: Any,
    artifact_id: Any,
    execution_id: Any,
    archive_sha256: str,
    size_bytes: Optional[int],
    created_at: Optional[datetime],
) -> dict[str, Any]:
    """The payload signed for one evidence pack.

    Small and self-contained on purpose. A verifier holding the downloaded
    archive can rebuild every field here: sha256 the bytes, compare, then
    check the signature over the canonical JSON of this object. Signing the
    stored artifact manifest instead would have made verification depend on
    fields the downloader never receives.
    """
    return {
        "schema": PAYLOAD_EVIDENCE_PACK,
        "account_id": str(account_id),
        "artifact_id": str(artifact_id),
        "execution_id": str(execution_id) if execution_id is not None else None,
        "archive_sha256": archive_sha256,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "created_at": format_signed_at(created_at) if created_at else None,
    }


def get_record_signature(
    db: Session, *, account_id: Any, payload_type: str, subject_id: Any
) -> Optional[RecordSignature]:
    """Look up the stored signature for one record, or None."""
    return db.execute(
        select(RecordSignature).where(
            RecordSignature.account_id == account_id,
            RecordSignature.payload_type == payload_type,
            RecordSignature.subject_id == str(subject_id),
        )
    ).scalar_one_or_none()


def record_signature_document(record: RecordSignature) -> dict[str, Any]:
    """Render a stored signature as the same document a bundle carries."""
    return {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": record.algorithm,
        "key_id": record.signing_key_id,
        "payload_type": record.payload_type,
        "digest": record.digest,
        "signed_at": format_signed_at(record.signed_at),
        "signature": record.signature,
        "payload": record.payload,
    }


def sign_record(
    db: Session,
    *,
    account_id: Any,
    payload_type: str,
    subject_type: str,
    subject_id: Any,
    payload: Any,
    signed_at: Optional[datetime] = None,
    commit: bool = True,
) -> Optional[dict[str, Any]]:
    """Sign a payload and store the signature beside the record it covers.

    Idempotent by subject: an existing signature is returned unchanged rather
    than replaced, because the records this is used for are immutable and a
    second signature over the same bytes would only invite the question of
    which one is real. Returns None when the account has no usable key, since
    signing is an addition to a record and never a precondition for storing
    it.
    """
    existing = get_record_signature(
        db, account_id=account_id, payload_type=payload_type, subject_id=subject_id
    )
    if existing is not None:
        return record_signature_document(existing)
    key = ensure_key(db, account_id=account_id, commit=commit)
    if key is None:
        return None
    try:
        material = load_material(key)
    except SigningError:
        logger.error("Signing key is unusable; storing no signature", exc_info=True)
        return None
    document = sign_digest(
        material,
        payload_type=payload_type,
        digest=digest_of(payload),
        signed_at=signed_at,
    )
    stamp = datetime.strptime(document["signed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    record = RecordSignature(
        account_id=account_id,
        payload_type=payload_type,
        subject_type=subject_type,
        subject_id=str(subject_id),
        payload=payload,
        digest=document["digest"],
        algorithm=document["algorithm"],
        signing_key_id=document["key_id"],
        signature=document["signature"],
        signed_at=stamp,
    )
    db.add(record)
    if commit:
        db.commit()
    else:
        db.flush()
    out = dict(document)
    out["payload"] = payload
    return out


def sign_evidence_pack(
    db: Session,
    *,
    account_id: Any,
    artifact_id: Any,
    execution_id: Any,
    archive_sha256: str,
    size_bytes: Optional[int] = None,
    created_at: Optional[datetime] = None,
    commit: bool = True,
) -> Optional[dict[str, Any]]:
    """Sign one evidence pack at mint time. Never raises at the caller."""
    payload = evidence_pack_payload(
        account_id=account_id,
        artifact_id=artifact_id,
        execution_id=execution_id,
        archive_sha256=archive_sha256,
        size_bytes=size_bytes,
        created_at=created_at,
    )
    try:
        # Inside a savepoint, so a signing failure rolls back the signature
        # and nothing else. A plain rollback here would undo the caller's
        # work, and an evidence pack that stored but did not sign is worth
        # far more than an upload that failed: the bytes cannot be captured
        # again after the run has finished.
        with db.begin_nested():
            document = sign_record(
                db,
                account_id=account_id,
                payload_type=PAYLOAD_EVIDENCE_PACK,
                subject_type=SUBJECT_EVIDENCE_PACK,
                subject_id=artifact_id,
                payload=payload,
                commit=False,
            )
        if commit:
            db.commit()
        return document
    except Exception:
        logger.error("Failed to sign evidence pack %s", artifact_id, exc_info=True)
        return None
