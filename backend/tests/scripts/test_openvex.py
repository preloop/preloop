"""The checked-in OpenVEX documents match the generator and the schema."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "openvex"
    / "openvex-0.2.0.schema.json"
)
FRONTEND_VEX = REPO_ROOT / "security" / "vex" / "preloop-frontend.openvex.json"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SPEC = importlib.util.spec_from_file_location(
    "generate_vex", REPO_ROOT / "scripts" / "generate_vex.py"
)
assert SPEC is not None and SPEC.loader is not None
generate_vex = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_vex)

UNDICI_PURL = "pkg:npm/undici-types@7.16.0"
LODASH_PURL = "pkg:npm/lodash.camelcase@4.3.0"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FrontendOpenVexTest(unittest.TestCase):
    """undici-types and lodash.camelcase statements for the frontend SBOM."""

    def test_document_matches_the_openvex_0_2_0_schema(self) -> None:
        schema = _load(SCHEMA_PATH)
        document = _load(FRONTEND_VEX)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(document), key=lambda item: list(item.path)
        )
        self.assertEqual(errors, [], errors)

    def test_every_statement_has_a_justification(self) -> None:
        document = _load(FRONTEND_VEX)
        self.assertGreaterEqual(len(document["statements"]), 1)
        for statement in document["statements"]:
            justification = statement.get("justification")
            self.assertIsInstance(justification, str)
            self.assertTrue(justification.strip())
            self.assertEqual(statement["status"], "not_affected")
            self.assertTrue(statement.get("impact_statement", "").strip())

    def test_undici_types_statements_name_the_declarations_package(self) -> None:
        document = _load(FRONTEND_VEX)
        undici = [
            statement
            for statement in document["statements"]
            if statement["products"][0]["identifiers"]["purl"] == UNDICI_PURL
        ]
        names = {statement["vulnerability"]["name"] for statement in undici}
        self.assertEqual(names, set(generate_vex.UNDICI_ADVISORIES))
        self.assertEqual(len(undici), 24)
        for statement in undici:
            self.assertEqual(statement["justification"], "vulnerable_code_not_present")
            self.assertIn(".d.ts", statement["impact_statement"])
            self.assertIn("undici runtime", statement["impact_statement"])
            self.assertEqual(statement["products"][0]["@id"], UNDICI_PURL)

    def test_lodash_camelcase_is_not_in_the_execute_path(self) -> None:
        document = _load(FRONTEND_VEX)
        lodash = [
            statement
            for statement in document["statements"]
            if statement["vulnerability"]["name"] == "CVE-2018-3721"
        ]
        self.assertEqual(len(lodash), 1)
        statement = lodash[0]
        self.assertEqual(
            statement["justification"], "vulnerable_code_not_in_execute_path"
        )
        self.assertEqual(statement["products"][0]["identifiers"]["purl"], LODASH_PURL)
        self.assertIn("shipped execute path", statement["impact_statement"])

    def test_checked_in_document_matches_the_generator(self) -> None:
        document = _load(FRONTEND_VEX)
        rebuilt = generate_vex.build_frontend_document(
            document["timestamp"], document["version"]
        )
        self.assertEqual(rebuilt, document)

    def test_release_workflow_attaches_every_vex_document(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("cp security/vex/*.openvex.json sbom/", workflow)
        self.assertIn("cp security/vex/*.openvex.json release-assets/", workflow)


if __name__ == "__main__":
    unittest.main()
