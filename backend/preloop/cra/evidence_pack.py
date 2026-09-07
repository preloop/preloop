"""CRA evidence-pack integrity checks for persist and CI.

Uses ``preloop.services.flow_artifacts`` for gzip/tar validation and
``result.json`` extraction. Download acceptance binds the controller
digest. Packed agent JSON is compared on SBOM/finding content, not only
schema/verdict/status. Server ``evidence`` annotations on the API result
are not a substitute for that digest.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from preloop.config import settings
from preloop.cra.schemas import CAPTURE_ERROR_CODES
from preloop.cra.validate import json_in
from preloop.services.flow_artifacts import extract_result_json, validate_archive

EVIDENCE_STATUS_HEADER = "x-preloop-evidence-status"
EVIDENCE_SHA256_HEADER = "x-preloop-evidence-sha256"
AVAILABLE_STATUS = "available"

_RESULT_NAMES = frozenset({"result.json", "workspace/result.json"})
_CONTENT_KEYS = (
    "findings",
    "sbom",
    "source",
    "source_sbom",
    "sbom_audit",
    "vuln_scan",
    "inventory",
    "checks",
    "minimum_elements",
    "license_flags",
    "component",
    "record",
    "coverage",
    "art14_candidates",
    "inputs_declared",
)
_VALIDATE_MESSAGES = {
    "artifact_oversized": "evidence archive exceeds size bound",
    "artifact_empty": "evidence archive is empty",
    "artifact_expansion_limit": "evidence archive expansion limit exceeded",
    "artifact_corrupt": "evidence archive is corrupt",
    "artifact_unsafe_path": "evidence archive contains an unsafe path",
    "artifact_unsafe_member": "evidence archive contains an unsafe member",
    "artifact_invalid_members": "evidence archive members are invalid",
}


class EvidencePackError(ValueError):
    """Raised when an evidence archive cannot be accepted as CRA evidence."""


def evidence_archive_max_bytes() -> int:
    """Compressed evidence cap from durable settings (default 32 MiB)."""
    return int(settings.flow_evidence_max_bytes)


def evidence_expanded_max_bytes() -> int:
    """Expanded extraction cap from durable settings (default 2 GiB)."""
    return int(settings.flow_artifact_expanded_max_bytes)


def validate_gzip_tar_archive(
    archive: bytes,
    *,
    max_bytes: Optional[int] = None,
    max_expanded_bytes: Optional[int] = None,
) -> int:
    """Reject empty, oversized, or corrupt gzip/tar bodies."""
    compressed = evidence_archive_max_bytes() if max_bytes is None else max_bytes
    expanded = (
        evidence_expanded_max_bytes()
        if max_expanded_bytes is None
        else max_expanded_bytes
    )
    try:
        return int(
            validate_archive(archive, max_bytes=compressed, max_expanded_bytes=expanded)
        )
    except ValueError as exc:
        code = str(exc) or "artifact_corrupt"
        raise EvidencePackError(
            _VALIDATE_MESSAGES.get(code, "evidence archive is corrupt")
        ) from exc


def _member_names(archive: bytes) -> list[str]:
    names: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                names.append(member.name)
    return names


def _is_result_member(name: str) -> bool:
    return name in _RESULT_NAMES or name.endswith("/result.json")


def require_evidence_members(archive: bytes) -> list[str]:
    """Require at least one file member. Legacy packs may omit result.json."""
    try:
        names = _member_names(archive)
    except (tarfile.TarError, OSError) as exc:
        raise EvidencePackError("evidence archive is corrupt") from exc
    if not names:
        raise EvidencePackError("evidence archive has no file members")
    return names


def archive_sha256(archive: bytes) -> str:
    """Return the lowercase hex digest of the gzip body."""
    return hashlib.sha256(archive).hexdigest()


def header_value(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Read a header case-insensitively."""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            text = str(value).strip()
            return text or None
    return None


