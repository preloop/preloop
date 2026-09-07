"""Product/release mapping validation. Synthetic remotes and digests only."""

from __future__ import annotations

import base64
import hashlib
import os

import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.services.product_provenance import (
    AmbiguousProductMappingError,
    DuplicateProductMappingError,
    MismatchedProductMappingError,
    PRODUCT_PROVENANCE_SCHEMA,
    ProductProvenanceError,
    PublicationCandidate,
    RuntimeProvenanceFacts,
    UnauthorizedProductMappingError,
    authorize_publication_decision,
    extract_product_provenance_payload,
    publication_approval_allows,
    publication_approval_required,
    sha256_digest,
    validate_product_provenance,
)

FIRMWARE = "https://github.com/example/firmware.git"
APP = "https://github.com/example/companion-app.git"
COMPLIANCE = "https://github.com/example/product-compliance.git"
SHA_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA_C = "cccccccccccccccccccccccccccccccccccccccc"
SBOM = b'{"spdxVersion":"SPDX-2.3","name":"example-product"}'


def _mapping(**overrides: object) -> dict:
    body: dict = {
        "schema": PRODUCT_PROVENANCE_SCHEMA,
        "product": {"name": "example-product"},
        "release": {"identifier": "1.4.2", "channel": "supported"},
        "build": {"id": "build-14"},
        "sbom": {
            "digest": sha256_digest(SBOM),
            "path": "sbom/image.spdx.json",
        },
        "repositories": [
            {
                "remote": FIRMWARE,
                "sha": SHA_A,
                "clone_path": "firmware",
                "role": "code",
            },
            {
                "remote": APP,
                "sha": SHA_B,
                "clone_path": "companion-app",
                "role": "code",
            },
            {
                "remote": COMPLIANCE,
                "sha": SHA_C,
                "clone_path": "compliance",
                "role": "compliance",
            },
        ],
    }
    body.update(overrides)
    return body


def _facts(**overrides: object) -> RuntimeProvenanceFacts:
    values = dict(
        authorized_remotes=(FIRMWARE, APP, COMPLIANCE),
        clone_paths=("firmware", "companion-app", "compliance"),
        clone_shas={FIRMWARE: SHA_A, APP: SHA_B, COMPLIANCE: SHA_C},
        sbom_bytes=SBOM,
        sbom_path="sbom/image.spdx.json",
        publication_approval=None,
    )
    values.update(overrides)
    return RuntimeProvenanceFacts(**values)


def test_legacy_absent_mapping_is_none() -> None:
    assert validate_product_provenance(None, _facts()) is None
    assert extract_product_provenance_payload({"payload": {}}) is None


def test_two_code_repos_and_compliance_verify() -> None:
    record = validate_product_provenance(_mapping(), _facts())
    assert record is not None
    assert record.mapping_status == "verified"
    assert record.sbom_status == "verified"
    assert [repo.clone_path for repo in record.repositories] == [
        "firmware",
        "companion-app",
        "compliance",
    ]
    assert all(repo.sha_status == "verified" for repo in record.repositories)
    dumped = record.as_dict()
    assert "cryptographic" in dumped["attestation"]
    assert dumped["sbom"]["digest"] == "sha256:" + hashlib.sha256(SBOM).hexdigest()


def test_mismatched_sha_is_rejected() -> None:
    with pytest.raises(MismatchedProductMappingError, match="observed checkout"):
        validate_product_provenance(
            _mapping(),
            _facts(clone_shas={FIRMWARE: SHA_A, APP: SHA_A, COMPLIANCE: SHA_C}),
        )


def test_mismatched_sbom_digest_is_rejected() -> None:
    mapping = _mapping()
    mapping["sbom"]["digest"] = "sha256:" + "b" * 64
    with pytest.raises(MismatchedProductMappingError, match="SBOM digest"):
        validate_product_provenance(mapping, _facts())


