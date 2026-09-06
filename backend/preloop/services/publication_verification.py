"""Trusted verifier handoff; intentionally separate from agent result JSON."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from preloop.services.trusted_publisher import PublicationError


@dataclass(frozen=True, slots=True)
class VerifiedPublication:
    """Controller-owned result after verification of the immutable bundle.

    Construct only in the trusted runner/controller adapter after #428 checks
    complete. This type is not a wire schema and has no JSON deserializer.
    """

    execution_id: str
    head_sha: str
    bundle_sha256: str


def require_verified_publication(
    value: object, *, execution_id: str, bundle: bytes
) -> str:
    """Reject sandbox claims and stale/different artifact evidence."""
    if not isinstance(value, VerifiedPublication):
        raise PublicationError(
            "Isolated publication requires controller-issued verification evidence; sandbox result.json is not an attestation"
        )
    if (
        value.execution_id != execution_id
        or value.bundle_sha256 != hashlib.sha256(bundle).hexdigest()
    ):
        raise PublicationError(
            "Verification evidence does not match this execution and exact commit artifact"
        )
    return value.head_sha
