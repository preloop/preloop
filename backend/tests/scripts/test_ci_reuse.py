"""Adversarial tests for trusted same-input CI reuse and required-check behavior."""

from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "ci_reuse", ROOT / "scripts/ci/reuse_tests.py"
)
assert SPEC and SPEC.loader
reuse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reuse)
NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
COMMIT = "a" * 40
DIGEST = "b" * 64
REPO = "example/project"


def job(name: str, steps: tuple[str, ...]) -> dict:
    return {
        "name": name,
        "run_id": 10,
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": step, "status": "completed", "conclusion": "success"}
            for step in steps
        ],
    }


def jobs() -> list[dict]:
    result = [
        job(f"Detect changed paths [merge {COMMIT}]", (f"Verify checkout {COMMIT}",))
    ]
    for required in reuse.SUITES.values():
        for name, steps in required.items():
            result.append(job(f"{name} [inputs {DIGEST}]", steps))
    return result


def run() -> dict:
    return {
        "id": 10,
        "node_id": "WORKFLOW_NODE",
        "run_attempt": 2,
        "event": "pull_request",
        "path": reuse.WORKFLOW,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "pull_requests": [{"number": 42, "head": {"sha": "current-mutable-head"}}],
        "created_at": "2026-09-11T11:00:00Z",
    }


class FakeAPI:
    repo = REPO

    def __init__(self) -> None:
        self.runs = [run()]
        self.jobs = jobs()
        self.trusted = True
        self.same_tree = True
        self.paths: list[str] = []

    def get(self, path: str) -> dict:
        self.paths.append(path)
        if "workflows/" in path:
            return {"workflow_runs": self.runs}
        return {"jobs": self.jobs, "total_count": len(self.jobs)}

    def trusted_workflow(self, run_data: dict) -> bool:
        return self.trusted

    def matching_fingerprint(self, commit: str, digest: str) -> bool:
        assert commit == COMMIT and digest == DIGEST
        return self.same_tree


