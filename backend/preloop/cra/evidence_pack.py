"""CRA evidence-pack integrity checks for persist and CI.

Uses ``preloop.services.flow_artifacts`` for gzip/tar validation and
``result.json`` extraction. Download acceptance binds the controller
digest. Packed agent JSON is compared in full after stripping only known
controller-added publication/provenance/dossier/verification metadata.
Unknown agent fields are not ignored. Server ``evidence`` annotations on
the API result are not a substitute for the controller digest.

Packs also carry ``manifest.json`` (:data:`PACK_MANIFEST_SCHEMA`): every
file member with its size and sha256, the digests of the inputs the run
was given, and the source commits that were declared for it. The archive
digest in the receipt proves the pack arrived intact; the manifest is what
makes the pack readable on its own, without the execution record, and it
is what turns a member that was silently truncated or replaced into a
verification failure instead of a plausible-looking report.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from preloop.config import settings
from preloop.cra.schemas import CAPTURE_ERROR_CODES
from preloop.cra.validate import json_in
from preloop.services.flow_artifacts import extract_result_json, validate_archive

EVIDENCE_STATUS_HEADER = "x-preloop-evidence-status"
EVIDENCE_SHA256_HEADER = "x-preloop-evidence-sha256"
AVAILABLE_STATUS = "available"

# Self-description member. Named at the archive root so a reader finds it
# with `tar -xOf pack.tar.gz manifest.json` and never confuses it with an
# agent-authored file under evidence/.
PACK_MANIFEST_NAME = "manifest.json"
PACK_MANIFEST_SCHEMA = "preloop.cra.evidence_manifest/v1"
# A manifest is a short index. Anything larger is not one of ours, and
# reading it is refused rather than trusted.
PACK_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
# Repacking to insert a manifest holds one member at a time plus the
# rebuilt archive. Above this expanded size the pack is left as it arrived:
# a missing manifest is a documentation gap, a rewrite that runs the
# control plane out of memory is an outage.
MANIFEST_REPACK_MAX_EXPANDED_BYTES = 256 * 1024 * 1024

_RESULT_NAMES = frozenset({"result.json", "workspace/result.json"})
# Known controller-added keys. Packed agent JSON is compared in full after
# these are removed from both sides. Unknown agent fields stay bound.
# Provenance writes product_provenance / dossier_manifest (not provenance /
# dossier). Keep both spellings so a renamed annotation cannot silently
# become agent content.
_CONTROLLER_RESULT_KEYS = frozenset(
    {
        "trusted_publication",
        "_private_publication",
        "evidence_upload",
        "verification",
        "verification_reported",
        "container_termination",
        "provenance",
        "dossier",
        "product_provenance",
        "dossier_manifest",
    }
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


def canonical_manifest_json(value: Any) -> bytes:
    """UTF-8 JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _pack_member_digests(archive: bytes) -> list[dict[str, Any]]:
    """Size and sha256 of every file member, sorted by name.

    Streamed: one member is in memory at a time, so a pack near the
    expansion cap does not have to fit in RAM to be described.
    """
    members: list[dict[str, Any]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as stream:
            for member in stream:
                if not member.isfile() or member.name == PACK_MANIFEST_NAME:
                    continue
                body = stream.extractfile(member)
                if body is None:
                    raise EvidencePackError("evidence archive is corrupt")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = body.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
                members.append(
                    {
                        "name": member.name,
                        "size_bytes": size,
                        "sha256": digest.hexdigest(),
                    }
                )
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise EvidencePackError("evidence archive is corrupt") from exc
    members.sort(key=lambda item: str(item["name"]))
    return members


def _manifest_inputs(payload: Any) -> list[dict[str, Any]]:
    """Digest the workspace seeds a run was given, as delivered."""
    import base64

    from preloop.utils.workspace_seed import (
        WorkspaceSeedError,
        parse_workspace_files,
    )

    try:
        seeds = parse_workspace_files(payload if isinstance(payload, dict) else None)
    except WorkspaceSeedError:
        return []
    inputs: list[dict[str, Any]] = []
    for seed in seeds:
        try:
            raw = base64.b64decode(seed.content_base64, validate=True)
        except Exception:
            continue
        inputs.append(
            {
                "path": seed.path,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return inputs


def _manifest_source(payload: Any) -> dict[str, Any]:
    """Declared product source, so a reader knows what was audited.

    ``declared`` is deliberate: this is the caller's mapping, not a build
    attestation. The verified form lives in ``dossier_manifest`` on the
    execution result, which the pack points at by execution id. The mapping
    is read with the same inside-then-beside rule as ``workspace_files``.
    """
    mapping = None
    if isinstance(payload, Mapping):
        try:
            from preloop.services.product_provenance import (
                ProductProvenanceError,
                extract_product_provenance_payload,
            )

            mapping = extract_product_provenance_payload(payload)
        except ProductProvenanceError:
            mapping = None
    if not isinstance(mapping, dict):
        return {"status": "unmapped", "repositories": []}
    raw = mapping.get("repositories")
    repositories: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            remote = entry.get("remote") or entry.get("repository_url")
            commit = entry.get("sha") or entry.get("commit")
            repositories.append(
                {
                    "remote": str(remote) if remote else None,
                    "commit": str(commit) if commit else None,
                    "clone_path": entry.get("clone_path"),
                    "role": entry.get("role"),
                }
            )
    return {"status": "declared", "repositories": repositories}


def evidence_manifest_context(
    trigger_event_data: Any, *, execution_id: Any = None
) -> dict[str, Any]:
    """Control-plane facts to embed in a pack manifest.

    Both producers use this: the container-side packer receives it as JSON
    in the environment, the control plane calls it directly when it repacks
    a legacy archive.
    """
    from preloop.utils.workspace_seed import (
        WorkspaceSeedError,
        workspace_seed_payload,
    )

    # Seeds and the declared source are looked up wherever the caller put
    # them, so a body that puts workspace_files or product_provenance beside
    # payload still records what the run was given. Reading only the nested
    # payload would leave "inputs" empty and "source" unmapped for the
    # staging shape, which is exactly the evidence a reviewer needs.
    try:
        seed_container = workspace_seed_payload(
            dict(trigger_event_data)
            if isinstance(trigger_event_data, Mapping)
            else None
        )
    except WorkspaceSeedError:
        seed_container = None
    return {
        "execution_id": str(execution_id) if execution_id is not None else None,
        "inputs": _manifest_inputs(seed_container),
        "source": _manifest_source(trigger_event_data),
    }


def build_pack_manifest(
    archive: bytes,
    *,
    context: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Describe an archive: member digests, input digests, declared source."""
    members = _pack_member_digests(archive)
    facts = dict(context or {})
    inputs = facts.get("inputs")
    source = facts.get("source")
    stamp = generated_at or datetime.now(timezone.utc)
    manifest = {
        "schema": PACK_MANIFEST_SCHEMA,
        "execution_id": facts.get("execution_id"),
        "generated_at": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "members": members,
        "members_digest": hashlib.sha256(canonical_manifest_json(members)).hexdigest(),
        "inputs": list(inputs) if isinstance(inputs, list) else [],
        "source": dict(source) if isinstance(source, Mapping) else {},
        "note": (
            "sha256 values cover the members of this archive as packed and "
            "the inputs as delivered to the run. Source commits are declared "
            "by the caller, not attested by the platform."
        ),
    }
    return manifest


def read_pack_manifest(archive: bytes) -> Optional[dict[str, Any]]:
    """Return the pack manifest, or None when the archive carries none."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            try:
                member = tar.getmember(PACK_MANIFEST_NAME)
            except KeyError:
                return None
            if not member.isfile() or member.size > PACK_MANIFEST_MAX_BYTES:
                raise EvidencePackError("evidence manifest.json is not readable")
            body = tar.extractfile(member)
            if body is None:
                raise EvidencePackError("evidence manifest.json is not readable")
            parsed = json.loads(body.read())
    except (tarfile.TarError, OSError, UnicodeDecodeError) as exc:
        raise EvidencePackError("evidence archive is corrupt") from exc
    except json.JSONDecodeError as exc:
        raise EvidencePackError("evidence manifest.json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise EvidencePackError("evidence manifest.json is not an object")
    return parsed


def verify_pack_manifest(archive: bytes) -> Optional[dict[str, Any]]:
    """Check every member against ``manifest.json``. None when absent.

    A member whose bytes do not match, a listed member that is missing, and
    a packed member that nothing lists are all failures: each of them means
    the pack no longer describes the run it claims to describe.
    """
    manifest = read_pack_manifest(archive)
    if manifest is None:
        return None
    listed_raw = manifest.get("members")
    if not isinstance(listed_raw, list):
        raise EvidencePackError("evidence manifest.json lists no members")
    listed: dict[str, dict[str, Any]] = {}
    for entry in listed_raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise EvidencePackError("evidence manifest.json has an invalid member")
        listed[entry["name"]] = entry
    actual = {entry["name"]: entry for entry in _pack_member_digests(archive)}
    for name, entry in sorted(listed.items()):
        found = actual.get(name)
        if found is None:
            raise EvidencePackError(
                f"evidence manifest.json lists {name!r}, which is not in the archive"
            )
        if entry.get("sha256") != found["sha256"]:
            raise EvidencePackError(
                f"evidence archive member {name!r} does not match its manifest digest"
            )
        size = entry.get("size_bytes")
        if isinstance(size, int) and size != found["size_bytes"]:
            raise EvidencePackError(
                f"evidence archive member {name!r} does not match its manifest size"
            )
    for name in sorted(actual):
        if name not in listed:
            raise EvidencePackError(
                f"evidence archive member {name!r} is not listed in manifest.json"
            )
    digest = manifest.get("members_digest")
    if isinstance(digest, str) and digest:
        expected = hashlib.sha256(canonical_manifest_json(listed_raw)).hexdigest()
        if digest != expected:
            raise EvidencePackError(
                "evidence manifest.json members_digest does not cover its member list"
            )
    return manifest


def _copy_members(source: bytes, into: tarfile.TarFile) -> int:
    """Stream every member of ``source`` into an open archive."""
    copied = 0
    with tarfile.open(fileobj=io.BytesIO(source), mode="r|gz") as stream:
        for member in stream:
            if member.name == PACK_MANIFEST_NAME:
                continue
            if member.isdir():
                into.addfile(member)
                continue
            if not member.isfile():
                continue
            body = stream.extractfile(member)
            if body is None:
                raise EvidencePackError("evidence archive is corrupt")
            into.addfile(member, body)
            copied += 1
    return copied


def ensure_pack_manifest(
    archive: bytes,
    *,
    context: Optional[Mapping[str, Any]] = None,
    max_bytes: Optional[int] = None,
    generated_at: Optional[datetime] = None,
) -> bytes:
    """Return the archive with ``manifest.json``, adding one if it has none.

    Called before the archive is stored and before its receipt is minted,
    so the receipt digest is the digest of the bytes that are kept: the
    #483 receipt semantics are unchanged, the described bytes and the
    digested bytes are the same bytes.

    Every failure returns the original archive. A pack without a manifest
    is a gap in what a reader can check on their own; a pack this function
    corrupted would be worse than the gap.
    """
    if not archive:
        return archive
    cap = evidence_archive_max_bytes() if max_bytes is None else max_bytes
    try:
        expanded = validate_gzip_tar_archive(archive, max_bytes=cap)
        if expanded > MANIFEST_REPACK_MAX_EXPANDED_BYTES:
            return archive
        if read_pack_manifest(archive) is not None:
            return archive
        manifest = build_pack_manifest(
            archive, context=context, generated_at=generated_at
        )
        body = canonical_manifest_json(manifest)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as out:
            copied = _copy_members(archive, out)
            info = tarfile.TarInfo(PACK_MANIFEST_NAME)
            info.size = len(body)
            info.mode = 0o600
            info.mtime = 0
            out.addfile(info, io.BytesIO(body))
        if not copied:
            return archive
        repacked = buffer.getvalue()
        if not repacked or len(repacked) > cap:
            return archive
        # The rewrite must survive the same gate the original passed: a
        # member count or expansion bound that the extra member pushed over
        # the line means the pack ships as it arrived.
        validate_gzip_tar_archive(repacked, max_bytes=cap)
    except (EvidencePackError, tarfile.TarError, OSError, ValueError):
        return archive
    return repacked


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


def _agent_content(obj: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in obj.items() if key not in _CONTROLLER_RESULT_KEYS
    }


def _content_fingerprint(obj: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _agent_content(obj), sort_keys=True, default=str, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def results_bind_content(
    api_result: Any,
    archive_result: Mapping[str, Any],
) -> None:
    """Bind packed result.json to the persisted result beyond envelope fields.

    Schema/verdict/status/flow must still agree. Remaining sanitized agent
    JSON must fingerprint-match after removing only known controller-added
    publication/provenance/dossier/verification metadata. Unknown fields
    are compared, so a packed rejected decision cannot bind to an accepted
    API result.
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
    # Self-check before any binding: a pack that contradicts its own
    # manifest is not evidence, whatever the transport says about it.
    verify_pack_manifest(archive)
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
