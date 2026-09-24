"""Platform measurement of NTIA minimum elements from SBOM bytes."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

from preloop.cra.sbom_measure import measure_document, measure_inputs, measure_trigger

_TS = "2026-01-01T00:00:00Z"


def _cdx(
    components: list[dict[str, Any]],
    *,
    dependencies: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> bytes:
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": metadata
        or {
            "timestamp": _TS,
            "authors": [{"name": "Example Author"}],
            "component": {
                "type": "application",
                "name": "example-app",
                "bom-ref": "app",
                "supplier": {"name": "Root Supplier"},
            },
        },
        "components": components,
        "dependencies": dependencies or [],
    }
    return json.dumps(document).encode()


def _component(
    ref: str,
    *,
    supplier: str | None = None,
    author: str | None = None,
    purl: str | None = None,
    nested: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "library",
        "name": ref,
        "version": "1.0.0",
        "bom-ref": ref,
    }
    if supplier is not None:
        body["supplier"] = {"name": supplier}
    if author is not None:
        body["author"] = author
    if purl is not None:
        body["purl"] = purl
    if nested is not None:
        body["components"] = nested
    return body


class TestCycloneDxMinimumElements:
    def test_root_supplier_does_not_cover_components(self) -> None:
        raw = _cdx(
            [_component("lib-a"), _component("lib-b")],
            dependencies=[
                {"ref": "app", "dependsOn": ["lib-a", "lib-b"]},
                {"ref": "lib-a", "dependsOn": []},
                {"ref": "lib-b", "dependsOn": []},
            ],
        )
        measured = measure_inputs([("example.cdx.json", raw)])
        assert measured["components"] == 2
        assert measured["missing_counts"]["supplier"] == 2
        assert measured["author_only"] == 0
        assert measured["missing_supplier_and_author"] == 2
        assert measured["passed"] is False
        assert "supplier" in measured["missing"]

    def test_author_only_is_counted_separately(self) -> None:
        raw = _cdx(
            [_component("lib-a", author="Example Person"), _component("lib-b")],
            dependencies=[
                {"ref": "lib-a", "dependsOn": []},
                {"ref": "lib-b", "dependsOn": []},
            ],
        )
        measured = measure_document("example.cdx.json", raw)
        assert measured["missing_counts"]["supplier"] == 2
        assert measured["author_only"] == 1
        assert measured["missing_supplier_and_author"] == 1

    def test_nested_components_are_counted(self) -> None:
        child = _component("child", supplier="Child Co", purl="pkg:generic/child@1")
        parent = _component(
            "parent", supplier="Parent Co", purl="pkg:generic/parent@1", nested=[child]
        )
        raw = _cdx(
            [parent],
            dependencies=[
                {"ref": "parent", "dependsOn": ["child"]},
                {"ref": "child", "dependsOn": []},
            ],
        )
        measured = measure_inputs([("example.cdx.json", raw)])
        assert measured["components"] == 2
        assert measured["missing_counts"]["supplier"] == 0
        assert measured["passed"] is True

    def test_purl_counts_and_bom_ref_does_not(self) -> None:
        identified = _component("lib-a", supplier="A", purl="pkg:generic/lib-a@1")
        by_cpe = _component("lib-b", supplier="B")
        by_cpe["cpe"] = "cpe:2.3:a:example:lib-b:1.0.0:*:*:*:*:*:*:*"
        ref_only = _component("lib-c", supplier="C")
        raw = _cdx(
            [identified, by_cpe, ref_only],
            dependencies=[
                {"ref": "lib-a", "dependsOn": []},
                {"ref": "lib-b", "dependsOn": []},
                {"ref": "lib-c", "dependsOn": []},
            ],
        )
        measured = measure_inputs([("example.cdx.json", raw)])
        assert measured["missing_counts"]["unique_identifier"] == 1
        assert "unique_identifier" in measured["missing"]

    def test_gzip_input_is_measured(self) -> None:
        raw = gzip.compress(
            _cdx(
                [_component("lib-a")],
                dependencies=[{"ref": "lib-a", "dependsOn": []}],
            )
        )
        measured = measure_document("example.cdx.json.gz", raw)
        assert measured["components"] == 1
        assert measured["sha256"]
        assert measured["parser"] == "cyclonedx"
        assert measured["spec_version"] == "1.6"

    def test_unparseable_input_is_skipped(self) -> None:
        measured = measure_document("example.cdx.json", b"this is not json")
        assert measured["status"] == "skipped"
        assert measured["reason"]


class TestSpdxMinimumElements:
    def test_noassertion_supplier_is_missing(self) -> None:
        document = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "example",
            "documentDescribes": ["SPDXRef-Root"],
            "creationInfo": {
                "created": _TS,
                "creators": ["Person: Example Author"],
            },
            "packages": [
                {
                    "SPDXID": "SPDXRef-Root",
                    "name": "example-app",
                    "versionInfo": "1.0.0",
                    "supplier": "Organization: Root Supplier",
                },
                {
                    "SPDXID": "SPDXRef-Lib",
                    "name": "libexample",
                    "versionInfo": "1.0.0",
                    "supplier": "NOASSERTION",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": "pkg:generic/libexample@1.0.0",
                        }
                    ],
                },
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Root",
                },
                {
                    "spdxElementId": "SPDXRef-Root",
                    "relationshipType": "DEPENDS_ON",
                    "relatedSpdxElement": "SPDXRef-Lib",
                },
            ],
        }
        measured = measure_inputs(
            [("example.spdx.json", json.dumps(document).encode())]
        )
        assert measured["components"] == 1
        assert measured["missing_counts"]["supplier"] == 1
        assert measured["passed"] is False
        assert measured["parser"] == "spdx"


class TestTriggerSeeds:
    def test_no_seeds_are_skipped(self) -> None:
        measured = measure_trigger({"payload": {"product": "example"}})
        assert measured["status"] == "skipped"
        assert "no SBOM seeds" in measured["reason"]

    def test_seed_bytes_are_measured(self) -> None:
        raw = _cdx([_component("lib-a", author="Example Person")])
        trigger = {
            "workspace_files": [
                {
                    "path": "sbom/example.cdx.json",
                    "content_base64": base64.b64encode(raw).decode(),
                }
            ]
        }
        measured = measure_trigger(trigger)
        assert measured["passed"] is False
        assert measured["components"] == 1
        assert measured["author_only"] == 1

    def test_aggregate_is_deterministic(self) -> None:
        first = _cdx([_component("lib-a")])
        second = _cdx([_component("lib-b"), _component("lib-c")])
        once = measure_inputs([("a.cdx.json", first), ("b.cdx.json", second)])
        twice = measure_inputs([("a.cdx.json", first), ("b.cdx.json", second)])
        assert once == twice
        assert once["components"] == 3
