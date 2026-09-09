"""Evidence pack result binding compares complete sanitized agent JSON."""

from __future__ import annotations

from typing import Any

import pytest

from preloop.cra.evidence_pack import (
    PACK_MANIFEST_SCHEMA,
    EvidencePackError,
    accept_evidence_archive,
    archive_sha256,
    ensure_pack_manifest,
    evidence_manifest_context,
    read_pack_manifest,
    results_bind_content,
    verify_pack_manifest,
)

from .conftest import clone, make_evidence_archive


def _accept(archive: bytes, api_result: dict[str, Any], execution_id: str = "exec-1"):
    return accept_evidence_archive(
        archive,
        headers={"content-type": "application/gzip"},
        execution_id=execution_id,
        api_result=api_result,
    )


def test_identical_results_bind(sbomaudit_result: dict[str, Any]) -> None:
    results_bind_content(sbomaudit_result, clone(sbomaudit_result))


def test_controller_annotations_do_not_break_bind(
    sbomaudit_result: dict[str, Any],
) -> None:
    api = clone(sbomaudit_result)
    api["trusted_publication"] = {"url": "https://example.com/pr/1"}
    api["verification"] = {"status": "passed", "source": "sandbox_log"}
    api["provenance"] = {"controller": True}
    api["dossier"] = {"id": "dossier-1"}
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"schema": "preloop.cra.dossier_manifest/v1"}
    api["container_termination"] = {"exit_code": 0}
    results_bind_content(api, clone(sbomaudit_result))
    archive = make_evidence_archive(sbomaudit_result)
    packed = _accept(archive, api)
    assert packed is not None
    assert packed["schema"] == sbomaudit_result["schema"]


def test_actual_controller_annotations_do_not_hide_decision_change(
    duediligence_result: dict[str, Any],
) -> None:
    api = clone(duediligence_result)
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"digest": "abc"}
    results_bind_content(api, clone(duediligence_result))
    packed = clone(duediligence_result)
    packed["decision"] = dict(packed["decision"])
    packed["decision"]["outcome"] = "rejected"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(api, packed)
    archive = make_evidence_archive(packed)
    with pytest.raises(EvidencePackError, match="content does not match"):
        _accept(archive, api)


def test_unknown_agent_field_is_bound(sbomaudit_result: dict[str, Any]) -> None:
    packed = clone(sbomaudit_result)
    packed["agent_extra"] = "invented"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(sbomaudit_result, packed)


def test_rejected_decision_does_not_bind_to_accepted(
    duediligence_result: dict[str, Any],
) -> None:
    packed = clone(duediligence_result)
    packed["decision"] = dict(packed["decision"])
    packed["decision"]["outcome"] = "rejected"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(duediligence_result, packed)
    archive = make_evidence_archive(packed)
    with pytest.raises(EvidencePackError, match="content does not match"):
        _accept(archive, duediligence_result)


