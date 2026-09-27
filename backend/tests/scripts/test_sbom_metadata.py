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


def _property(component: dict) -> str | None:
    properties = component.get("properties")
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == "preloop:types_only":
            value = prop.get("value")
            return value if isinstance(value, str) else None
    return None


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
        self._write_npm(
            modules / "url-only",
            "url-only",
            author={"url": "http://example.test"},
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
                _component("url-only", "pkg:npm/url-only@1.2.3"),
            ],
            "dependencies": [
                {"ref": "example", "dependsOn": []},
                {"ref": "pkg:npm/path-shape@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/string-shape@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/angle-url@1.2.3", "dependsOn": []},
                {"ref": "pkg:npm/url-only@1.2.3", "dependsOn": []},
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

        url_only = by_name["url-only"]
        self.assertEqual(url_only["name"], "http://example.test")
        self.assertEqual(url_only["url"], ["http://example.test"])
        self.assertNotIn("contact", url_only)

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

    def test_types_only_when_the_package_has_no_runtime_files(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "@types" / "node", "@types/node")
        (modules / "@types" / "node" / "index.d.ts").write_text(
            "export {}", encoding="utf-8"
        )
        self._write_npm(modules / "undici-types", "undici-types")
        (modules / "undici-types" / "index.d.ts").write_text(
            "export {}", encoding="utf-8"
        )
        self._write_npm(modules / "@widgets" / "button-types", "@widgets/button-types")
        (modules / "@widgets" / "button-types" / "index.d.ts").write_text(
            "export {}", encoding="utf-8"
        )
        self._write_npm(modules / "runtime-types", "runtime-types")
        (modules / "runtime-types" / "index.js").write_text(
            "module.exports = {}", encoding="utf-8"
        )
        self._write_npm(modules / "left-pad", "left-pad")
        (modules / "left-pad" / "index.js").write_text(
            "module.exports = {}", encoding="utf-8"
        )
        index = sbom_metadata.MetadataIndex([], [modules])
        document = {
            "components": [
                _component("@types/node", "pkg:npm/%40types/node@1.2.3"),
                _component("undici-types", "pkg:npm/undici-types@1.2.3"),
                _component(
                    "@widgets/button-types",
                    "pkg:npm/%40widgets/button-types@1.2.3",
                ),
                _component("runtime-types", "pkg:npm/runtime-types@1.2.3"),
                _component("left-pad", "pkg:npm/left-pad@1.2.3"),
                _component("missing-types", "pkg:npm/missing-types@1.2.3"),
            ]
        }
        sbom_metadata.stamp_types_only(document, index)
        self.assertEqual(_property(document["components"][0]), "true")
        self.assertEqual(_property(document["components"][1]), "true")
        self.assertEqual(_property(document["components"][2]), "true")
        self.assertIsNone(_property(document["components"][3]))
        self.assertIsNone(_property(document["components"][4]))
        self.assertIsNone(_property(document["components"][5]))
        self._assert_cyclonedx_1_6(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "example",
                        "bom-ref": "example",
                    }
                },
                "components": document["components"],
                "dependencies": [
                    {"ref": "example", "dependsOn": []},
                    *[
                        {"ref": item["bom-ref"], "dependsOn": []}
                        for item in document["components"]
                    ],
                ],
            }
        )

    def test_types_only_skips_a_different_installed_version(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "undici-types", "undici-types")
        manifest = modules / "undici-types" / "package.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body["version"] = "9.9.9"
        manifest.write_text(json.dumps(body), encoding="utf-8")
        (modules / "undici-types" / "index.d.ts").write_text(
            "export {}", encoding="utf-8"
        )
        index = sbom_metadata.MetadataIndex([], [modules])
        component = _component("undici-types", "pkg:npm/undici-types@1.2.3")
        sbom_metadata.stamp_types_only({"components": [component]}, index)
        self.assertIsNone(_property(component))

    def test_types_only_skips_shebang_and_executable_files(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "shebang-types", "shebang-types")
        cli = modules / "shebang-types" / "cli"
        cli.write_text("#!/usr/bin/env node\nconsole.log(1)\n", encoding="utf-8")
        self._write_npm(modules / "mode-types", "mode-types")
        tool = modules / "mode-types" / "tool"
        tool.write_text("echo hi\n", encoding="utf-8")
        tool.chmod(0o755)
        index = sbom_metadata.MetadataIndex([], [modules])
        shebang = _component("shebang-types", "pkg:npm/shebang-types@1.2.3")
        executable = _component("mode-types", "pkg:npm/mode-types@1.2.3")
        sbom_metadata.stamp_types_only(
            {"components": [shebang, executable]},
            index,
        )
        self.assertIsNone(_property(shebang))
        self.assertIsNone(_property(executable))

    def test_pypi_license_expression_alias_and_classifier(self) -> None:
        root = self._tmp()
        self._write_metadata(
            root,
            "expr",
            "License-Expression: MIT OR Apache-2.0\n",
        )
        self._write_metadata(root, "alias", "License: Apache 2.0\n")
        self._write_metadata(
            root,
            "classified",
            "Classifier: License :: OSI Approved :: MIT License\n",
        )
        self._write_metadata(
            root,
            "header",
            "License: MIT\n"
            "Classifier: License :: OSI Approved :: Apache Software License\n",
        )
        index = sbom_metadata.MetadataIndex([root], [])
        expression, expression_source = sbom_metadata.derive_license(
            _component("expr", "pkg:pypi/expr@1.2.3"), index
        )
        self.assertEqual(expression_source, "pypi_license_expression")
        self.assertEqual(expression, {"expression": "MIT OR Apache-2.0"})

        alias, alias_source = sbom_metadata.derive_license(
            _component("alias", "pkg:pypi/alias@1.2.3"), index
        )
        self.assertEqual(alias_source, "pypi_license")
        self.assertEqual(alias, {"license": {"id": "Apache-2.0"}})

        classified, classified_source = sbom_metadata.derive_license(
            _component("classified", "pkg:pypi/classified@1.2.3"), index
        )
        self.assertEqual(classified_source, "pypi_classifier")
        self.assertEqual(classified, {"license": {"id": "MIT"}})

        header, header_source = sbom_metadata.derive_license(
            _component("header", "pkg:pypi/header@1.2.3"), index
        )
        self.assertEqual(header_source, "pypi_license")
        self.assertEqual(header, {"license": {"id": "MIT"}})

    def test_pypi_ambiguous_classifiers_stay_unlicensed(self) -> None:
        root = self._tmp()
        self._write_metadata(
            root,
            "both",
            "Classifier: License :: OSI Approved :: MIT License\n"
            "Classifier: License :: OSI Approved :: Apache Software License\n",
        )
        self._write_metadata(
            root,
            "bsd",
            "License: BSD\nClassifier: License :: OSI Approved :: BSD License\n",
        )
        self._write_metadata(
            root,
            "bad-expression",
            "License-Expression: NotAReal-1.0\n"
            "Classifier: License :: OSI Approved :: MIT License\n",
        )
        index = sbom_metadata.MetadataIndex([root], [])
        for name in ("both", "bsd", "bad-expression"):
            component = _component(name, f"pkg:pypi/{name}@1.2.3")
            self.assertIsNone(sbom_metadata.derive_license(component, index), name)
            sbom_metadata.fill_component_licenses({"components": [component]}, index)
            self.assertNotIn("licenses", component, name)
            self.assertIsNone(sbom_metadata.license_source(component), name)

    def test_pypi_license_file_when_headers_do_not_name_one(self) -> None:
        root = self._tmp()
        apache = "Apache License\nVersion 2.0, January 2004\n"
        mit = (
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software.\n"
        )
        self._write_metadata(root, "filed", "License-File: LICENSE\n")
        filed = root / "filed-1.2.3.dist-info" / "licenses"
        filed.mkdir()
        (filed / "LICENSE").write_text(apache, encoding="utf-8")

        self._write_metadata(
            root,
            "noted",
            "License-File: LICENSE\nLicense-File: NOTICE\n",
        )
        noted = root / "noted-1.2.3.dist-info" / "licenses"
        noted.mkdir()
        (noted / "LICENSE").write_text(apache, encoding="utf-8")
        (noted / "NOTICE").write_text("Copyright notice only.\n", encoding="utf-8")

        self._write_metadata(
            root,
            "split",
            "License-File: LICENSE\nLicense-File: LICENSE-MIT\n",
        )
        split = root / "split-1.2.3.dist-info" / "licenses"
        split.mkdir()
        (split / "LICENSE").write_text(apache, encoding="utf-8")
        (split / "LICENSE-MIT").write_text(mit, encoding="utf-8")

        self._write_metadata(
            root,
            "blocked",
            "License-Expression: NotAReal-1.0\nLicense-File: LICENSE\n",
        )
        blocked = root / "blocked-1.2.3.dist-info" / "licenses"
        blocked.mkdir()
        (blocked / "LICENSE").write_text(apache, encoding="utf-8")

        self._write_metadata(root, "escape", "License-File: ../../SECRET\n")
        (root / "SECRET").write_text(apache, encoding="utf-8")

        self._write_metadata(
            root,
            "both-files",
            "Classifier: License :: OSI Approved :: MIT License\n"
            "Classifier: License :: OSI Approved :: Apache Software License\n"
            "License-File: LICENSE\n",
        )
        both = root / "both-files-1.2.3.dist-info" / "licenses"
        both.mkdir()
        (both / "LICENSE").write_text(apache, encoding="utf-8")

        index = sbom_metadata.MetadataIndex([root], [])
        chosen, source = sbom_metadata.derive_license(
            _component("filed", "pkg:pypi/filed@1.2.3"), index
        )
        self.assertEqual(source, "pypi_license_file")
        self.assertEqual(chosen, {"license": {"id": "Apache-2.0"}})

        noted_choice, noted_source = sbom_metadata.derive_license(
            _component("noted", "pkg:pypi/noted@1.2.3"), index
        )
        self.assertEqual(noted_source, "pypi_license_file")
        self.assertEqual(noted_choice, {"license": {"id": "Apache-2.0"}})

        for name in ("split", "blocked", "escape", "both-files"):
            component = _component(name, f"pkg:pypi/{name}@1.2.3")
            self.assertIsNone(sbom_metadata.derive_license(component, index), name)
            sbom_metadata.fill_component_licenses({"components": [component]}, index)
            self.assertNotIn("licenses", component, name)
            self.assertIsNone(sbom_metadata.license_source(component), name)

    def test_npm_license_and_ambiguous_array(self) -> None:
        root = self._tmp()
        modules = root / "node_modules"
        self._write_npm(modules / "single", "single", license="MIT")
        self._write_npm(
            modules / "either",
            "either",
            license="MIT OR Apache-2.0",
        )
        self._write_npm(modules / "old", "old")
        manifest = modules / "old" / "package.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body["licenses"] = [{"type": "ISC"}]
        manifest.write_text(json.dumps(body), encoding="utf-8")
        self._write_npm(modules / "split", "split")
        split = modules / "split" / "package.json"
        split_body = json.loads(split.read_text(encoding="utf-8"))
        split_body["licenses"] = [{"type": "MIT"}, {"type": "Apache-2.0"}]
        split.write_text(json.dumps(split_body), encoding="utf-8")
        self._write_npm(modules / "closed", "closed", license="UNLICENSED")
        index = sbom_metadata.MetadataIndex([], [modules])

        single, single_source = sbom_metadata.derive_license(
            _component("single", "pkg:npm/single@1.2.3"), index
        )
        self.assertEqual(single_source, "npm_license")
        self.assertEqual(single, {"license": {"id": "MIT"}})

        either, either_source = sbom_metadata.derive_license(
            _component("either", "pkg:npm/either@1.2.3"), index
        )
        self.assertEqual(either_source, "npm_license")
        self.assertEqual(either, {"expression": "MIT OR Apache-2.0"})

        old, old_source = sbom_metadata.derive_license(
            _component("old", "pkg:npm/old@1.2.3"), index
        )
        self.assertEqual(old_source, "npm_licenses")
        self.assertEqual(old, {"license": {"id": "ISC"}})

        for name in ("split", "closed"):
            component = _component(name, f"pkg:npm/{name}@1.2.3")
            self.assertIsNone(sbom_metadata.derive_license(component, index), name)
            sbom_metadata.fill_component_licenses({"components": [component]}, index)
            self.assertNotIn("licenses", component, name)

    def test_go_module_license_file_stdlib_and_ambiguous(self) -> None:
        root = self._tmp()
        cache = root / "modcache"
        self._write_go_license(
            cache,
            "github.com/acme/widget@v1.2.3",
            "LICENSE",
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software.\n",
        )
        self._write_go_license(
            cache,
            "github.com/!acme/!widget@v1.2.3",
            "LICENSE",
            "SPDX-License-Identifier: Apache-2.0\n",
        )
        self._write_go_license(
            cache,
            "github.com/acme/custom@v1.2.3",
            "LICENSE",
            "This software is provided under a private agreement.\n",
        )
        dual = cache / "github.com/acme/dual@v1.2.3"
        dual.mkdir(parents=True)
        (dual / "LICENSE").write_text(
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software.\n",
            encoding="utf-8",
        )
        (dual / "LICENSE-APACHE").write_text(
            "Apache License\nVersion 2.0, January 2004\n",
            encoding="utf-8",
        )
        index = sbom_metadata.MetadataIndex([], [], cache)

        widget, widget_source = sbom_metadata.derive_license(
            _component("widget", "pkg:golang/github.com/acme/widget@v1.2.3"),
            index,
        )
        self.assertEqual(widget_source, "go_module_license")
        self.assertEqual(widget, {"license": {"id": "MIT"}})

        nested, nested_source = sbom_metadata.derive_license(
            _component(
                "cmd",
                "pkg:golang/github.com/acme/widget/cmd@v1.2.3",
            ),
            index,
        )
        self.assertEqual(nested_source, "go_module_license")
        self.assertEqual(nested, {"license": {"id": "MIT"}})

        cased, cased_source = sbom_metadata.derive_license(
            _component("Widget", "pkg:golang/github.com/Acme/Widget@v1.2.3"),
            index,
        )
        self.assertEqual(cased_source, "go_module_license")
        self.assertEqual(cased, {"license": {"id": "Apache-2.0"}})

        stdlib, stdlib_source = sbom_metadata.derive_license(
            _component("std", "pkg:golang/std@go1.22.0"), index
        )
        self.assertEqual(stdlib_source, "go_stdlib")
        self.assertEqual(stdlib, {"license": {"id": "BSD-3-Clause"}})

        for name, purl in (
            ("custom", "pkg:golang/github.com/acme/custom@v1.2.3"),
            ("dual", "pkg:golang/github.com/acme/dual@v1.2.3"),
        ):
            component = _component(name, purl)
            self.assertIsNone(sbom_metadata.derive_license(component, index), name)
            sbom_metadata.fill_component_licenses({"components": [component]}, index)
            self.assertNotIn("licenses", component, name)
            self.assertIsNone(sbom_metadata.license_source(component), name)

    def test_header_rules_do_not_collapse_lookalike_text(self) -> None:
        bsd_preamble = (
            "Redistribution and use in source and binary forms, with or "
            "without modification, are permitted provided that the following "
            "conditions are met:\n"
        )
        source_clause = (
            "1. Redistributions of source code must retain the above "
            "copyright notice, this list of conditions and the following "
            "disclaimer.\n"
        )
        binary_clause = (
            "2. Redistributions in binary form must reproduce the above "
            "copyright notice, this list of conditions and the following "
            "disclaimer in the documentation and/or other materials provided "
            "with the distribution.\n"
        )
        one_clause = sbom_metadata.detect_license_text(bsd_preamble + source_clause)
        self.assertEqual(one_clause, {"license": {"id": "BSD-1-Clause"}})
        two_clause = sbom_metadata.detect_license_text(
            bsd_preamble + source_clause + binary_clause
        )
        self.assertEqual(two_clause, {"license": {"id": "BSD-2-Clause"}})
        three_clause = sbom_metadata.detect_license_text(
            bsd_preamble
            + source_clause
            + binary_clause
            + "Neither the name of the copyright holder nor the names of "
            "its contributors may be used to endorse or promote products.\n"
        )
        self.assertEqual(three_clause, {"license": {"id": "BSD-3-Clause"}})
        self.assertIsNone(
            sbom_metadata.detect_license_text(
                "Redistribution and use in source and binary forms, with or "
                "without modification, are permitted.\n"
            )
        )
        self.assertIsNone(
            sbom_metadata.detect_license_text(
                bsd_preamble
                + source_clause
                + binary_clause
                + "The views and conclusions contained in the software and "
                "documentation are those of the authors.\n"
            )
        )
        mit = (
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software and associated documentation "
            "files.\n"
        )
        self.assertEqual(
            sbom_metadata.detect_license_text(mit), {"license": {"id": "MIT"}}
        )
        self.assertIsNone(
            sbom_metadata.detect_license_text(
                mit + "You agree to the following additional conditions.\n"
            )
        )
        self.assertIsNone(
            sbom_metadata.detect_license_text(
                mit + "The software may not be used for surveillance.\n"
            )
        )

    def test_header_matching_ignores_line_wrap(self) -> None:
        wrapped_mit = (
            "Permission is hereby granted, free of charge, to any person\n"
            "obtaining a copy of this software.\n"
        )
        self.assertEqual(
            sbom_metadata.detect_license_text(wrapped_mit),
            {"license": {"id": "MIT"}},
        )
        wrapped_ban = (
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software.\n"
            "The software may not\nbe used for harm.\n"
        )
        self.assertIsNone(sbom_metadata.detect_license_text(wrapped_ban))
        wrapped_isc = (
            "Permission to use, copy, modify, and/or distribute this "
            "software for any\npurpose with or without fee is hereby "
            "granted, provided that the above copyright notice and this "
            "permission notice appear in all copies.\n"
        )
        self.assertEqual(
            sbom_metadata.detect_license_text(wrapped_isc),
            {"license": {"id": "ISC"}},
        )

    def test_golang_without_module_cache_stays_unlicensed(self) -> None:
        index = sbom_metadata.MetadataIndex([], [])
        component = _component("widget", "pkg:golang/github.com/acme/widget@v1.2.3")
        self.assertIsNone(sbom_metadata.derive_license(component, index))
        sbom_metadata.fill_component_licenses({"components": [component]}, index)
        self.assertNotIn("licenses", component)
        self.assertIsNone(sbom_metadata.license_source(component))

        stdlib, source = sbom_metadata.derive_license(
            _component("std", "pkg:golang/std@go1.22.0"), index
        )
        self.assertEqual(source, "go_stdlib")
        self.assertEqual(stdlib, {"license": {"id": "BSD-3-Clause"}})

    def test_main_reads_go_mod_cache_flag(self) -> None:
        import contextlib
        import io

        root = self._tmp()
        cache = root / "modcache"
        self._write_go_license(
            cache,
            "github.com/acme/widget@v1.2.3",
            "LICENSE",
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy of this software.\n",
        )
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\nauthors = [{name = "Example"}]\n',
            encoding="utf-8",
        )
        document = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "metadata": {},
            "components": [
                _component("widget", "pkg:golang/github.com/acme/widget@v1.2.3")
            ],
        }
        sbom = root / "example.cdx.json"
        sbom.write_text(json.dumps(document), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = sbom_metadata.main(
                [
                    "--pyproject",
                    str(pyproject),
                    "--go-mod-cache",
                    str(cache),
                    str(sbom),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn(
            "licence coverage 0.0% -> 100.0% (0/1 -> 1/1)",
            stdout.getvalue(),
        )
        stamped = json.loads(sbom.read_text(encoding="utf-8"))
        widget = stamped["components"][0]
        self.assertEqual(widget["licenses"], [{"license": {"id": "MIT"}}])
        self.assertEqual(sbom_metadata.license_source(widget), "go_module_license")

    def test_existing_declared_license_is_kept(self) -> None:
        root = self._tmp()
        self._write_metadata(root, "widget", "License-Expression: Apache-2.0\n")
        index = sbom_metadata.MetadataIndex([root], [])
        component = _component("widget", "pkg:pypi/widget@1.2.3")
        component["licenses"] = [{"license": {"id": "MIT"}}]
        component["evidence"] = {"licenses": [{"license": {"id": "ISC"}}]}
        sbom_metadata.fill_component_licenses({"components": [component]}, index)
        self.assertEqual(component["licenses"], [{"license": {"id": "MIT"}}])
        self.assertIsNone(sbom_metadata.license_source(component))

    def test_evidence_alone_does_not_count_as_declared(self) -> None:
        root = self._tmp()
        self._write_metadata(root, "widget", "License-Expression: Apache-2.0\n")
        index = sbom_metadata.MetadataIndex([root], [])
        component = _component("widget", "pkg:pypi/widget@1.2.3")
        component["evidence"] = {"licenses": [{"license": {"id": "MIT"}}]}
        document = {"components": [component]}
        before_count, before_pct = sbom_metadata.declared_licence_stats(document)
        self.assertEqual((before_count, before_pct), (0, "0.0%"))
        sbom_metadata.fill_component_licenses(document, index)
        self.assertEqual(component["licenses"], [{"license": {"id": "Apache-2.0"}}])
        self.assertEqual(
            sbom_metadata.license_source(component), "pypi_license_expression"
        )
        after_count, after_pct = sbom_metadata.declared_licence_stats(document)
        self.assertEqual((after_count, after_pct), (1, "100.0%"))
        self._assert_cyclonedx_1_6(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "example",
                        "bom-ref": "example",
                    }
                },
                "components": [
                    component,
                    {
                        **_component("either", "pkg:npm/either@1.2.3"),
                        "licenses": [{"expression": "MIT OR Apache-2.0"}],
                    },
                ],
                "dependencies": [
                    {"ref": "example", "dependsOn": []},
                    {"ref": component["bom-ref"], "dependsOn": []},
                    {"ref": "pkg:npm/either@1.2.3", "dependsOn": []},
                ],
            }
        )

    def test_main_prints_licence_coverage(self) -> None:
        import contextlib
        import io

        root = self._tmp()
        self._write_metadata(root, "widget", "License-Expression: MIT\n")
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\nauthors = [{name = "Example"}]\n',
            encoding="utf-8",
        )
        document = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "metadata": {},
            "components": [
                _component("widget", "pkg:pypi/widget@1.2.3"),
                {
                    **_component("kept", "pkg:npm/kept@1.2.3"),
                    "licenses": [{"license": {"id": "ISC"}}],
                },
            ],
        }
        sbom = root / "example.cdx.json"
        sbom.write_text(json.dumps(document), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = sbom_metadata.main(
                ["--pyproject", str(pyproject), "--python-root", str(root), str(sbom)]
            )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("lic0", text)
        self.assertIn("lic1", text)
        self.assertIn(
            "licence coverage 50.0% -> 100.0% (1/2 -> 2/2)",
            text,
        )
        stamped = json.loads(sbom.read_text(encoding="utf-8"))
        widget = stamped["components"][0]
        self.assertEqual(widget["licenses"], [{"license": {"id": "MIT"}}])
        self.assertEqual(
            sbom_metadata.license_source(widget), "pypi_license_expression"
        )

    def test_classifier_map_uses_spdx_ids(self) -> None:
        unknown = [
            spdx_id
            for spdx_id in sbom_metadata.CLASSIFIER_TO_SPDX.values()
            if spdx_id not in sbom_metadata.SPDX_LICENSE_IDS
        ]
        self.assertEqual(unknown, [])

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

    def _write_metadata(self, root: Path, name: str, extra: str) -> None:
        dist = root / f"{name}-1.2.3.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            f"Name: {name}\nVersion: 1.2.3\n{extra}",
            encoding="utf-8",
        )

    def _write_go_license(
        self, cache: Path, module_dir: str, filename: str, text: str
    ) -> None:
        directory = cache / module_dir
        directory.mkdir(parents=True)
        (directory / filename).write_text(text, encoding="utf-8")

    def _write_npm(
        self,
        directory: Path,
        name: str,
        *,
        author: str | dict | None = None,
        maintainers: list[str] | None = None,
        license: str | None = None,
    ) -> None:
        directory.mkdir(parents=True)
        body: dict = {"name": name, "version": "1.2.3"}
        if author is not None:
            body["author"] = author
        if maintainers is not None:
            body["maintainers"] = maintainers
        if license is not None:
            body["license"] = license
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
