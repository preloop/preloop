"""Release image inventories must cover the delivered platforms and digests."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import image_sbom  # noqa: E402

TAG = "v1.2.3"
SHA = "a" * 40


def spdx() -> dict:
    """Synthetic installed-package inventory with a connected SPDX graph."""
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "fixture-image",
        "documentNamespace": "https://example.com/sbom/fixture",
        "creationInfo": {
            "created": "2026-01-01T00:00:00Z",
            "creators": ["Tool: fixture"],
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-package",
                "name": "fixture-package",
                "versionInfo": "1.2.3",
                "downloadLocation": "NOASSERTION",
            }
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-package",
            }
        ],
    }


def evidence() -> tuple[dict, dict]:
    """BuildKit index shape, including one attestation per architecture."""
    index: dict = {"schemaVersion": 2, "manifests": []}
    sboms = {}
    for platform, char, attestation in (
        ("linux/amd64", "b", "d"),
        ("linux/arm64", "c", "e"),
    ):
        operating_system, arch = platform.split("/")
        index["manifests"].append(
            {
                "digest": "sha256:" + char * 64,
                "platform": {"os": operating_system, "architecture": arch},
            }
        )
        index["manifests"].append(
            {
                "digest": "sha256:" + attestation * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": "sha256:" + char * 64,
                },
            }
        )
        sboms[platform] = {"SPDX": spdx()}
    return index, sboms


class ImageSBOMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.index, self.sboms = evidence()
        self.image = image_sbom.IMAGES["backend"] + "@sha256:" + "f" * 64

    def export(self) -> Path:
        return image_sbom.export_sboms(
            self.directory, "backend", self.image, TAG, SHA, self.index, self.sboms
        )

    def test_export_and_verify_both_platforms(self) -> None:
        manifest = self.export()
        data = image_sbom.verify_manifest(manifest, TAG, SHA)
        self.assertEqual(
            {entry["platform"] for entry in data["sboms"]}, image_sbom.PLATFORMS
        )
        self.assertEqual(len(list(self.directory.glob("*.spdx.json"))), 2)
        self.assertEqual(data["source_sha"], SHA)
        self.assertEqual(data["image"], self.image)

    def test_missing_or_duplicate_architecture_fails(self) -> None:
        self.index["manifests"] = self.index["manifests"][:2]
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.export()
        self.index, _ = evidence()
        self.index["manifests"].append(copy.deepcopy(self.index["manifests"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate image platform"):
            self.export()

    def test_unbound_or_absent_attestations_fail(self) -> None:
        self.index["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = (
            "sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(ValueError, "digest-bound"):
            self.export()

    def test_empty_missing_and_ambiguous_sboms_fail(self) -> None:
        for invalid in (
            {},
            {"linux/amd64": {"SPDX": spdx()}},
            {p: {} for p in image_sbom.PLATFORMS},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                image_sbom.export_sboms(
                    self.directory, "backend", self.image, TAG, SHA, self.index, invalid
                )
        self.sboms["linux/amd64"]["AdditionalSPDXs"] = [spdx()]
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.export()

    def test_malformed_inventory_and_graph_fail(self) -> None:
        for field, value in (
            ("packages", []),
            ("relationships", []),
            ("creationInfo", {}),
            ("spdxVersion", "unknown"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                document = spdx()
                document[field] = value
                image_sbom.validate_spdx(document)
        document = spdx()
        document["relationships"][0]["relatedSpdxElement"] = "SPDXRef-missing"
        with self.assertRaises(ValueError):
            image_sbom.validate_spdx(document)

    def test_stale_tag_or_commit_fails(self) -> None:
        path = self.export()
        for tag, sha in (("v1.2.4", SHA), (TAG, "b" * 40)):
            with (
                self.subTest(tag=tag, sha=sha),
                self.assertRaisesRegex(ValueError, "Stale"),
            ):
                image_sbom.verify_manifest(path, tag, sha)

    def test_tampered_or_missing_inventory_fails(self) -> None:
        path = self.export()
        sbom = next(self.directory.glob("*.spdx.json"))
        sbom.write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            image_sbom.verify_manifest(path, TAG, SHA)
        sbom.unlink()
        with self.assertRaisesRegex(ValueError, "Missing regular"):
            image_sbom.verify_manifest(path, TAG, SHA)

    def test_path_traversal_in_manifest_fails(self) -> None:
        path = self.export()
        document = json.loads(path.read_text())
        document["sboms"][0]["file"] = "../payload.json"
        image_sbom.write_json(path, document)
        with self.assertRaisesRegex(ValueError, "filename"):
            image_sbom.verify_manifest(path, TAG, SHA)

    def test_registry_requires_immutable_reference_and_checks_index_digest(
        self,
    ) -> None:
        with patch.object(image_sbom.subprocess, "check_output") as run:
            with self.assertRaisesRegex(ValueError, "pinned"):
                image_sbom.registry_evidence("ghcr.io/preloop/preloop:latest")
            run.assert_not_called()
        raw = json.dumps(self.index).encode()
        image = image_sbom.IMAGES["backend"] + "@" + image_sbom.digest(raw)
        with patch.object(
            image_sbom.subprocess,
            "check_output",
            side_effect=[raw + b"\n", json.dumps(self.sboms).encode()],
        ) as run:
            self.assertEqual(
                image_sbom.registry_evidence(image), (self.index, self.sboms)
            )
            self.assertTrue(all(image in call.args[0] for call in run.call_args_list))
        with patch.object(image_sbom.subprocess, "check_output", return_value=raw):
            with self.assertRaisesRegex(ValueError, "Index digest"):
                image_sbom.registry_evidence(self.image)

    def test_moved_version_tag_fails(self) -> None:
        with patch.object(
            image_sbom.subprocess,
            "check_output",
            return_value=json.dumps("sha256:" + "0" * 64).encode(),
        ):
            with self.assertRaisesRegex(ValueError, "version tag digest mismatch"):
                image_sbom.verify_tag(self.image, TAG[1:])

    def test_mirror_must_describe_the_same_index(self) -> None:
        path = self.export()
        data = json.loads(path.read_text())
        data["mirrors"] = ["example/preloop@sha256:" + "0" * 64]
        image_sbom.write_json(path, data)
        with self.assertRaisesRegex(ValueError, "Mirror digest mismatch"):
            image_sbom.verify_manifest(path, TAG, SHA)

    def test_oci_export_checks_blob_integrity_without_extraction(self) -> None:
        blobs: dict[str, bytes] = {}

        def add_blob(document: dict) -> dict:
            raw = json.dumps(document).encode()
            digest = image_sbom.digest(raw)
            blobs["blobs/" + digest.replace(":", "/")] = raw
            return {"digest": digest}

        for descriptor in self.index["manifests"]:
            if "annotations" not in descriptor:
                continue
            layer = add_blob(
                {"predicateType": "https://spdx.dev/Document", "predicate": spdx()}
            )
            layer["annotations"] = {
                "in-toto.io/predicate-type": "https://spdx.dev/Document"
            }
            descriptor["digest"] = add_blob({"layers": [layer]})["digest"]
        index = add_blob(self.index)
        blobs["index.json"] = json.dumps({"manifests": [index]}).encode()
        archive = self.directory / "fixture.tar"

        def write_archive() -> None:
            with tarfile.open(archive, "w") as tar:
                for name, raw in blobs.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(raw)
                    tar.addfile(info, io.BytesIO(raw))

        write_archive()
        actual_digest, actual_index, actual_sboms = image_sbom.oci_evidence(archive)
        self.assertEqual(actual_digest, index["digest"])
        self.assertEqual(actual_index, self.index)
        self.assertEqual(actual_sboms, self.sboms)
        blobs["blobs/" + index["digest"].replace(":", "/")] = b"{}"
        write_archive()
        with self.assertRaisesRegex(ValueError, "blob digest mismatch"):
            image_sbom.oci_evidence(archive)


if __name__ == "__main__":
    unittest.main()
