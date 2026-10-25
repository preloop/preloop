"""Tests for the Delivery Report's deterministic fetch-scope and parent checks."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from preloop.services import delivery_report as dr
from preloop.services import delivery_report_checks as checks

MERGE = "m" * 40
PARENT = "p" * 40


def _pr(**overrides: Any) -> Dict[str, Any]:
    pr: Dict[str, Any] = {
        "number": 7,
        "title": "Remove the stub views",
        "url": "https://example.com/acme/app/pull/7",
        "author": "jane-doe",
        "description": "Long body " * 200,
        "merged_at": "2026-10-14T12:00:00+00:00",
        "head_sha": "h" * 40,
        "merge_commit_sha": MERGE,
        "comments": [
            {"author": "john-doe", "body": "nit: rename this", "path": "app/a.py"}
        ],
        "changes": {
            "files_changed": 2,
            "changed_files": [
                {
                    "filename": "app/views.py",
                    "additions": 3,
                    "deletions": 40,
                    "patch": "@@ -1,3 +1,1 @@\n-def stub_view():\n+pass",
                },
                {
                    "filename": "tests/test_views.py",
                    "additions": 8,
                    "deletions": 0,
                    "patch": (
                        "+def test_redirect_keeps_old_bookmarks():\n"
                        "+    assert True\n"
                        "+async def test_stub_route_is_gone():\n"
                    ),
                },
            ],
        },
    }
    pr.update(overrides)
    return pr


class TestTestNames:
    def test_reads_python_go_and_js_names_from_test_files_only(self) -> None:
        pr = _pr(
            changes={
                "changed_files": [
                    {"filename": "tests/test_a.py", "patch": "+def test_one():\n"},
                    {"filename": "cli/a_test.go", "patch": "+func TestTwo(t *T) {\n"},
                    {
                        "filename": "web/a.test.ts",
                        "patch": "+  it('renders three', () => {\n"
                        '+  test.skip("four", () => {\n',
                    },
                    {"filename": "app/a.py", "patch": "+def test_not_a_test():\n"},
                    {"filename": "tests/test_b.py", "patch": " def test_context():\n"},
                ]
            }
        )
        assert checks.added_test_names(pr) == [
            "test_one",
            "TestTwo",
            "renders three",
            "four",
        ]


class TestFetchScope:
    def test_pr_without_criteria_or_markers_gets_no_threads_or_hunks(self) -> None:
        item = checks.bounded_pr_input(_pr())
        assert item["scope"] == {
            "review_threads": False,
            "reason": "no linked criteria and no severity marker",
            "diff_hunks_for": [],
        }
        assert "review_threads" not in item
        assert "diff_hunks" not in item
        assert "description" not in item
        assert item["files"] == [
            {"path": "app/views.py", "lines": 43},
            {"path": "tests/test_views.py", "lines": 8},
        ]
        assert item["test_names"] == [
            "test_redirect_keeps_old_bookmarks",
            "test_stub_route_is_gone",
        ]
        assert item["check_conclusion"] == "unknown"

    def test_closing_an_issue_with_criteria_reads_threads(self) -> None:
        item = checks.bounded_pr_input(
            _pr(), criteria=["Redirects keep old bookmarks working"]
        )
        assert item["scope"]["review_threads"] is True
        assert item["review_threads"][0]["body"] == "nit: rename this"
        # A test name matches the criterion: no hunks.
        assert item["scope"]["diff_hunks_for"] == []

    def test_hunks_only_for_a_criterion_no_test_name_matches(self) -> None:
        item = checks.bounded_pr_input(
            _pr(),
            criteria=[
                "Redirects keep old bookmarks working",
                "Lazy loaders for the stub views are deleted",
            ],
        )
        assert item["scope"]["diff_hunks_for"] == [
            "Lazy loaders for the stub views are deleted"
        ]
        # Source hunks only; test files are covered by their names.
        assert list(item["diff_hunks"]) == ["app/views.py"]
        assert item["diff_hunks"]["app/views.py"][1] == "-def stub_view():"

    @pytest.mark.parametrize(
        "comment",
        [
            {"author": "github-advanced-security[bot]", "body": "Alert"},
            {"author": {"login": "preloop"}, "body": "**HIGH Severity:** XSS"},
            {"author": "preloop", "body": "severity: critical"},
        ],
    )
    def test_severity_or_code_scanning_marker_reads_threads(
        self, comment: Dict[str, Any]
    ) -> None:
        scope = checks.fetch_scope(_pr(comments=[comment]))
        assert scope["review_threads"] is True
        assert scope["reason"] == "severity or code-scanning marker present"

    def test_codeql_workflow_mention_is_not_a_marker(self) -> None:
        pr = _pr(comments=[{"author": "dependabot", "body": "Bumps CodeQL action"}])
        assert checks.fetch_scope(pr)["review_threads"] is False

    def test_high_risk_summary_is_a_marker(self) -> None:
        body = (
            "<!-- PRELOOP_SUMMARY -->\n> **[High]**\n> touches auth\n"
            "<!-- /PRELOOP_SUMMARY -->"
        )
        pr = _pr(description=body, comments=[])
        assert checks.has_severity_marker(pr) is True
        assert checks.last_review_summary(pr) == "> **[High]**\n> touches auth"

    def test_last_summary_falls_back_to_the_newest_comment(self) -> None:
        pr = _pr(
            description="no summary",
            comments=[
                {"body": "<!-- PRELOOP_SUMMARY -->old<!-- /PRELOOP_SUMMARY -->"},
                {"body": "<!-- PRELOOP_SUMMARY -->new<!-- /PRELOOP_SUMMARY -->"},
            ],
        )
        assert checks.last_review_summary(pr) == "new"

    @pytest.mark.parametrize(
        "runs,expected",
        [
            ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], "success"),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}], "failure"),
            ([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}], "pending"),
            ([], "unknown"),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "ACTION_REQUIRED"}], "unknown"),
            ([{"conclusion": "STALE"}], "unknown"),
            ([{"conclusion": "STARTUP_FAILURE"}], "unknown"),
            ([{"conclusion": "NEUTRAL"}, {"conclusion": "SUCCESS"}], "success"),
        ],
    )
    def test_check_conclusion(self, runs: List[Dict[str, Any]], expected: str) -> None:
        assert checks.check_conclusion(_pr(checks=runs)) == expected

    def test_deterministic_report_records_the_scope(self) -> None:
        report = dr.assemble_report(
            window={"from": "2026-10-12T00:00:00Z", "to": "2026-10-19T00:00:00Z"},
            pull_requests=[_pr()],
            issues=[],
        )
        assert report["shipped"][0]["fetch_scope"]["review_threads"] is False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Jane Doe",
            "GIT_AUTHOR_EMAIL": "jane@example.com",
            "GIT_COMMITTER_NAME": "Jane Doe",
            "GIT_COMMITTER_EMAIL": "jane@example.com",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    ).stdout.strip()


@pytest.fixture
def merged_repo(tmp_path: Path) -> Dict[str, str]:
    """A repo whose merge removes a stub; router.ts never named it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "router.ts").write_text("routes = ['home']\n")
    (repo / "lazy.ts").write_text("load('stub-view')\n")
    (repo / "stub-view.ts").write_text("export const stub = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "remove")
    (repo / "stub-view.ts").unlink()
    (repo / "lazy.ts").write_text("\n")
    _git(repo, "commit", "-q", "-am", "remove stub")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge remove", "remove")
    return {"repo": str(repo), "merge": _git(repo, "rev-parse", "HEAD")}


class TestParentReferences:
    def test_file_without_a_reference_needed_no_change(
        self, merged_repo: Dict[str, str]
    ) -> None:
        record = checks.parent_references(
            merged_repo["repo"],
            merged_repo["merge"],
            ["router.ts", "lazy.ts", "gone.ts"],
            ["stub-view"],
        )
        assert record["error"] is None
        assert record["parent_sha"] and record["parent_sha"] != merged_repo["merge"]
        by_path = {p["path"]: p for p in record["paths"]}
        assert by_path["router.ts"]["referenced"] is False
        assert by_path["lazy.ts"]["referenced"] is True
        assert by_path["lazy.ts"]["hits"] == [
            f"{record['parent_sha']}:lazy.ts:1:load('stub-view')"
        ]
        assert by_path["gone.ts"] == {
            "path": "gone.ts",
            "exists": False,
            "referenced": False,
            "hits": [],
        }

    def test_parent_is_the_first_parent_not_the_branch(
        self, merged_repo: Dict[str, str]
    ) -> None:
        # At ^2 (the branch) lazy.ts no longer names the stub; ^1 still does.
        record = checks.parent_references(
            merged_repo["repo"], merged_repo["merge"], ["lazy.ts"], ["stub-view"]
        )
        assert record["paths"][0]["referenced"] is True

    def test_unresolvable_merge_is_an_error(self, merged_repo: Dict[str, str]) -> None:
        record = checks.parent_references(
            merged_repo["repo"], "0" * 40, ["router.ts"], ["stub"]
        )
        assert record["error"] == f"cannot resolve {'0' * 40}^1"
        assert record["paths"] == []

    def test_failed_grep_is_an_error_not_no_reference(self) -> None:
        def runner(args: List[str]) -> subprocess.CompletedProcess[str]:
            code = 128 if args[0] == "grep" else 0
            out = "p" * 40 if args[0] == "rev-parse" else ""
            return subprocess.CompletedProcess(args, code, out, "fatal")

        record = checks.parent_references(
            "/nowhere", MERGE, ["router.ts"], ["stub-view"], runner=runner
        )
        assert record["error"] == "git grep failed on router.ts"
        assert record["paths"] == []

    def test_no_terms_is_an_error(self, merged_repo: Dict[str, str]) -> None:
        record = checks.parent_references(
            merged_repo["repo"], merged_repo["merge"], ["router.ts"], [""]
        )
        assert record["error"] == "no terms to look for"

    def test_script_runs_standalone_from_the_workspace(
        self, merged_repo: Dict[str, str], tmp_path: Path
    ) -> None:
        # The run executes the seeded copy with no platform on the path.
        seeded = tmp_path / "tools" / "delivery_report_checks.py"
        seeded.parent.mkdir()
        seeded.write_bytes(Path(checks.__file__).read_bytes())
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                str(seeded),
                "parent-check",
                "--repo",
                merged_repo["repo"],
                "--merge",
                merged_repo["merge"],
                "--path",
                "router.ts",
                "--term",
                "stub-view",
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        record = json.loads(out.stdout)
        assert record["paths"][0]["referenced"] is False
        pr_file = tmp_path / "pr.json"
        pr_file.write_text(json.dumps(_pr()))
        scoped = subprocess.run(
            [
                sys.executable,
                "-I",
                str(seeded),
                "scope",
                str(pr_file),
                "--criterion",
                "Lazy loaders for the stub views are deleted",
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert json.loads(scoped.stdout)["scope"]["review_threads"] is True


def _record(**overrides: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema": checks.PARENT_CHECK_SCHEMA,
        "merge_sha": MERGE,
        "parent_sha": PARENT,
        "terms": ["stub-view"],
        "paths": [
            {"path": "router.ts", "exists": True, "referenced": False, "hits": []}
        ],
        "error": None,
    }
    record.update(overrides)
    return record


ACCORDINGLY = "Update the route tests (router.ts) accordingly for removed stub-view"


class TestApplyParentChecks:
    def _closed(self, criterion: Dict[str, Any]) -> Dict[str, Any]:
        report = dr.finalize_delivery_report(
            {
                "closed_issues": [
                    {
                        "ref": "acme/app#9",
                        "closing_sha": MERGE,
                        "purpose": {
                            "criteria": [
                                criterion,
                                {"text": "Build drops the chunks", "met": None},
                            ]
                        },
                    }
                ]
            }
        )
        purpose: Dict[str, Any] = report["closed_issues"][0]["purpose"]
        return purpose

    def test_clean_parent_check_makes_the_criterion_met(self) -> None:
        purpose = self._closed(
            {"text": ACCORDINGLY, "met": False, "parent_check": _record()}
        )
        first = purpose["criteria"][0]
        assert first["met"] is True
        assert first["evidence"] == [
            "router.ts: no reference to stub-view at parent pppppppppppp; "
            "nothing needed changing"
        ]
        assert first["parent_check"]["parent_sha"] == PARENT
        # Only the build criterion is left open: an evidence gap, no miss.
        assert purpose["verdict"] == dr.PURPOSE_UNVERIFIED

    def test_accordingly_miss_with_a_reference_stays_a_miss(self) -> None:
        record = _record(
            paths=[
                {
                    "path": "router.ts",
                    "exists": True,
                    "referenced": True,
                    "hits": [f"{PARENT}:router.ts:3:stub-view"],
                }
            ]
        )
        purpose = self._closed(
            {"text": ACCORDINGLY, "met": False, "parent_check": record}
        )
        assert purpose["criteria"][0]["met"] is False
        assert "unchecked" not in purpose["criteria"][0]
        assert purpose["verdict"] == dr.PURPOSE_PARTIAL

    def test_accordingly_miss_without_a_check_is_an_evidence_gap(self) -> None:
        purpose = self._closed({"text": ACCORDINGLY, "met": False})
        assert purpose["criteria"][0]["met"] is None
        assert purpose["criteria"][0]["unchecked"] == "parent-commit check not run"
        assert purpose["verdict"] == dr.PURPOSE_UNVERIFIED

    @pytest.mark.parametrize(
        "record",
        [
            _record(merge_sha="x" * 40),
            _record(error="cannot resolve"),
            _record(parent_sha=None),
            _record(schema="other"),
            _record(paths=[]),
            _record(
                paths=[
                    {
                        "path": "router.ts",
                        "referenced": True,
                        "hits": ["router.ts:1:stub-view"],
                    }
                ]
            ),
        ],
    )
    def test_a_record_that_does_not_clear_keeps_the_miss(
        self, record: Dict[str, Any]
    ) -> None:
        purpose = self._closed(
            {"text": "Old callers are removed", "met": False, "parent_check": record}
        )
        assert purpose["criteria"][0]["met"] is False

    def test_other_misses_are_left_alone(self) -> None:
        purpose = self._closed({"text": "p95 under 1.5 s", "met": False})
        assert purpose["criteria"][0]["met"] is False
        assert purpose["verdict"] == dr.PURPOSE_PARTIAL
        assert "unchecked" not in purpose["criteria"][0]

    def test_audited_issue_is_not_rewritten(self) -> None:
        audit = {
            "issue_ref": "acme/app#9",
            "merge_sha": MERGE,
            "purpose": {"criteria": [{"text": ACCORDINGLY, "met": False}]},
        }
        report = dr.finalize_delivery_report(
            {"closed_issues": [{"ref": "acme/app#9", "closing_sha": MERGE}]},
            merge_audits=[audit],
        )
        assert report["closed_issues"][0]["purpose"]["criteria"][0]["met"] is False


class TestSeed:
    def test_checks_script_is_seeded_into_the_workspace(self) -> None:
        data = dr.with_report_checks_seed({"payload": {}})
        assert data is not None
        seeds = data["payload"]["workspace_files"]
        assert seeds[0]["path"] == "tools/delivery_report_checks.py"
        assert (
            base64.b64decode(seeds[0]["content_base64"])
            == Path(checks.__file__).read_bytes()
        )

    def test_both_seeds_travel_together(self) -> None:
        data = dr.with_merge_audits_seed(
            dr.with_report_checks_seed({"payload": {}}), dr.merge_audits_document([])
        )
        assert data is not None
        assert [s["path"] for s in data["payload"]["workspace_files"]] == [
            "tools/delivery_report_checks.py",
            dr.MERGE_AUDITS_WORKSPACE_PATH,
        ]


@pytest.mark.parametrize("closing_sha", [None, "", "different-merge"])
def test_parent_check_requires_the_known_closing_merge(closing_sha: str | None) -> None:
    report = dr.finalize_delivery_report(
        {
            "closed_issues": [
                {
                    "ref": "acme/app#9",
                    "closing_sha": closing_sha,
                    "purpose": {
                        "criteria": [
                            {
                                "text": ACCORDINGLY,
                                "met": False,
                                "parent_check": _record(),
                            }
                        ]
                    },
                }
            ],
        }
    )
    criterion = report["closed_issues"][0]["purpose"]["criteria"][0]
    assert criterion["met"] is None
    assert criterion["unchecked"] == "parent-commit check not run"


@pytest.mark.parametrize(
    "text, met",
    [
        ("p95 under 1.5 s", False),
        ("Update unrelated.ts accordingly for removed stub-view", None),
        ("Update router.ts accordingly for removed different-view", None),
    ],
)
def test_parent_evidence_must_match_the_criterion(text: str, met: bool | None) -> None:
    purpose = dr.apply_parent_checks(
        {"criteria": [{"text": text, "met": False, "parent_check": _record()}]},
        merge_sha=MERGE,
    )
    assert purpose["criteria"][0]["met"] is met


def test_diff_hunks_have_a_total_item_budget() -> None:
    files = [
        {
            "filename": f"app/file{i}.py",
            "patch": "\n".join("+changed" for _ in range(checks.MAX_HUNK_LINES)),
        }
        for i in range(20)
    ]
    item = checks.bounded_pr_input(
        _pr(changes={"changed_files": files}), criteria=["Latency stays below target"]
    )
    assert (
        sum(len(lines) for lines in item["diff_hunks"].values())
        == checks.MAX_ITEM_HUNK_LINES
    )
    assert item["diff_hunks_truncated"] is True
