"""Regression tests for missing evidence and false-green release audit runs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_sbom  # noqa: E402
import release_audit  # noqa: E402
from test_image_sbom import SHA, TAG, evidence  # noqa: E402


def run_record(**overrides: object) -> dict:
    """Synthetic GitHub run, with no deployment/account identifiers."""
    return {
        "id": 123,
        "run_attempt": 1,
        "head_branch": TAG,
        "head_sha": SHA,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example.com/run/123",
        **overrides,
    }


class ReleaseAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        index, sboms = evidence()
        for name, repository in image_sbom.IMAGES.items():
            image_sbom.export_sboms(
                self.directory,
                name,
                repository + "@sha256:" + "f" * 64,
                TAG,
                SHA,
                index,
                sboms,
            )
        for name in ("backend", "frontend", "cli"):
            image_sbom.write_json(
                self.directory / f"preloop-sbom-{name}-{TAG[1:]}.cdx.json",
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.6",
                    "metadata": {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}],
                    },
                    "components": [
                        {
                            "type": "library",
                            "name": "fixture",
                            "version": "1.2.3",
                            "bom-ref": "fixture",
                            "purl": "pkg:pypi/fixture@1.2.3",
                            "supplier": {"name": "Example"},
                        }
                    ],
                    "dependencies": [{"ref": "fixture", "dependsOn": []}],
                },
            )

    def test_offline_validation_reports_its_limits(self) -> None:
        report = release_audit.validate(self.directory, TAG, SHA)
        self.assertEqual(len(report["images"]), 4)
        self.assertFalse(report["registry_verified"])
        self.assertFalse(report["tag_workflows_verified"])

    def test_missing_image_or_component_cannot_pass(self) -> None:
        path = self.directory / f"preloop-image-scanner-{TAG[1:]}.json"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing release images"):
            release_audit.validate(self.directory, TAG, SHA)
        self.setUp()
        (self.directory / f"preloop-sbom-cli-{TAG[1:]}.cdx.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing component"):
            release_audit.validate(self.directory, TAG, SHA)

    def test_select_run_ignores_main_pr_and_other_tag_even_on_same_commit(self) -> None:
        runs = [
            run_record(id=1),
            run_record(id=2, head_branch="main"),
            run_record(id=3, event="pull_request"),
            run_record(id=4, head_sha="b" * 40),
            run_record(id=5, head_branch="v1.2.4"),
        ]
        self.assertEqual(release_audit.select_run(runs, TAG, SHA)["id"], 1)
        self.assertIsNone(release_audit.select_run(runs[1:], TAG, SHA))

    def test_newest_run_failure_overrides_old_success(self) -> None:
        newest = release_audit.select_run(
            [run_record(id=1), run_record(id=2, conclusion="failure")], TAG, SHA
        )
        with self.assertRaisesRegex(ValueError, "workflow failed"):
            release_audit.require_success(newest)

    def test_pending_failed_skipped_cancelled_are_never_success(self) -> None:
        self.assertFalse(
            release_audit.require_success(
                run_record(status="in_progress", conclusion=None)
            )
        )
        for conclusion in (
            "failure",
            "skipped",
            "cancelled",
            "timed_out",
            "neutral",
            None,
        ):
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                release_audit.require_success(run_record(conclusion=conclusion))

    def test_missing_tag_runs_time_out_without_downloading(self) -> None:
        with (
            patch.object(
                release_audit,
                "api",
                return_value={"workflow_runs": [run_record(head_branch="main")]},
            ),
            patch.object(release_audit.subprocess, "run") as download,
        ):
            with self.assertRaisesRegex(ValueError, "Timed out"):
                release_audit.collect(self.directory, "example/project", TAG, SHA, 0)
            download.assert_not_called()

    def test_code_scanning_findings_block_collect_even_after_successful_analysis(
        self,
    ) -> None:
        responses = [
            {"workflow_runs": [run_record()]} for _ in release_audit.WORKFLOWS
        ] + [[{"number": 1}]]
        with (
            patch.object(release_audit, "api", side_effect=responses),
            patch.object(release_audit.subprocess, "run") as download,
        ):
            with self.assertRaisesRegex(ValueError, "open code-scanning findings"):
                release_audit.collect(self.directory, "example/project", TAG, SHA, 0)
            download.assert_not_called()

    def test_collect_downloads_exact_run_artifacts_and_validates_full_evidence(
        self,
    ) -> None:
        responses = [
            {"workflow_runs": [run_record()]} for _ in release_audit.WORKFLOWS
        ] + [[]]
        with (
            patch.object(release_audit, "api", side_effect=responses),
            patch.object(release_audit.subprocess, "run") as download,
        ):
            release_audit.collect(self.directory, "example/project", TAG, SHA, 0)
            self.assertEqual(download.call_count, 2)
            for call in download.call_args_list:
                self.assertIn("123", call.args[0])
                self.assertTrue(call.kwargs["check"])
        report = release_audit.validate(
            self.directory, TAG, SHA, require_workflows=True
        )
        self.assertTrue(report["tag_workflows_verified"])
        path = self.directory / "release-audit-runs.json"
        data = json.loads(path.read_text())
        data["workflows"]["codeql.yml"]["status"] = "in_progress"
        image_sbom.write_json(path, data)
        with self.assertRaisesRegex(ValueError, "Incomplete tag workflow"):
            release_audit.validate(self.directory, TAG, SHA, require_workflows=True)

    def test_published_sbom_must_equal_release_asset(self) -> None:
        index, sboms = evidence()
        with (
            patch.object(
                release_audit, "registry_evidence", return_value=(index, sboms)
            ),
            patch.object(release_audit, "verify_tag"),
        ):
            report = release_audit.validate(
                self.directory, TAG, SHA, verify_registry=True
            )
            self.assertTrue(report["registry_verified"])
            sboms["linux/arm64"]["SPDX"]["packages"][0]["versionInfo"] = "2.0.0"
            with self.assertRaisesRegex(ValueError, "differs from release asset"):
                release_audit.validate(self.directory, TAG, SHA, verify_registry=True)

    def test_component_minimum_elements_are_remeasured(self) -> None:
        path = self.directory / f"preloop-sbom-backend-{TAG[1:]}.cdx.json"
        data = json.loads(path.read_text())
        del data["components"][0]["supplier"]
        image_sbom.write_json(path, data)
        with self.assertRaisesRegex(ValueError, "minimum elements failed"):
            release_audit.validate(self.directory, TAG, SHA)


if __name__ == "__main__":
    unittest.main()
