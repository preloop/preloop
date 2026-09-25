"""Supplier derivation for the release SBOM stamp."""

from __future__ import annotations

import importlib.util
import json
import textwrap
import unittest
from pathlib import Path

from preloop.cra.sbom_measure import measure_inputs

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "sbom_metadata", REPO_ROOT / "scripts" / "sbom_metadata.py"
)
assert SPEC is not None and SPEC.loader is not None
sbom_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sbom_metadata)


def _component(name: str, purl: str, *, author: str | None = None) -> dict:
    body: dict = {
        "type": "library",
        "name": name,
        "version": "1.2.3",
        "purl": purl,
        "bom-ref": purl,
    }
    if author is not None:
        body["author"] = author
    return body


class SupplierDerivationTest(unittest.TestCase):
    """Each offline source, plus a document the platform measurement accepts."""

    def test_pypi_author_from_metadata(self) -> None:
        root = self._tmp()
        self._write_pypi(root, "widget", "1.2.3", author="Ada Lovelace")
        index = sbom_metadata.MetadataIndex([root], [])
        component = _component("widget", "pkg:pypi/widget@1.2.3", author="Ada Lovelace")
        supplier, source = sbom_metadata.derive_supplier(component, index)
        self.assertEqual(source, "package_metadata_author")
        self.assertEqual(supplier["name"], "Ada Lovelace")
        self.assertEqual(component["author"], "Ada Lovelace")

    def test_pypi_maintainer_when_author_is_absent(self) -> None:
        root = self._tmp()
        dist = root / "widget-1.2.3.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Name: widget\nVersion: 1.2.3\nMaintainer: Grace Hopper\n",
            encoding="utf-8",
        )
        index = sbom_metadata.MetadataIndex([root], [])
        supplier, source = sbom_metadata.derive_supplier(
            _component("widget", "pkg:pypi/widget@1.2.3"), index
        )
        self.assertEqual(source, "package_metadata_maintainer")
        self.assertEqual(supplier["name"], "Grace Hopper")

    def test_pypi_pyproject_in_the_wheel_when_metadata_has_no_person(self) -> None:
        root = self._tmp()
        dist = root / "widget-1.2.3.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Name: widget\nVersion: 1.2.3\n", encoding="utf-8"
        )
        (dist / "pyproject.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "widget"
                version = "1.2.3"
                authors = [{name = "Katherine Johnson", email = "kj@example.com"}]
                """
            ),
            encoding="utf-8",
        )
        index = sbom_metadata.MetadataIndex([root], [])
        supplier, source = sbom_metadata.derive_supplier(
            _component("widget", "pkg:pypi/widget@1.2.3"), index
        )
        self.assertEqual(source, "package_metadata_author")
        self.assertEqual(supplier["name"], "Katherine Johnson")
        self.assertEqual(supplier["contact"][0]["email"], "kj@example.com")

    def test_npm_author_maintainer_and_scope(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "left-pad", "left-pad", author="A Person")
        self._write_npm(
            modules / "only-maintainer",
            "only-maintainer",
            maintainers=["Pat Maintainer <pat@example.com>"],
        )
        self._write_npm(modules / "@widgets" / "button", "@widgets/button")
        index = sbom_metadata.MetadataIndex([], [modules])

        author_supplier, author_source = sbom_metadata.derive_supplier(
            _component("left-pad", "pkg:npm/left-pad@1.2.3"), index
        )
        self.assertEqual(author_source, "package_metadata_author")
        self.assertEqual(author_supplier["name"], "A Person")

        maintainer_supplier, maintainer_source = sbom_metadata.derive_supplier(
            _component("only-maintainer", "pkg:npm/only-maintainer@1.2.3"), index
        )
        self.assertEqual(maintainer_source, "package_metadata_maintainer")
        self.assertEqual(maintainer_supplier["name"], "Pat Maintainer")

        scope_supplier, scope_source = sbom_metadata.derive_supplier(
            _component("@widgets/button", "pkg:npm/%40widgets/button@1.2.3"), index
        )
        self.assertEqual(scope_source, "npm_scope")
        self.assertEqual(scope_supplier["name"], "@widgets")

    def test_npm_maintainer_outranks_contributor(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(
            modules / "ranked",
            "ranked",
            maintainers=["Pat Maintainer"],
        )
        manifest = modules / "ranked" / "package.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body["contributors"] = ["Connie Contributor"]
        manifest.write_text(json.dumps(body), encoding="utf-8")
        index = sbom_metadata.MetadataIndex([], [modules])
        supplier, source = sbom_metadata.derive_supplier(
            _component("ranked", "pkg:npm/ranked@1.2.3"), index
        )
        self.assertEqual(source, "package_metadata_maintainer")
        self.assertEqual(supplier["name"], "Pat Maintainer")

    def test_manual_override_for_metadata_with_no_person(self) -> None:
        index = sbom_metadata.MetadataIndex([], [])
        supplier, source = sbom_metadata.derive_supplier(
            _component("lighthouse-logger", "pkg:npm/lighthouse-logger@1.4.2"),
            index,
        )
        self.assertEqual(source, "manual_override")
        self.assertEqual(supplier["name"], "paulirish")

    def test_npm_repository_path_when_no_person_or_scope(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "left-pad", "left-pad")
        manifest = modules / "left-pad" / "package.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body["repository"] = "acme/left-pad"
        manifest.write_text(json.dumps(body), encoding="utf-8")
        index = sbom_metadata.MetadataIndex([], [modules])
        supplier, source = sbom_metadata.derive_supplier(
            _component("left-pad", "pkg:npm/left-pad@1.2.3"), index
        )
        self.assertEqual(source, "module_path")
        self.assertEqual(supplier["name"], "acme")

    def test_go_module_path(self) -> None:
        index = sbom_metadata.MetadataIndex([], [])
        cases = {
            "pkg:golang/std@go1.22.0": "The Go Authors",
            "pkg:golang/golang.org/x/crypto@v0.1.0": "The Go Authors",
            "pkg:golang/github.com/acme/widget@v1.2.3": "acme",
            "pkg:golang/example.com/tools/cmd@v0.1.0": "example.com/tools",
        }
        for purl, expected in cases.items():
            supplier, source = sbom_metadata.derive_supplier(
                _component(purl.split("/")[-1].split("@")[0], purl), index
            )
            self.assertEqual(source, "module_path", purl)
            self.assertEqual(supplier["name"], expected, purl)

    def test_unresolved_does_not_invent_a_purl_namespace(self) -> None:
        index = sbom_metadata.MetadataIndex([], [])
        component = _component("widget", "pkg:pypi/widget@1.2.3")
        self.assertIsNone(sbom_metadata.derive_supplier(component, index))
        sbom_metadata.fill_component_suppliers({"components": [component]}, index)
        self.assertNotIn("supplier", component)
        self.assertEqual(sbom_metadata.supplier_source(component), "unresolved")

    def test_measurement_passes_on_stamped_output(self) -> None:
        root = self._tmp()
        self._write_pypi(
            root, "widget", "1.2.3", author="Ada Lovelace <ada@example.com>"
        )
        modules = root / "node_modules"
        self._write_npm(modules / "left-pad", "left-pad", author="A Person")
        document = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "metadata": {
                "timestamp": "2026-09-25T00:00:00Z",
                "component": {
                    "type": "application",
                    "name": "example",
                    "version": "1.0.0",
                    "bom-ref": "example",
                },
            },
            "components": [
                _component("widget", "pkg:pypi/widget@1.2.3", author="keep-me"),
                _component("left-pad", "pkg:npm/left-pad@1.2.3"),
                _component("crypto", "pkg:golang/golang.org/x/crypto@v0.1.0"),
                _component("widget-go", "pkg:golang/github.com/acme/widget@v1.2.3"),
            ],
            "dependencies": [
                {"ref": "example", "dependsOn": []},
                {"ref": "pkg:pypi/widget@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/left-pad@1.2.3", "dependsOn": []},
                {"ref": "pkg:golang/golang.org/x/crypto@v0.1.0", "dependsOn": []},
                {"ref": "pkg:golang/github.com/acme/widget@v1.2.3", "dependsOn": []},
            ],
        }
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\nauthors = [{name = "Example"}]\n',
            encoding="utf-8",
        )
        authors = sbom_metadata.read_authors(pyproject)
        sbom_metadata.stamp(document, authors)
        sbom_metadata.fill_component_suppliers(
            document, sbom_metadata.MetadataIndex([root], [modules])
        )
        widget = document["components"][0]
        self.assertEqual(widget["author"], "keep-me")
        self.assertEqual(widget["supplier"]["name"], "Ada Lovelace")
        self.assertEqual(
            sbom_metadata.supplier_source(widget), "package_metadata_author"
        )
        golden = {
            "name": "Ada Lovelace",
            "contact": [{"name": "Ada Lovelace", "email": "ada@example.com"}],
        }
        self.assertEqual(widget["supplier"], golden)
        measured = measure_inputs([("example.cdx.json", json.dumps(document).encode())])
        self.assertTrue(measured["passed"], measured)
        self.assertEqual(measured["missing_counts"]["supplier"], 0)

    def test_url_is_not_emitted_as_contact_email(self) -> None:
        """A homepage is supplier.url. Only a real email is contact.email."""
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(
            modules / "path-shape",
            "path-shape",
            author={"name": "Example Person", "url": "http://example.test"},
        )
        self._write_npm(
            modules / "string-shape",
            "string-shape",
            author="Example Person <person@example.com> (http://example.test)",
        )
        self._write_npm(
            modules / "angle-url",
            "angle-url",
            author="Example Person <http://example.test>",
        )
        index = sbom_metadata.MetadataIndex([], [modules])
        document = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
            "version": 1,
            "metadata": {
                "timestamp": "2026-09-25T00:00:00Z",
                "tools": [{"vendor": "example", "name": "fixture", "version": "0"}],
                "component": {
                    "type": "application",
                    "name": "example",
                    "version": "1.0.0",
                    "bom-ref": "example",
                },
            },
            "components": [
                _component("path-shape", "pkg:npm/path-shape@1.2.3"),
                _component("string-shape", "pkg:npm/string-shape@1.2.3"),
                _component("angle-url", "pkg:npm/angle-url@1.2.3"),
            ],
            "dependencies": [
                {"ref": "example", "dependsOn": []},
                {"ref": "pkg:npm/path-shape@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/string-shape@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/angle-url@1.2.3", "dependsOn": []},
            ],
        }
        sbom_metadata.fill_component_suppliers(document, index)
        by_name = {item["name"]: item["supplier"] for item in document["components"]}

        object_supplier = by_name["path-shape"]
        self.assertEqual(object_supplier["url"], ["http://example.test"])
        self.assertNotIn("email", object_supplier.get("contact", [{}])[0])

        string_supplier = by_name["string-shape"]
        self.assertEqual(string_supplier["url"], ["http://example.test"])
        self.assertEqual(string_supplier["contact"][0]["email"], "person@example.com")

        angle_supplier = by_name["angle-url"]
        self.assertEqual(angle_supplier["url"], ["http://example.test"])
        self.assertNotIn("email", json.dumps(angle_supplier))

        self._assert_cyclonedx_1_6(document)

    def test_measure_command_imports_without_third_party_packages(self) -> None:
        import os
        import subprocess
        import sys

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT / "backend")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        probe = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import preloop.cra.__main__ as entry; assert callable(entry.main)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def _tmp(self) -> Path:
        from tempfile import TemporaryDirectory

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def _write_pypi(self, root: Path, name: str, version: str, author: str) -> None:
        dist = root / f"{name}-{version}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            f"Name: {name}\nVersion: {version}\nAuthor: {author}\n",
            encoding="utf-8",
        )

    def _write_npm(
        self,
        directory: Path,
        name: str,
        *,
        author: str | dict | None = None,
        maintainers: list[str] | None = None,
    ) -> None:
        directory.mkdir(parents=True)
        body: dict = {"name": name, "version": "1.2.3"}
        if author is not None:
            body["author"] = author
        if maintainers is not None:
            body["maintainers"] = maintainers
        (directory / "package.json").write_text(json.dumps(body), encoding="utf-8")

    def _assert_cyclonedx_1_6(self, document: dict) -> None:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonStrictValidator

        error = JsonStrictValidator(SchemaVersion.V1_6).validate_str(
            json.dumps(document)
        )
        self.assertIsNone(error, error)


if __name__ == "__main__":
    unittest.main()
