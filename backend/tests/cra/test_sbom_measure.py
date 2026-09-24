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

    def test_a_non_sbom_neighbour_does_not_fail_a_complete_document(self) -> None:
        complete = _cdx(
            [
                _component(
                    "lib-a", supplier="Example Supplier", purl="pkg:generic/lib-a@1"
                )
            ],
            dependencies=[{"ref": "lib-a", "dependsOn": []}],
        )
        measured = measure_inputs(
            [
                ("sbom/app.cdx.json", complete),
                ("sbom/README.md", b"# SBOMs live here\n"),
            ]
        )
        assert measured["passed"] is True
        assert measured["missing"] == []
        assert measured["components"] == 1

    def test_a_promised_json_sbom_that_does_not_parse_fails(self) -> None:
        complete = _cdx(
            [
                _component(
                    "lib-a", supplier="Example Supplier", purl="pkg:generic/lib-a@1"
                )
            ],
            dependencies=[{"ref": "lib-a", "dependsOn": []}],
        )
        measured = measure_inputs(
            [
                ("app.cdx.json", complete),
                ("other.cdx.json", b"not json"),
            ]
        )
        assert measured["passed"] is False

    def test_tag_value_spdx_does_not_fail_the_aggregate(self) -> None:
        complete = _cdx(
            [
                _component(
                    "lib-a", supplier="Example Supplier", purl="pkg:generic/lib-a@1"
                )
            ],
            dependencies=[{"ref": "lib-a", "dependsOn": []}],
        )
        tag_value = b"SPDXVersion: SPDX-2.3\nSPDXID: SPDXRef-DOCUMENT\n"
        measured = measure_inputs(
            [("app.cdx.json", complete), ("build.spdx", tag_value)]
        )
        assert measured["passed"] is True

    def test_gzip_over_the_cap_is_skipped(self) -> None:
        raw = gzip.compress(b"x" * (32 * 1024 * 1024 + 1))
        measured = measure_document("example.cdx.json.gz", raw)
        assert measured["status"] == "skipped"
        assert "size cap" in measured["reason"]

    def test_xml_sbom_is_recorded_as_unmeasured(self) -> None:
        xml = (
            b'<?xml version="1.0"?>'
            b'<bom xmlns="http://cyclonedx.org/schema/bom/1.6"></bom>'
        )
        trigger = {
            "workspace_files": [
                {
                    "path": "sbom/bom.cdx.xml",
                    "content_base64": base64.b64encode(xml).decode(),
                }
            ]
        }
        measured = measure_trigger(trigger)
        assert measured["status"] == "skipped"
        assert "does not measure" in measured["reason"]
        assert "no SBOM seeds" not in measured["reason"]
        assert measured["documents"][0]["path"] == "sbom/bom.cdx.xml"
        assert measured["documents"][0].get("affects_passed") is not True

    def test_tag_value_beside_json_does_not_fail(self) -> None:
        complete = _cdx(
            [
                _component(
                    "lib-a", supplier="Example Supplier", purl="pkg:generic/lib-a@1"
                )
            ],
            dependencies=[{"ref": "lib-a", "dependsOn": []}],
        )
        trigger = {
            "workspace_files": [
                {
                    "path": "sbom/app.cdx.json",
                    "content_base64": base64.b64encode(complete).decode(),
                },
                {
                    "path": "sbom/build.spdx",
                    "content_base64": base64.b64encode(
                        b"SPDXVersion: SPDX-2.3\n"
                    ).decode(),
                },
            ]
        }
        measured = measure_trigger(trigger)
        assert measured["passed"] is True
        assert any(
            item.get("path") == "sbom/build.spdx" for item in measured["documents"]
        )

    def test_comment_prefixed_tag_value_is_recorded(self) -> None:
        body = b"# written by an example toolchain\n\nSPDXVersion: SPDX-2.3\n"
        trigger = {
            "workspace_files": [
                {
                    "path": "docs/legacy.spdx.txt",
                    "content_base64": base64.b64encode(body).decode(),
                }
            ]
        }
        measured = measure_trigger(trigger)
        assert measured["status"] == "skipped"
        assert "does not measure" in measured["reason"]
        assert measured["documents"][0]["path"] == "docs/legacy.spdx.txt"
        preamble = b"<!-- " + (b"x" * 400) + b" -->"
        xml = preamble + b'<bom xmlns="http://cyclonedx.org/schema/bom/1.6"/>'
        xml_trigger = {
            "workspace_files": [
                {
                    "path": "docs/legacy.cdx.txt",
                    "content_base64": base64.b64encode(xml).decode(),
                }
            ]
        }
        xml_measured = measure_trigger(xml_trigger)
        assert "does not measure" in xml_measured["reason"]
        assert xml_measured["documents"][0]["path"] == "docs/legacy.cdx.txt"

    def test_aggregate_is_deterministic(self) -> None:
        first = _cdx([_component("lib-a")])
        second = _cdx([_component("lib-b"), _component("lib-c")])
        once = measure_inputs([("a.cdx.json", first), ("b.cdx.json", second)])
        twice = measure_inputs([("a.cdx.json", first), ("b.cdx.json", second)])
        assert once == twice
        assert once["components"] == 3