def controller_digest(
    headers: Mapping[str, str],
    receipt: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return the controller-advertised archive digest, if any.

    Agent annotations on result.json are ignored. Only download headers
    and the evidence-status receipt are controller authority.
    """
    advertised = header_value(headers, EVIDENCE_SHA256_HEADER)
    if advertised:
        return advertised.lower().removeprefix("sha256:")
    if receipt is not None:
        digest = receipt.get("sha256") or receipt.get("digest")
        if isinstance(digest, str) and digest.strip():
            return digest.lower().removeprefix("sha256:")
    return None


def verify_server_digest(archive: bytes, headers: Mapping[str, str]) -> None:
    """When the download supplies a digest header, it must match the body."""
    advertised = header_value(headers, EVIDENCE_SHA256_HEADER)
    if not advertised:
        return
    expected = advertised.lower().removeprefix("sha256:")
    actual = archive_sha256(archive)
    if expected != actual:
        raise EvidencePackError(
            "evidence digest does not match X-Preloop-Evidence-SHA256"
        )


def verify_evidence_status_header(headers: Mapping[str, str]) -> None:
    """When the download supplies a status, only ``available`` is usable."""
    status = header_value(headers, EVIDENCE_STATUS_HEADER)
    if status is None:
        return
    if status.lower() != AVAILABLE_STATUS:
        raise EvidencePackError(
            f"evidence status is {status!r}; only {AVAILABLE_STATUS!r} may be accepted"
        )


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    execution_id: str,
    archive: bytes,
) -> None:
    """Check a server evidence receipt against the downloaded body."""
    status = receipt.get("status")
    if status != AVAILABLE_STATUS:
        raise EvidencePackError(
            f"evidence receipt status is {status!r}; only {AVAILABLE_STATUS!r} may be accepted"
        )
    receipt_exec = receipt.get("execution_id")
    if receipt_exec is not None and str(receipt_exec) != str(execution_id):
        raise EvidencePackError(
            "evidence receipt execution_id does not match the requested execution"
        )
    digest = receipt.get("sha256") or receipt.get("digest")
    if isinstance(digest, str) and digest:
        expected = digest.lower().removeprefix("sha256:")
        if expected != archive_sha256(archive):
            raise EvidencePackError(
                "evidence receipt sha256 does not match the archive"
            )
    size_bytes = receipt.get("size_bytes")
    if isinstance(size_bytes, int) and size_bytes != len(archive):
        raise EvidencePackError(
            "evidence receipt size_bytes does not match the archive"
        )


def _unwrap_result(obj: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(obj, Mapping):
        return None
    if json_in(obj.get("error"), CAPTURE_ERROR_CODES):
        raw = obj.get("raw")
        if isinstance(raw, Mapping):
            return raw
        return None
    return obj


def _content_fingerprint(obj: Mapping[str, Any]) -> str:
    subset = {key: obj.get(key) for key in _CONTENT_KEYS if key in obj}
    encoded = json.dumps(
        subset, sort_keys=True, default=str, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def results_bind_content(
    api_result: Any,
    archive_result: Mapping[str, Any],
) -> None:
    """Bind packed result.json to the persisted result beyond envelope fields.

    Schema/verdict/status/flow must still agree, and SBOM/finding content
    must fingerprint-match. Controller ``evidence`` annotations are ignored.
    """
    candidate = _unwrap_result(api_result)
    if candidate is None:
        raise EvidencePackError(
            "packed result.json is inconsistent with the persisted execution result"
        )
    for key in ("schema", "verdict", "status", "flow"):
        if key in candidate or key in archive_result:
            if candidate.get(key) != archive_result.get(key):
                raise EvidencePackError(
                    "packed result.json is inconsistent with the persisted "
                    "execution result"
                )
    if _content_fingerprint(candidate) != _content_fingerprint(archive_result):
        raise EvidencePackError(
            "packed result.json content does not match the persisted result"
        )


def accept_evidence_archive(
    archive: bytes,
    *,
    headers: Mapping[str, str],
    execution_id: str,
    api_result: Any = None,
    receipt: Optional[Mapping[str, Any]] = None,
    max_bytes: Optional[int] = None,
    max_expanded_bytes: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Validate membership, digest, and result binding.

    Uniform packs include ``result.json``. Legacy capture (default
    ``FLOW_ARTIFACT_DIRECT_UPLOAD=false``) may omit it and use flat member
    names; those archives are accepted only with a controller digest.
    """
    validate_gzip_tar_archive(
        archive, max_bytes=max_bytes, max_expanded_bytes=max_expanded_bytes
    )
    names = require_evidence_members(archive)
    verify_evidence_status_header(headers)
    verify_server_digest(archive, headers)
    if receipt is not None:
        verify_receipt(receipt, execution_id=execution_id, archive=archive)
    packed = extract_result_json(archive)
    has_result = packed is not None or any(_is_result_member(name) for name in names)
    digest = controller_digest(headers, receipt)
    if has_result:
        if packed is None:
            raise EvidencePackError(
                "evidence archive result.json is missing or unreadable"
            )
        packed_exec = packed.get("execution_id")
        if packed_exec is not None and str(packed_exec) != str(execution_id):
            raise EvidencePackError(
                "packed result.json execution_id does not match the requested execution"
            )
        if api_result is not None:
            results_bind_content(api_result, packed)
        return packed
    if digest is None or digest != archive_sha256(archive):
        raise EvidencePackError(
            "legacy evidence archive has no result.json and no matching "
            "controller digest"
        )
    return None


def same_origin(left: str, right: str) -> bool:
    """Return True when two URLs share scheme and host:port."""
    a = urlsplit(left)
    b = urlsplit(right)
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())
