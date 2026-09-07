"""Deterministic CRA dossier manifest and content digests.

Platform-owned. Agent result JSON cannot mint human approvals. Blob storage
and evidence receipts belong to the evidence workstream; this module reports
interoperable integration fields only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from preloop.services.product_provenance import (
    VerifiedProductProvenance,
    sha256_digest,
)
from preloop.utils.secret_scrubbing import scrub_secrets

DOSSIER_MANIFEST_SCHEMA = "preloop.cra.dossier_manifest/v1"
_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "authorization",
        "api_key",
        "access_token",
        "approval_token",
        "private_key",
        "credential",
    }
)


class DossierManifestError(ValueError):
    """A recoverable, safe-to-display dossier construction failure."""


def canonical_json(value: Any) -> bytes:
    """UTF-8 JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        stamp = str(value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return stamp
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def content_digest(value: Any) -> str:
    """SHA-256 of canonical JSON for a result, mapping, or manifest body."""
    return sha256_digest(canonical_json(value))


def redact_value(value: Any) -> Any:
    """Drop secrets and scrub residual credential-shaped strings."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_KEYS or any(
                marker in lowered for marker in ("token", "password", "secret")
            ):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return scrub_secrets(value) or value
    return value


def platform_approvals_from_records(
    records: Sequence[Any],
) -> list[dict[str, Any]]:
    """Copy immutable platform approval identifiers. Never invent reviewer names.

    Only fields that exist on the ApprovalRequest row are exported. Agent
    result ``reviewer`` / ``approval_id`` values are ignored.
    """
    approvals: list[dict[str, Any]] = []
    for record in records:
        identifier = getattr(record, "id", None)
        if identifier is None:
            continue
        status = str(getattr(record, "status", "") or "")
        decided_by_ai = bool(getattr(record, "decided_by_ai", False))
        auto_reason = getattr(record, "auto_approved_reason", None)
        human = (not decided_by_ai) and auto_reason is None
        reviewer = None
        responses = getattr(record, "responses", None)
        if human and status in {"approved", "declined"} and isinstance(responses, list):
            for vote in responses:
                if not isinstance(vote, Mapping):
                    continue
                user_id = vote.get("user_id")
                if user_id:
                    reviewer = str(user_id)
                    break
        resolved = getattr(record, "resolved_at", None)
        approvals.append(
            {
                "id": str(identifier),
                "status": status,
                "tool_name": getattr(record, "tool_name", None),
                "reviewer_user_id": reviewer,
                "resolved_at": resolved,
                "decision": status if status in {"approved", "declined"} else None,
                "decided_by_human": human,
                "source": "platform_approval_request",
            }
        )
    approvals.sort(key=lambda item: item["id"])
    return approvals


def build_dossier_manifest(
    *,
    execution_id: str,
    result: Mapping[str, Any] | None,
    provenance: VerifiedProductProvenance | None,
    artifact_refs: Mapping[str, Any] | None,
    approvals: Sequence[Any] = (),
    publication: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic, redacted dossier manifest.

    Evidence blob storage and receipts are owned by the evidence workstream.
    ``evidence_integration`` reports the fields that workstream should bind.
    """
    try:
        UUID(str(execution_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DossierManifestError("Dossier requires the execution UUID") from exc
    generated = generated_at or datetime.now(timezone.utc)
    safe_result = redact_value(dict(result or {}))
    safe_artifacts = redact_value(dict(artifact_refs or {}))
    safe_publication = redact_value(dict(publication or {})) if publication else None
    platform_approvals = redact_value(platform_approvals_from_records(approvals))
    source_inputs: dict[str, Any]
    if provenance is None:
        source_inputs = {
            "status": "legacy_unmapped",
            "note": (
                "No product_provenance mapping was supplied. Legacy single-repo "
                "or artifact-only identity applies. Agent-written SHAs are not "
                "a build attestation."
            ),
        }
    else:
        source_inputs = provenance.as_dict()
    body = {
        "schema": DOSSIER_MANIFEST_SCHEMA,
        "execution_id": str(execution_id),
        "generated_at": generated,
        "result": safe_result,
        "source_inputs": source_inputs,
        "artifact_refs": safe_artifacts,
        "platform_approvals": platform_approvals,
        "publication": safe_publication,
        "disclaimer": (
            "Machine-generated evidence for conformity assessment support. "
            "Not a conformity assessment, certification, or legal advice."
        ),
    }
    digest_input = {
        key: body[key]
        for key in (
            "schema",
            "execution_id",
            "result",
            "source_inputs",
            "artifact_refs",
            "platform_approvals",
            "publication",
            "disclaimer",
        )
    }
    manifest_digest = content_digest(digest_input)
    result_digest = content_digest(safe_result)
    content = {
        "result_digest": result_digest,
        "source_input_digest": content_digest(source_inputs),
        "artifact_refs_digest": content_digest(safe_artifacts),
        "approvals_digest": content_digest(platform_approvals),
        "publication_digest": content_digest(safe_publication)
        if safe_publication is not None
        else None,
    }
    content_hash = content_digest(content)
    body["digests"] = {
        "manifest": manifest_digest,
        "content": content_hash,
        **content,
    }
    body["evidence_integration"] = {
        "artifact_kind": "cra_evidence",
        "execution_id": str(execution_id),
        "manifest_digest": manifest_digest,
        "content_digest": content_hash,
        "result_digest": result_digest,
        "blob_storage": "evidence_workstream",
        "receipt": None,
        "note": (
            "Blob bytes, availability, and retention receipts are owned by the "
            "evidence workstream. This manifest is the interoperable identity."
        ),
    }
    parsed = json.loads(canonical_json(body).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise DossierManifestError("Dossier canonicalization failed")
    return parsed
