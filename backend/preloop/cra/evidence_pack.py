"""CRA evidence-pack integrity checks compatible with the evidence worker.

The sibling evidence clone owns durable storage, receipts, and
``GET /api/v1/flows/executions/{id}/evidence`` plus ``/evidence-status``.
This module consumes that HTTP/receipt contract and falls back to local
stdlib validation when ``preloop.services.flow_artifacts.extract_result_json``
is not yet merged into this tree.

Public evidence headers (when the evidence worker is deployed):

- ``X-Preloop-Evidence-Status``: ``available`` / ``missing`` / ``expired`` / ``failed``
- ``X-Preloop-Evidence-SHA256``: hex digest of the gzip body
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

# Matches hosted capture ``MAX_EVIDENCE_ARCHIVE_BYTES`` and the evidence
# worker's 256 KiB result.json cap.
MAX_EVIDENCE_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_RESULT_JSON_BYTES = 256 * 1024
EVIDENCE_STATUS_HEADER = "x-preloop-evidence-status"
EVIDENCE_SHA256_HEADER = "x-preloop-evidence-sha256"
AVAILABLE_STATUS = "available"

_RESULT_NAMES = ("result.json", "workspace/result.json")
_EVIDENCE_PREFIXES = ("evidence/", "workspace/evidence/")


class EvidencePackError(ValueError):
    """Raised when an evidence archive cannot be accepted as CRA evidence."""


def _canonical_extract() -> Any:
    try:
        from preloop.services import flow_artifacts as _fa
    except ImportError:
        return None
    return getattr(_fa, "extract_result_json", None)


def _canonical_validate() -> Any:
    try:
        from preloop.services import flow_artifacts as _fa
    except ImportError:
        return None
    return getattr(_fa, "validate_archive", None)


def extract_result_json(archive: bytes) -> Optional[dict[str, Any]]:
    """Read ``result.json`` packed next to evidence members, if present.

    Prefers the evidence worker's ``flow_artifacts.extract_result_json``
    when that symbol exists so hosted persist and CI share one extractor.
    """
    canonical = _canonical_extract()
    if canonical is not None:
        try:
            parsed = canonical(archive)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for name in _RESULT_NAMES:
                try:
                    member = tar.getmember(name)
                except KeyError:
                    continue
                if not member.isfile() or member.size > MAX_RESULT_JSON_BYTES:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                parsed = json.loads(source.read())
                if isinstance(parsed, dict):
                    return parsed
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def validate_gzip_tar_archive(
    archive: bytes,
    *,
    max_bytes: int = MAX_EVIDENCE_ARCHIVE_BYTES,
    max_expanded_bytes: int = MAX_EVIDENCE_EXPANDED_BYTES,
) -> int:
    """Reject empty, oversized, HTML, or corrupt gzip/tar bodies."""
    if not archive:
        raise EvidencePackError("evidence archive is empty")
    if len(archive) > max_bytes:
        raise EvidencePackError("evidence archive exceeds size bound")
    canonical = _canonical_validate()
    if canonical is not None:
        try:
            return int(
                canonical(
                    archive,
                    max_bytes=max_bytes,
                    max_expanded_bytes=max_expanded_bytes,
                )
            )
        except ValueError as exc:
            raise EvidencePackError(str(exc) or "evidence archive is corrupt") from exc
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as stream:
            stream.read(1)
    except (OSError, EOFError) as exc:
        raise EvidencePackError("evidence body is not a valid gzip archive") from exc
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as tar:
            for member in tar:
                total += max(0, int(member.size))
                if total > max_expanded_bytes:
                    raise EvidencePackError("evidence archive expansion limit exceeded")
                if member.isfile():
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise EvidencePackError("evidence archive is corrupt")
                    remaining = member.size
                    while remaining:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            raise EvidencePackError("evidence archive is corrupt")
                        remaining -= len(chunk)
    except EvidencePackError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise EvidencePackError("evidence archive is corrupt") from exc
    return total


def _member_names(archive: bytes) -> list[str]:
    names: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                names.append(member.name)
    return names


def require_result_and_evidence_members(archive: bytes) -> None:
    """Require ``result.json`` plus at least one ``evidence/`` artifact."""
    try:
        names = _member_names(archive)
    except (tarfile.TarError, OSError) as exc:
        raise EvidencePackError("evidence archive is corrupt") from exc
    has_result = any(
        name in _RESULT_NAMES or name.endswith("/result.json") for name in names
    )
    has_evidence = any(
        name == "evidence" or name.startswith(_EVIDENCE_PREFIXES) for name in names
    )
    if not has_result:
        raise EvidencePackError("evidence archive is missing result.json")
    if not has_evidence:
        raise EvidencePackError("evidence archive is missing evidence/ members")


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


def verify_server_digest(archive: bytes, headers: Mapping[str, str]) -> None:
    """When the evidence worker supplies a digest, it must match the body."""
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
    """When the evidence worker supplies a status, only ``available`` is usable."""
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
    digest = receipt.get("sha256")
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


def results_consistent(
    api_result: Any,
    archive_result: Optional[Mapping[str, Any]],
) -> bool:
    """Require the persisted result and packed result.json to agree."""
    if archive_result is None:
        return False
    candidate = api_result
    if isinstance(api_result, Mapping) and isinstance(api_result.get("raw"), Mapping):
        candidate = api_result.get("raw")
    if not isinstance(candidate, Mapping):
        return False
    if candidate.get("schema") != archive_result.get("schema"):
        return False
    for key in ("verdict", "status", "flow"):
        if key in candidate or key in archive_result:
            if candidate.get(key) != archive_result.get(key):
                return False
    return True


def accept_evidence_archive(
    archive: bytes,
    *,
    headers: Mapping[str, str],
    execution_id: str,
    api_result: Any = None,
    receipt: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate membership, digest, and result consistency. Returns packed result.json."""
    validate_gzip_tar_archive(archive)
    require_result_and_evidence_members(archive)
    verify_evidence_status_header(headers)
    verify_server_digest(archive, headers)
    if receipt is not None:
        verify_receipt(receipt, execution_id=execution_id, archive=archive)
    packed = extract_result_json(archive)
    if packed is None:
        raise EvidencePackError("evidence archive result.json is missing or unreadable")
    packed_exec = packed.get("execution_id")
    if packed_exec is not None and str(packed_exec) != str(execution_id):
        raise EvidencePackError(
            "packed result.json execution_id does not match the requested execution"
        )
    if api_result is not None and not results_consistent(api_result, packed):
        raise EvidencePackError(
            "packed result.json is inconsistent with the persisted execution result"
        )
    return packed


def same_origin(left: str, right: str) -> bool:
    """Return True when two URLs share scheme and host:port."""
    a = urlsplit(left)
    b = urlsplit(right)
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())
