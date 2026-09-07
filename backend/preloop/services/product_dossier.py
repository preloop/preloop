"""Deterministic CRA dossier manifest and content digests.

Platform-owned. Agent result JSON cannot mint human approvals. Evidence
bytes and retention are taken from inspect_evidence / load_evidence
receipts (kind=evidence). This module does not invent availability.
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
CONTROL_PLANE_RESULT_KEYS = (
    "product_provenance",
    "dossier_manifest",
    "trusted_publication",
    "_private_publication",
)
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


def strip_control_plane_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Agent-authored result without control-plane annotations."""
    cleaned = dict(result or {})
    for key in CONTROL_PLANE_RESULT_KEYS:
        cleaned.pop(key, None)
    return cleaned


def portable_evidence_fields(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy server-owned evidence receipt fields. Never claim unverified retention."""
    if not isinstance(receipt, Mapping):
        return {
            "kind": "evidence",
            "status": "missing",
            "sha256": None,
            "artifact_id": None,
            "retention_hours": None,
            "integrity_verified": False,
            "retained": False,
        }
    status = str(receipt.get("status") or "missing")
    verified = bool(receipt.get("integrity_verified"))
    digest = receipt.get("sha256") or receipt.get("digest")
    retention = receipt.get("retention_hours")
    if status != "available":
        retention = None
    return {
        "kind": "evidence",
        "status": status,
        "sha256": digest,
        "artifact_id": receipt.get("artifact_id"),
        "execution_id": receipt.get("execution_id"),
        "transport": receipt.get("transport"),
        "retention_hours": retention,
        "integrity_verified": verified,
        "retained": bool(status == "available" and verified and digest),
        "object_lock": False,
        "legal_hold": False,
        "error": receipt.get("error"),
    }


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
    evidence_receipt: Mapping[str, Any] | None = None,
    raw_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, redacted dossier manifest.

    ``raw_result`` is the agent result without control-plane keys.
    ``result`` is the annotated result (provenance/publication) without
    ``dossier_manifest``. Evidence fields come from inspect/load receipts.
    """
    try:
        UUID(str(execution_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DossierManifestError("Dossier requires the execution UUID") from exc
    generated = generated_at or datetime.now(timezone.utc)
    annotated = redact_value(strip_control_plane_result(dict(result or {})))
    if provenance is not None:
        annotated["product_provenance"] = redact_value(provenance.as_dict())
    if publication:
        annotated["trusted_publication"] = redact_value(dict(publication))
    raw = redact_value(
        strip_control_plane_result(
            dict(raw_result) if raw_result is not None else dict(result or {})
        )
    )
    safe_artifacts = redact_value(dict(artifact_refs or {}))
    safe_publication = redact_value(dict(publication or {})) if publication else None
    platform_approvals = redact_value(platform_approvals_from_records(approvals))
    evidence = portable_evidence_fields(evidence_receipt)
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
    raw_digest = content_digest(raw)
    annotated_digest = content_digest(annotated)
    identity = {
        "schema": DOSSIER_MANIFEST_SCHEMA,
        "execution_id": str(execution_id),
        "source_inputs": source_inputs,
        "artifact_refs": safe_artifacts,
        "platform_approvals": platform_approvals,
        "publication": safe_publication,
        "disclaimer": (
            "Machine-generated evidence for conformity assessment support. "
            "Not a conformity assessment, certification, or legal advice."
        ),
        "raw_result_digest": raw_digest,
        "annotated_result_digest": annotated_digest,
    }
    manifest_digest = content_digest(identity)
    content = {
        "raw_result_digest": raw_digest,
        "annotated_result_digest": annotated_digest,
        "source_input_digest": content_digest(source_inputs),
        "artifact_refs_digest": content_digest(safe_artifacts),
        "approvals_digest": content_digest(platform_approvals),
        "publication_digest": content_digest(safe_publication)
        if safe_publication is not None
        else None,
    }
    body = {
        **identity,
        "generated_at": generated,
        "result": annotated,
        "digests": {
            "manifest": manifest_digest,
            "content": content_digest(content),
            **content,
        },
        "evidence": evidence,
    }
    parsed = json.loads(canonical_json(body).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise DossierManifestError("Dossier canonicalization failed")
    return parsed