class ReuseTests(unittest.TestCase):
    def test_only_regular_root_changelog_content_is_excluded(self) -> None:
        def tree(path: bytes, oid: bytes, mode: bytes = b"100644") -> bytes:
            return mode + b" blob " + oid * 40 + b"\t" + path + b"\0"

        base = tree(b"CHANGELOG.md", b"a")
        self.assertEqual(
            reuse.fingerprint_tree(base),
            reuse.fingerprint_tree(tree(b"CHANGELOG.md", b"b")),
        )
        for name in (
            b"README.md",
            b"backend/test.py",
            b"frontend/app.ts",
            b"cli/go.sum",
            b".github/workflows/ci.yml",
            b"scripts/ci/reuse_tests.py",
            b"docs/fixture.md",
        ):
            self.assertNotEqual(
                reuse.fingerprint_tree(base + tree(name, b"a")),
                reuse.fingerprint_tree(base + tree(name, b"b")),
            )
        for other in (
            b"",
            tree(b"CHANGELOG.md", b"a", b"120000"),
            tree(b"CHANGELOG.md", b"a", b"100755"),
        ):
            self.assertNotEqual(
                reuse.fingerprint_tree(base), reuse.fingerprint_tree(other)
            )

    def test_proves_all_suites_and_filters_branch_and_attempt(self) -> None:
        api = FakeAPI()
        found = reuse.find_reuse(api, 42, 11, DIGEST, NOW, "fix/a+b")
        self.assertEqual(set(found), set(reuse.SUITES))
        self.assertIn("branch=fix%2Fa%2Bb", api.paths[0])
        self.assertIn("/attempts/2/jobs", api.paths[1])

    def test_unrelated_failed_run_can_supply_actual_success(self) -> None:
        api = FakeAPI()
        api.runs[0]["conclusion"] = "failure"
        self.assertIn("backend", reuse.find_reuse(api, 42, 11, DIGEST, NOW, "branch"))

    def test_missing_failed_skipped_duplicate_steps_never_prove_backend(self) -> None:
        for change in ("missing", "failure", "skipped", "duplicate", "step_failure"):
            with self.subTest(change=change):
                candidates = jobs()
                target = candidates[1]
                if change == "missing":
                    candidates.remove(target)
                elif change == "duplicate":
                    candidates.append(copy.deepcopy(target))
                elif change == "step_failure":
                    target["steps"][0]["conclusion"] = "failure"
                else:
                    target["conclusion"] = change
                self.assertNotIn("backend", reuse.proven_suites(candidates, DIGEST))
        candidates = jobs()
        next(j for j in candidates if j["name"].startswith("Backend Coverage"))[
            "steps"
        ][0]["conclusion"] = "skipped"
        self.assertNotIn("backend", reuse.proven_suites(candidates, DIGEST))

    def test_windows_continue_on_error_cannot_hide_failed_test(self) -> None:
        candidates = jobs()
        windows = next(j for j in candidates if j["name"].startswith("CLI Tests"))
        windows["steps"][-1]["conclusion"] = "failure"
        self.assertNotIn("cli", reuse.proven_suites(candidates, DIGEST))

    def test_forged_marker_wrong_workflow_or_changed_merge_tree_reruns(self) -> None:
        for trusted, same in ((False, True), (True, False)):
            api = FakeAPI()
            api.trusted, api.same_tree = trusted, same
            self.assertEqual(reuse.find_reuse(api, 42, 11, DIGEST, NOW, "branch"), {})

    def test_other_pr_fork_stale_canceled_and_current_run_are_rejected(self) -> None:
        for mutate in (
            lambda r: r.update(id=11),
            lambda r: r.update(conclusion="cancelled"),
            lambda r: r.update(created_at="2026-09-09T00:00:00Z"),
            lambda r: r.update(pull_requests=[{"number": 99}]),
            lambda r: r.update(head_repository={"full_name": "attacker/fork"}),
            lambda r: r.update(path=".github/workflows/other.yml"),
        ):
            api = FakeAPI()
            mutate(api.runs[0])
            self.assertEqual(reuse.find_reuse(api, 42, 11, DIGEST, NOW, "branch"), {})

    def test_executed_workflow_source_is_bound_to_run_repo_path_and_bytes(self) -> None:
        api = reuse.GitHub(REPO, "synthetic-read-only-token")
        node = {
            "databaseId": 10,
            "file": {
                "path": reuse.WORKFLOW,
                "repositoryFileUrl": f"https://github.com/{REPO}/blob/{COMMIT}/{reuse.WORKFLOW}",
            },
        }
        with (
            patch.object(api, "read_json", return_value={"data": {"node": node}}),
            patch.object(
                reuse,
                "git",
                side_effect=[b"", b"trusted workflow", b"trusted workflow"],
            ),
        ):
            self.assertTrue(api.trusted_workflow(run()))
        for mutate in (
            lambda n: n.update(databaseId=99),
            lambda n: n["file"].update(path="other.yml"),
            lambda n: n["file"].update(
                repositoryFileUrl=f"https://github.com/attacker/fork/blob/{COMMIT}/{reuse.WORKFLOW}"
            ),
            lambda n: n["file"].update(
                repositoryFileUrl=f"https://github.com/{REPO}/blob/main/{reuse.WORKFLOW}"
            ),
        ):
            bad = copy.deepcopy(node)
            mutate(bad)
            with (
                patch.object(api, "read_json", return_value={"data": {"node": bad}}),
                patch.object(reuse, "git") as git,
            ):
                self.assertFalse(api.trusted_workflow(run()))
                git.assert_not_called()
        with (
            patch.object(api, "read_json", return_value={"data": {"node": node}}),
            patch.object(
                reuse,
                "git",
                side_effect=[b"", b"forged historical workflow", b"trusted workflow"],
            ),
        ):
            self.assertFalse(api.trusted_workflow(run()))

    def test_fresh_events_forks_reruns_and_api_failures_do_not_reuse(self) -> None:
        event = {
            "number": 42,
            "pull_request": {
                "head": {"repo": {"full_name": REPO}, "ref": "branch"},
                "base": {"repo": {"full_name": REPO}},
            },
        }
        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "11",
            "GH_TOKEN": "synthetic",
        }
        for event_name in ("push", "workflow_dispatch"):
            with patch.object(reuse, "find_reuse") as find:
                self.assertEqual(
                    reuse.plan(event, {**env, "GITHUB_EVENT_NAME": event_name}, DIGEST),
                    {},
                )
                find.assert_not_called()
        with patch.object(reuse, "find_reuse") as find:
            self.assertEqual(
                reuse.plan(event, {**env, "GITHUB_RUN_ATTEMPT": "2"}, DIGEST), {}
            )
            find.assert_not_called()
        for error in (
            ValueError("malformed"),
            AttributeError("null"),
            reuse.URLError("denied"),
        ):
            with patch.object(reuse, "find_reuse", side_effect=error):
                self.assertEqual(reuse.plan(event, env, DIGEST), {})
        for value in (None, [None], {}, "bad"):
            with self.assertRaises(ValueError):
                reuse.records({"jobs": value}, "jobs")

    def test_workflow_pins_effective_commit_and_manual_does_not_publish(self) -> None:
        workflow = yaml.safe_load((ROOT / reuse.WORKFLOW).read_text())
        all_jobs = workflow["jobs"]
        for name in (
            "changes",
            "test-backend",
            "test-backend-coverage",
            "test-frontend",
            "test-runtime-plugins",
            "test-cli-windows",
        ):
            checkout = next(
                step
                for step in all_jobs[name]["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
            )
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertEqual(
            all_jobs["build-and-push"]["if"], "github.event_name == 'push'"
        )

    def test_real_ci_gate_rejects_unproven_skip_and_accepts_verified_reuse(
        self,
    ) -> None:
        workflow = yaml.safe_load((ROOT / reuse.WORKFLOW).read_text())
        script = workflow["jobs"]["ci"]["steps"][0]["run"]
        env = {
            **os.environ,
            **dict.fromkeys(
                (
                    "CHANGES",
                    "LINT",
                    "HELM",
                    "REQRESOLVE",
                    "BACKEND",
                    "COVERAGE",
                    "FRONTEND",
                    "PLUGINS",
                    "CLIVULN",
                ),
                "success",
            ),
            **dict.fromkeys(
                ("EXPECT_BACKEND", "EXPECT_FRONTEND", "EXPECT_PLUGINS"), "true"
            ),
            **dict.fromkeys(
                (
                    "REUSE_BACKEND",
                    "REUSE_FRONTEND",
                    "REUSE_PLUGINS",
                    "EVIDENCE_BACKEND",
                    "EVIDENCE_FRONTEND",
                    "EVIDENCE_PLUGINS",
                ),
                "",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["GITHUB_STEP_SUMMARY"] = str(Path(tmp) / "summary")
            env.update(BACKEND="skipped", COVERAGE="skipped")
            bad = subprocess.run(["bash", "-c", script], env=env, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            env.update(
                REUSE_BACKEND="true",
                EVIDENCE_BACKEND="https://github.com/example/project/actions/runs/10/attempts/2",
            )
            good = subprocess.run(["bash", "-c", script], env=env, capture_output=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn(
                "reused successful", Path(env["GITHUB_STEP_SUMMARY"]).read_text()
            )


if __name__ == "__main__":
    unittest.main()