def test_duplicate_remote_is_rejected() -> None:
    mapping = _mapping()
    mapping["repositories"][1]["remote"] = FIRMWARE
    mapping["repositories"][1]["clone_path"] = "other"
    with pytest.raises(DuplicateProductMappingError, match="same remote"):
        validate_product_provenance(mapping, _facts())


def test_two_compliance_repos_are_ambiguous() -> None:
    mapping = _mapping()
    mapping["repositories"][1]["role"] = "compliance"
    with pytest.raises(AmbiguousProductMappingError, match="compliance"):
        validate_product_provenance(mapping, _facts())


def test_remote_outside_account_is_rejected() -> None:
    mapping = _mapping()
    mapping["repositories"][0]["remote"] = "https://github.com/example/other.git"
    with pytest.raises(UnauthorizedProductMappingError, match="outside"):
        validate_product_provenance(mapping, _facts())


def test_product_mode_without_trusted_shas_is_rejected() -> None:
    with pytest.raises(MismatchedProductMappingError, match="trusted checkout"):
        validate_product_provenance(_mapping(), _facts(clone_shas={}))


def test_git_sha_is_not_an_sbom_digest() -> None:
    mapping = _mapping()
    mapping["sbom"]["digest"] = SHA_A
    with pytest.raises(MismatchedProductMappingError, match="sha256"):
        validate_product_provenance(mapping, _facts())


def test_single_repo_legacy_mapping_may_be_unverified() -> None:
    mapping = {
        "schema": PRODUCT_PROVENANCE_SCHEMA,
        "product": "example-product",
        "release": "1.0.0",
        "sbom": {"digest": sha256_digest(SBOM), "path": "sbom/image.spdx.json"},
        "repositories": [
            {
                "remote": FIRMWARE,
                "sha": SHA_A,
                "clone_path": "firmware",
                "role": "code",
            }
        ],
    }
    record = validate_product_provenance(
        mapping,
        _facts(
            authorized_remotes=(FIRMWARE,),
            clone_paths=("firmware",),
            clone_shas={},
        ),
    )
    assert record is not None
    assert record.mapping_status == "declared_unverified"
    assert record.repositories[0].sha_status == "declared_unverified"


def _candidate(
    url: str,
    sha: str,
    *,
    branch: str = "preloop/change",
    base: str = "main",
) -> dict[str, str]:
    return {
        "repository_url": url,
        "branch": branch,
        "base": base,
        "head_sha": sha,
    }


def _approval(*candidates: dict[str, str], **overrides: object) -> object:
    from types import SimpleNamespace

    body = dict(
        status="approved",
        decided_by_ai=False,
        auto_approved_reason=None,
        expires_at=None,
        tool_name="isolated_publication",
        tool_args={
            "action": "isolated_publication",
            "candidates": list(candidates),
        },
    )
    body.update(overrides)
    return SimpleNamespace(**body)