def test_gate_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["vuln_scan"] = dict(packed["vuln_scan"])
    packed["vuln_scan"]["gate"] = dict(packed["vuln_scan"]["gate"])
    packed["vuln_scan"]["gate"]["passed"] = False
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_waiver_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["vuln_scan"] = dict(packed["vuln_scan"])
    packed["vuln_scan"]["gate"] = dict(packed["vuln_scan"]["gate"])
    packed["vuln_scan"]["gate"]["waivers_applied"] = [
        {
            "id": "CVE-2024-0001",
            "reason": "invented",
            "author": "agent@example.com",
            "date": "2026-08-20",
        }
    ]
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_gap_register_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["gap_register"] = {"items": [], "history_rows": []}
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_legacy_pack_without_result_json_uses_digest(
    sbomaudit_result: dict[str, Any],
) -> None:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        note = b"# evidence\n"
        info = tarfile.TarInfo("evidence/sbom-verify.md")
        info.size = len(note)
        tar.addfile(info, io.BytesIO(note))
    archive = buf.getvalue()
    digest = archive_sha256(archive)
    packed = accept_evidence_archive(
        archive,
        headers={
            "content-type": "application/gzip",
            "x-preloop-evidence-sha256": digest,
        },
        execution_id="exec-1",
        api_result=sbomaudit_result,
    )
    assert packed is None


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _replace_member(archive: bytes, name: str, body: bytes) -> bytes:
    """Rewrite one member, leaving manifest.json (and the rest) alone."""
    import io
    import tarfile

    buf = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src,
        tarfile.open(fileobj=buf, mode="w:gz") as out,
    ):
        for member in src.getmembers():
            data = body if member.name == name else src.extractfile(member).read()
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestPackManifest:
    """manifest.json makes a pack readable without the execution record."""

    def test_manifest_lists_every_member_with_its_digest(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        import hashlib

        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        manifest = verify_pack_manifest(described)
        assert manifest["schema"] == PACK_MANIFEST_SCHEMA
        names = [member["name"] for member in manifest["members"]]
        assert names == ["evidence/sbom-verify.md", "result.json"]
        note = next(
            member
            for member in manifest["members"]
            if member["name"] == "evidence/sbom-verify.md"
        )
        assert note["sha256"] == hashlib.sha256(b"# CRA evidence member\n").hexdigest()
        assert note["size_bytes"] == len(b"# CRA evidence member\n")

    def test_manifest_carries_input_digests_and_declared_source(self) -> None:
        import base64
        import hashlib

        payload = {
            "workspace_files": [
                {
                    "path": "sbom.json",
                    "content_base64": base64.b64encode(b'{"sbom":1}').decode(),
                }
            ],
            "product_provenance": {
                "repositories": [
                    {"remote": "https://github.com/example/p.git", "sha": "a" * 40}
                ]
            },
        }
        context = evidence_manifest_context(
            {"payload": payload}, execution_id="11111111-1111-4111-8111-111111111111"
        )
        described = ensure_pack_manifest(
            _tar_bytes({"evidence/report.md": b"# report\n"}), context=context
        )
        manifest = verify_pack_manifest(described)
        assert manifest["inputs"] == [
            {
                "path": "sbom.json",
                "size_bytes": 10,
                "sha256": hashlib.sha256(b'{"sbom":1}').hexdigest(),
            }
        ]
        assert manifest["source"]["status"] == "declared"
        assert manifest["source"]["repositories"][0]["commit"] == "a" * 40
        assert manifest["execution_id"] == "11111111-1111-4111-8111-111111111111"

    def test_a_swapped_member_no_longer_verifies(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        tampered = _replace_member(
            described, "evidence/sbom-verify.md", b"# a different report\n"
        )
        with pytest.raises(EvidencePackError, match="does not match its manifest"):
            verify_pack_manifest(tampered)

    def test_a_truncated_member_no_longer_verifies(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        tampered = _replace_member(described, "evidence/sbom-verify.md", b"# CRA evi")
        with pytest.raises(EvidencePackError, match="does not match its manifest"):
            verify_pack_manifest(tampered)

    def test_an_unlisted_member_no_longer_verifies(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        import io
        import tarfile

        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        buf = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(described), mode="r:gz") as src,
            tarfile.open(fileobj=buf, mode="w:gz") as out,
        ):
            for member in src.getmembers():
                out.addfile(member, src.extractfile(member))
            extra = b"# planted\n"
            info = tarfile.TarInfo("evidence/planted.md")
            info.size = len(extra)
            out.addfile(info, io.BytesIO(extra))
        with pytest.raises(EvidencePackError, match="not listed in manifest.json"):
            verify_pack_manifest(buf.getvalue())

    def test_accept_rejects_a_pack_that_contradicts_its_manifest(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        tampered = _replace_member(
            described, "evidence/sbom-verify.md", b"# a different report\n"
        )
        with pytest.raises(EvidencePackError, match="does not match its manifest"):
            _accept(tampered, sbomaudit_result)

    def test_accept_still_takes_a_pack_that_has_no_manifest(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        """Packs captured before this change stay readable."""
        archive = make_evidence_archive(sbomaudit_result)
        assert read_pack_manifest(archive) is None
        assert verify_pack_manifest(archive) is None
        packed = _accept(archive, sbomaudit_result)
        assert packed["schema"] == sbomaudit_result["schema"]

    def test_describing_a_pack_is_idempotent(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        once = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        assert ensure_pack_manifest(once) == once

    def test_a_corrupt_archive_is_returned_unchanged(self) -> None:
        junk = b"\x1f\x8bnot-a-tar-at-all"
        assert ensure_pack_manifest(junk) == junk

    def test_the_receipt_digest_covers_the_described_bytes(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        """#483 semantics: the digest published is the digest of what is kept."""
        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        digest = archive_sha256(described)
        packed = accept_evidence_archive(
            described,
            headers={
                "content-type": "application/gzip",
                "x-preloop-evidence-sha256": digest,
            },
            execution_id="exec-1",
            api_result=sbomaudit_result,
            receipt={
                "status": "available",
                "execution_id": "exec-1",
                "sha256": digest,
                "size_bytes": len(described),
            },
        )
        assert packed["schema"] == sbomaudit_result["schema"]

    def test_result_json_binding_ignores_the_manifest_member(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        described = ensure_pack_manifest(make_evidence_archive(sbomaudit_result))
        packed = _accept(described, sbomaudit_result)
        assert packed == sbomaudit_result
