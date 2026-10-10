"""Regression checks for evidence that can otherwise overstate pilot results."""

from __future__ import annotations

import copy
import io
import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anthropic_evidence_hook import on_pre_build

from anthropic_conformance import (
    FIXTURE_PATH,
    build_template,
    main,
    run_fixtures,
    validate_evidence,
)


class EvidenceTests(unittest.TestCase):
    """Keep synthetic, incomplete and privacy-unsafe evidence out of claims."""

    def setUp(self) -> None:
        self.fixtures = json.loads(FIXTURE_PATH.read_text())

    def test_all_offline_cases_pass_without_live_claim(self) -> None:
        evidence = run_fixtures(self.fixtures)
        self.assertTrue(all(c["status"] == "pass" for c in evidence["cases"]))
        self.assertEqual(evidence["enforcement_state"], "unverified")
        self.assertIsNone(evidence["test_tenant_ref"])

    def test_forbidden_batch_side_effect_fails(self) -> None:
        fixtures = copy.deepcopy(self.fixtures)
        batch = next(f for f in fixtures if f["id"] == "denied_tool_batch")
        batch["outcome"]["side_effect_count"] = 1
        evidence = run_fixtures(fixtures)
        case = next(c for c in evidence["cases"] if c["id"] == "denied_tool_batch")
        self.assertEqual(case["status"], "fail")

    def test_synthetic_pass_cannot_become_live_verified(self) -> None:
        evidence = run_fixtures(self.fixtures)
        evidence["enforcement_state"] = "verified_for_recorded_surface"
        with self.assertRaises(ValueError):
            validate_evidence(evidence)

    def test_duplicate_and_missing_cases_are_rejected(self) -> None:
        evidence = run_fixtures(self.fixtures)
        evidence["cases"][0] = evidence["cases"][1]
        with self.assertRaisesRegex(ValueError, "case_set"):
            validate_evidence(evidence)

    def test_raw_prompt_or_secret_fields_are_rejected(self) -> None:
        for field in ("prompt", "token", "email", "filename", "arguments"):
            evidence = run_fixtures(self.fixtures)
            evidence["cases"][0][field] = "must-not-be-accepted"
            with self.assertRaisesRegex(ValueError, "schema_invalid"):
                validate_evidence(evidence)

    def test_live_pass_requires_artifact_and_batch_observation(self) -> None:
        evidence = build_template("live_application", "test-tenant-001")
        case = next(c for c in evidence["cases"] if c["id"] == "denied_tool_batch")
        case.update(status="pass", observations=["side_effects_absent"])
        with self.assertRaisesRegex(ValueError, "zero_side_effects"):
            validate_evidence(evidence)
        case["side_effect_count"] = 0
        with self.assertRaisesRegex(ValueError, "artifact"):
            validate_evidence(evidence)
        case["artifact_refs"] = ["artifact-001"]
        validate_evidence(evidence)

    def test_pending_template_cannot_claim_verified_surface(self) -> None:
        evidence = build_template("live_application", "test-tenant-001")
        evidence["enforcement_state"] = "verified_for_recorded_surface"
        with self.assertRaises(ValueError):
            validate_evidence(evidence)

    def test_mixed_protocol_application_evidence_is_rejected(self) -> None:
        evidence = build_template("live_application", "test-tenant-001")
        evidence["cases"][0]["evidence_kind"] = "synthetic_protocol"
        with self.assertRaises(ValueError):
            validate_evidence(evidence)

    def test_live_cli_requires_all_operator_gates_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch("sys.argv", ["harness", "--live", "--output", str(output)]):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(output.exists())

    def test_live_cli_rejects_imported_protocol_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.json"
            output = Path(directory) / "report.json"
            source.write_text(json.dumps(run_fixtures(self.fixtures)))
            argv = [
                "harness",
                "--live",
                "--test-tenant-ref",
                "test-tenant-001",
                "--harmless-fixtures",
                "--evidence",
                str(source),
                "--output",
                str(output),
            ]
            with patch("sys.argv", argv):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(output.exists())

    def test_live_cli_handles_unreadable_or_nonobject_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "evidence.json"
            output = Path(directory) / "report.json"
            payloads = (None, b"{", b"\xff", b"[]", b"null", b"42", b'"text"')
            for payload in payloads:
                with self.subTest(payload=payload):
                    if payload is not None:
                        source.write_bytes(payload)
                    argv = [
                        "harness",
                        "--live",
                        "--test-tenant-ref",
                        "test-tenant-001",
                        "--harmless-fixtures",
                        "--evidence",
                        str(source),
                        "--output",
                        str(output),
                    ]
                    with (
                        patch("sys.argv", argv),
                        patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    ):
                        with self.assertRaises(SystemExit) as caught:
                            main()
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("evidence file", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())
                    self.assertFalse(output.exists())

    def test_documentation_build_runs_tests_and_cli_with_telemetry_disabled(
        self,
    ) -> None:
        with patch("anthropic_evidence_hook.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            on_pre_build(object())
        self.assertEqual(run.call_count, 2)
        self.assertIn("unittest", run.call_args_list[0].args[0])
        self.assertIn(
            "docs/scripts/anthropic_conformance.py", run.call_args_list[1].args[0]
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["PRELOOP_DISABLE_TELEMETRY"], "true")
        self.assertFalse(Path(run.call_args_list[1].args[0][-1]).parent.exists())

    def test_documentation_build_fails_when_either_guard_fails(self) -> None:
        for codes in ((1,), (0, 1)):
            with (
                self.subTest(codes=codes),
                patch("anthropic_evidence_hook.subprocess.run") as run,
            ):
                run.side_effect = [
                    subprocess.CompletedProcess([], code) for code in codes
                ]
                with self.assertRaisesRegex(RuntimeError, "evidence guards failed"):
                    on_pre_build(object())
                self.assertEqual(run.call_count, len(codes))


if __name__ == "__main__":
    unittest.main()