def test_expired_and_unrelated_approvals_do_not_authorize() -> None:
    from datetime import datetime, timedelta, timezone

    expired = _approval(
        _candidate(FIRMWARE, SHA_A),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    unrelated = _approval(_candidate(APP, SHA_B))
    with pytest.raises(ProductProvenanceError, match="human platform approval"):
        authorize_publication_decision(
            [expired, unrelated],
            required=True,
            candidates=[_candidate(FIRMWARE, SHA_A)],
        )
    assert publication_approval_required({"publication_approval": True}) is True


def test_denied_publication_approval_refuses() -> None:
    with pytest.raises(ProductProvenanceError, match="human platform approval"):
        authorize_publication_decision(
            [_approval(_candidate(FIRMWARE, SHA_A), status="declined")],
            required=True,
            candidates=[_candidate(FIRMWARE, SHA_A)],
        )


def test_source_base_approval_does_not_cover_changed_head() -> None:
    with pytest.raises(ProductProvenanceError, match="human platform approval"):
        authorize_publication_decision(
            [_approval(_candidate(FIRMWARE, SHA_A))],
            required=True,
            candidates=[_candidate(FIRMWARE, SHA_B)],
        )


def test_swapped_repo_sha_pairs_do_not_authorize() -> None:
    with pytest.raises(ProductProvenanceError, match="human platform approval"):
        authorize_publication_decision(
            [
                _approval(
                    _candidate(FIRMWARE, SHA_B),
                    _candidate(APP, SHA_A),
                )
            ],
            required=True,
            candidates=[
                _candidate(FIRMWARE, SHA_A),
                _candidate(APP, SHA_B),
            ],
        )


def test_unpaired_repository_and_commit_sets_cannot_authorize() -> None:
    from types import SimpleNamespace

    unpaired = SimpleNamespace(
        status="approved",
        decided_by_ai=False,
        auto_approved_reason=None,
        expires_at=None,
        tool_name="isolated_publication",
        tool_args={
            "action": "isolated_publication",
            "repositories": [FIRMWARE, APP],
            "commits": [SHA_A, SHA_B],
        },
    )
    with pytest.raises(
        ProductProvenanceError, match="unpaired|human platform approval"
    ):
        authorize_publication_decision(
            [unpaired],
            required=True,
            repository_urls=[FIRMWARE, APP],
            commits=[SHA_A, SHA_B],
        )
    with pytest.raises(ProductProvenanceError, match="human platform approval"):
        authorize_publication_decision(
            [unpaired],
            required=True,
            candidates=[
                _candidate(FIRMWARE, SHA_A),
                _candidate(APP, SHA_B),
            ],
        )


def test_approval_covers_remaining_partial_resume_candidates() -> None:
    authorize_publication_decision(
        [_approval(_candidate(FIRMWARE, SHA_A), _candidate(APP, SHA_B))],
        required=True,
        candidates=[_candidate(APP, SHA_B)],
    )
    record = PublicationCandidate(
        repository_url=APP,
        branch="preloop/change",
        base="main",
        head_sha=SHA_B,
    )
    authorize_publication_decision(
        [_approval(_candidate(FIRMWARE, SHA_A), _candidate(APP, SHA_B))],
        required=True,
        candidates=[record],
    )


def test_agent_written_approval_id_is_not_authority() -> None:
    with pytest.raises(ProductProvenanceError, match="not publication policy"):
        publication_approval_allows(
            None, required_id="11111111-1111-4111-8111-111111111111"
        )
    assert publication_approval_required({"publication_approval": "required"}) is True
    assert publication_approval_required({}) is False


def test_unobserved_pin_is_not_verified_checkout() -> None:
    record = validate_product_provenance(
        _mapping(),
        _facts(
            clone_shas={},
            requested_pins={FIRMWARE: SHA_A, APP: SHA_B, COMPLIANCE: SHA_C},
        ),
    )
    assert record is not None
    assert record.mapping_status == "pin_matched"
    assert {repo.sha_status for repo in record.repositories} == {"pin_matched"}
    with pytest.raises(MismatchedProductMappingError, match="Unobserved"):
        validate_product_provenance(
            _mapping(),
            _facts(
                clone_shas={},
                requested_pins={FIRMWARE: SHA_A, APP: SHA_B, COMPLIANCE: SHA_C},
            ),
            require_observed=True,
        )


def test_payload_extract_reads_nested_product_provenance() -> None:
    mapping = extract_product_provenance_payload(
        {"payload": {"product_provenance": _mapping()}}
    )
    assert mapping is not None
    assert mapping["product"]["name"] == "example-product"


def test_workspace_seed_round_trip_digest() -> None:
    encoded = base64.b64encode(SBOM).decode("ascii")
    trigger = {
        "payload": {
            "product_provenance": _mapping(),
            "workspace_files": [
                {"path": "sbom/image.spdx.json", "content_base64": encoded}
            ],
        }
    }
    from preloop.services.product_provenance import facts_from_workspace_files

    data, path = facts_from_workspace_files(trigger, sbom_path="sbom/image.spdx.json")
    assert path == "sbom/image.spdx.json"
    assert data is not None
    assert sha256_digest(data) == sha256_digest(SBOM)
