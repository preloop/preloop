"""Pre-check engine and built-in checks (#2541), over a fake reader.

No database and no tracker: repository scope and Preloop's own execution
facts are patched, and the reader returns synthetic state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest
from pydantic import ValidationError

from preloop.models.schemas.flow_precheck import FlowPrecheck
from preloop.services import flow_precheck as engine
from preloop.services.flow_precheck import (
    MAX_CONTEXT_BYTES,
    PrecheckConfigError,
    PrecheckRepository,
    compute_digest,
    evaluate_precheck,
    repositories_for_check,
)
from preloop.services.flow_precheck_reader import (
    BranchCheckState,
    CheckRunState,
    FailedRun,
    IssueState,
    PrecheckBudget,
    PrecheckError,
    PullRequestState,
    ReadPage,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
REPO = "acme/app"
TRACKER = SimpleNamespace(id="tracker-1", tracker_type="github")
REPOSITORY = PrecheckRepository(slug=REPO, tracker_type="github", tracker=TRACKER)


def pr(number: int, **overrides: Any) -> PullRequestState:
    """An open pull request that needs attention by default."""
    base = PullRequestState(
        repository=REPO,
        number=number,
        url=f"https://github.com/{REPO}/pull/{number}",
        title=f"Change {number}",
        author="jane-doe",
        head_sha=f"{number:07x}",
        base_branch="main",
        updated_at=NOW - timedelta(hours=5),
        is_draft=False,
        labels=(),
        review_decision=None,
        mergeable="MERGEABLE",
        checks_state="FAILURE",
        unresolved_threads=0,
    )
    return replace(base, **overrides)


class FakeReader:
    """Answers from fixed state and counts calls."""

    def __init__(
        self,
        *,
        prs: Sequence[PullRequestState] = (),
        branch: Optional[BranchCheckState] = None,
        runs: Sequence[FailedRun] = (),
        issues: Sequence[IssueState] = (),
        error: Optional[PrecheckError] = None,
        delay: float = 0.0,
    ) -> None:
        self.prs, self.branch, self.runs, self.issues = prs, branch, runs, issues
        self.error, self.delay = error, delay
        self.calls: List[Dict[str, Any]] = []

    async def _answer(self, value: Any, **call: Any) -> Any:
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return value

    async def list_open_pull_requests_with_state(
        self, repository: str, *, include_drafts: bool
    ) -> ReadPage:
        items = tuple(p for p in self.prs if include_drafts or not p.is_draft)
        return await self._answer(ReadPage(items=items), kind="prs")

    async def default_branch_check_state(
        self, repository: str, *, branch: str, required_only: bool
    ) -> BranchCheckState:
        return await self._answer(self.branch, kind="branch")

    async def list_failed_ci_runs(
        self, repository: str, *, since: datetime, branches: Sequence[str], limit: int
    ) -> ReadPage:
        items = tuple(r for r in self.runs if r.created_at and r.created_at >= since)
        return await self._answer(ReadPage(items=items), kind="runs", since=since)

    async def list_issues(
        self,
        repository: str,
        *,
        labels_all: Sequence[str],
        milestone: Optional[str],
        unassigned: bool,
    ) -> ReadPage:
        return await self._answer(ReadPage(items=tuple(self.issues)), kind="issues")


def green_branch() -> BranchCheckState:
    return BranchCheckState(
        repository=REPO,
        branch="main",
        head_sha="abc",
        checks=(CheckRunState(name="tests", state="success"),),
    )


def red_branch() -> BranchCheckState:
    return BranchCheckState(
        repository=REPO,
        branch="main",
        head_sha="abc",
        checks=(
            CheckRunState(name="tests", state="failure", url="https://example.com/1"),
            CheckRunState(name="lint", state="success"),
        ),
    )


@pytest.fixture
def facts(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Patch the repository scope and the execution facts the engine reads."""
    state: Dict[str, Any] = {
        "repositories": [REPOSITORY],
        "last_run_at": NOW - timedelta(hours=1),
        "last_real_run_at": NOW - timedelta(hours=1),
        "active": set(),
    }
    monkeypatch.setattr(
        engine,
        "resolve_flow_repositories",
        lambda db, **kwargs: list(state["repositories"]),
    )
    monkeypatch.setattr(
        engine,
        "_read_internal_facts",
        lambda db, flow, needs_active: engine._InternalFacts(
            last_run_at=state["last_run_at"],
            last_real_run_at=state["last_real_run_at"],
            active_pull_request_urls=set(state["active"]),
        ),
    )

    async def inline(operation: Any) -> Any:
        return operation()

    monkeypatch.setattr(engine, "_off_loop", inline)
    return state


def flow_with(precheck: Dict[str, Any]) -> Any:
    return SimpleNamespace(
        id="flow-1",
        account_id="account-1",
        precheck=precheck,
        trigger_project_ids=None,
        git_clone_config=None,
        created_at=NOW - timedelta(days=30),
    )


async def run(
    precheck: Dict[str, Any],
    reader: FakeReader,
    *,
    kind: str = "schedule",
    event: Optional[Dict[str, Any]] = None,
) -> Any:
    async def factory(tracker: Any, budget: PrecheckBudget) -> FakeReader:
        return reader

    return await evaluate_precheck(
        None,  # type: ignore[arg-type]
        flow_with(precheck),
        event or {"payload": {}},
        trigger_kind=kind,  # type: ignore[arg-type]
        reader_factory=factory,
        now=NOW,
    )


PRS = {"checks": [{"id": "stale_prs", "type": "open_pull_requests"}]}


# -- schema -----------------------------------------------------------------


class TestSchema:
    def test_defaults(self) -> None:
        parsed = FlowPrecheck.model_validate(PRS)
        assert parsed.applies_to == ["schedule"]
        assert parsed.on_error == "fail"
        assert parsed.run_if == "any"
        check = parsed.checks[0]
        assert check.exclude.approved_and_mergeable is True
        assert check.exclude.active_execution is True
        assert check.max_items == 25

    @pytest.mark.parametrize(
        "document",
        [
            {"checks": []},
            {"checks": [{"id": "x", "type": "script"}]},
            {"checks": [{"id": "x", "type": "issues", "typo": 1}]},
            {"checks": [{"id": "x", "type": "issues", "max_items": 51}]},
            {"checks": [{"id": "x", "type": "issues"}] * 2},
            {"checks": [{"id": f"c{i}", "type": "issues"} for i in range(9)]},
            {"checks": [{"id": "Bad Id", "type": "issues"}]},
            {"applies_to": ["event"], "checks": [{"id": "x", "type": "issues"}]},
            {"skip_unchanged": True, "checks": [{"id": "x", "type": "issues"}]},
            {"on_error": "skip", "checks": [{"id": "x", "type": "issues"}]},
        ],
    )
    def test_refused(self, document: Dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            FlowPrecheck.model_validate(document)

    def test_since_forms(self) -> None:
        for since in ("last_run", "previous_tick", {"hours": 6}):
            FlowPrecheck.model_validate(
                {"checks": [{"id": "x", "type": "failed_ci_runs", "since": since}]}
            )


# -- scope ------------------------------------------------------------------


class TestScope:
    def test_named_repository_outside_the_flow_is_refused(self) -> None:
        check = FlowPrecheck.model_validate(
            {"checks": [{"id": "x", "type": "issues", "repositories": ["other/repo"]}]}
        ).checks[0]
        with pytest.raises(PrecheckConfigError, match="not one of the flow's"):
            repositories_for_check(check, [REPOSITORY])

    def test_names_compare_case_insensitively(self) -> None:
        check = FlowPrecheck.model_validate(
            {"checks": [{"id": "x", "type": "issues", "repositories": ["ACME/App"]}]}
        ).checks[0]
        assert repositories_for_check(check, [REPOSITORY]) == [REPOSITORY]

    def test_a_flow_without_repositories_is_refused(self) -> None:
        check = FlowPrecheck.model_validate(PRS).checks[0]
        with pytest.raises(PrecheckConfigError, match="no repository"):
            repositories_for_check(check, [])

    def test_a_tracker_without_a_reader_is_refused(self) -> None:
        gitlab = PrecheckRepository(slug="g/r", tracker_type="gitlab", tracker=None)
        check = FlowPrecheck.model_validate(PRS).checks[0]
        with pytest.raises(PrecheckConfigError, match="gitlab trackers"):
            repositories_for_check(check, [gitlab])


# -- open_pull_requests -------------------------------------------------------


def _prs_check(**params: Any) -> Dict[str, Any]:
    return {"checks": [{"id": "stale_prs", "type": "open_pull_requests", **params}]}


class TestOpenPullRequests:
    async def test_items_carry_ids_urls_titles_and_reasons(self, facts) -> None:
        outcome = await run(
            _prs_check(),
            FakeReader(prs=[pr(101, unresolved_threads=2, mergeable="CONFLICTING")]),
        )
        assert outcome.action == "run"
        entry = outcome.document["checks"]["stale_prs"]
        assert entry["count"] == 1
        [item] = entry["items"]
        assert item["number"] == 101
        assert item["url"] == f"https://github.com/{REPO}/pull/101"
        assert item["reasons"] == [
            "failing_checks",
            "merge_conflict",
            "unresolved_review_threads",
        ]
        assert set(item) >= {"title", "author", "head_sha", "updated_at"}

    async def test_approved_and_mergeable_is_never_matched(self, facts) -> None:
        reader = FakeReader(
            prs=[pr(1, review_decision="APPROVED", mergeable="MERGEABLE")]
        )
        outcome = await run(_prs_check(), reader)
        assert outcome.action == "skip"
        assert outcome.reason == "nothing_to_do"

        opted_out = await run(
            _prs_check(exclude={"approved_and_mergeable": False}), reader
        )
        assert opted_out.action == "run"

    async def test_a_pull_request_with_a_live_execution_is_excluded(
        self, facts
    ) -> None:
        facts["active"] = {f"https://github.com/{REPO}/pull/1"}
        outcome = await run(_prs_check(), FakeReader(prs=[pr(1), pr(2)]))
        numbers = [
            i["number"] for i in outcome.document["checks"]["stale_prs"]["items"]
        ]
        assert numbers == [2]

    async def test_fresh_claim_excludes_and_stale_claim_does_not(self, facts) -> None:
        fresh = pr(
            1,
            labels=("in-progress",),
            label_applied_at={"in-progress": NOW - timedelta(hours=1)},
        )
        stale = pr(
            2,
            labels=("In-Progress",),
            label_applied_at={"in-progress": NOW - timedelta(hours=10)},
        )
        params = {
            "exclude": {"claim_labels": ["in-progress"], "claim_max_age_hours": 6}
        }
        outcome = await run(_prs_check(**params), FakeReader(prs=[fresh, stale]))
        numbers = [
            i["number"] for i in outcome.document["checks"]["stale_prs"]["items"]
        ]
        assert numbers == [2]

    async def test_needs_any_idle_hours_authors_labels_drafts(self, facts) -> None:
        prs = [
            pr(1),  # failing checks, idle 5h: kept
            pr(2, checks_state="SUCCESS"),  # no reason
            pr(3, updated_at=NOW - timedelta(minutes=30)),  # not idle
            pr(4, author="bot"),  # excluded author
            pr(5, labels=("do-not-touch",)),  # excluded label
            pr(6, is_draft=True),  # draft
        ]
        outcome = await run(
            _prs_check(
                needs_any=["failing_checks", "merge_conflict"],
                idle_hours=2,
                authors={"exclude": ["bot"]},
                labels={"none": ["do-not-touch"]},
            ),
            FakeReader(prs=prs),
        )
        numbers = [
            i["number"] for i in outcome.document["checks"]["stale_prs"]["items"]
        ]
        assert numbers == [1]

    async def test_min_count_and_truncation(self, facts) -> None:
        reader = FakeReader(prs=[pr(n) for n in range(1, 6)])
        outcome = await run(_prs_check(max_items=2), reader)
        entry = outcome.document["checks"]["stale_prs"]
        assert entry["count"] == 5
        assert len(entry["items"]) == 2
        assert entry["truncated"] is True

        outcome = await run(_prs_check(min_count=6), reader)
        assert outcome.action == "skip"

    async def test_titles_are_truncated(self, facts) -> None:
        outcome = await run(_prs_check(), FakeReader(prs=[pr(1, title="x" * 500)]))
        title = outcome.document["checks"]["stale_prs"]["items"][0]["title"]
        assert len(title) == 120


# -- the other checks ---------------------------------------------------------


class TestDefaultBranchCi:
    CHECK = {"checks": [{"id": "main_red", "type": "default_branch_ci"}]}

    async def test_red_branch_matches(self, facts) -> None:
        outcome = await run(self.CHECK, FakeReader(branch=red_branch()))
        entry = outcome.document["checks"]["main_red"]
        assert outcome.action == "run"
        assert entry["summary"] == f"main is red on {REPO}"
        assert entry["items"][0]["failing_checks"] == [
            {"name": "tests", "url": "https://example.com/1"}
        ]

    async def test_green_branch_skips(self, facts) -> None:
        outcome = await run(self.CHECK, FakeReader(branch=green_branch()))
        assert outcome.action == "skip"
        assert outcome.summary == f"main is green on {REPO}"

    async def test_required_checks_only(self, facts) -> None:
        branch = replace(red_branch(), required=("lint",))
        outcome = await run(self.CHECK, FakeReader(branch=branch))
        assert outcome.action == "skip"


def failed_run(run_id: int, hours_ago: float, workflow: str = "CI") -> FailedRun:
    return FailedRun(
        repository=REPO,
        id=run_id,
        url=f"https://example.com/runs/{run_id}",
        workflow=workflow,
        branch="main",
        head_sha="abc",
        event="push",
        created_at=NOW - timedelta(hours=hours_ago),
    )


class TestFailedCiRuns:
    async def test_since_last_run(self, facts) -> None:
        facts["last_run_at"] = NOW - timedelta(hours=3)
        reader = FakeReader(runs=[failed_run(1, 2), failed_run(2, 5)])
        outcome = await run(
            {"checks": [{"id": "new_failures", "type": "failed_ci_runs"}]}, reader
        )
        assert [
            i["id"] for i in outcome.document["checks"]["new_failures"]["items"]
        ] == [1]
        assert reader.calls[0]["since"] == NOW - timedelta(hours=3)

    async def test_previous_tick_and_hours(self, facts) -> None:
        reader = FakeReader(runs=[failed_run(1, 2)])
        event = {"payload": {"previous_scheduled_at": "2026-01-01T11:00:00+00:00"}}
        outcome = await run(
            {
                "checks": [
                    {"id": "n", "type": "failed_ci_runs", "since": "previous_tick"}
                ]
            },
            reader,
            event=event,
        )
        assert outcome.action == "skip"
        assert reader.calls[0]["since"] == NOW - timedelta(hours=1)

        outcome = await run(
            {"checks": [{"id": "n", "type": "failed_ci_runs", "since": {"hours": 4}}]},
            reader,
        )
        assert outcome.action == "run"

    async def test_workflow_filter(self, facts) -> None:
        reader = FakeReader(runs=[failed_run(1, 0.5, "Docs")])
        outcome = await run(
            {"checks": [{"id": "n", "type": "failed_ci_runs", "workflows": ["CI"]}]},
            reader,
        )
        assert outcome.action == "skip"


class TestIssues:
    async def test_filters_and_priority_order(self, facts) -> None:
        def issue(number: int, labels: tuple, assignees: tuple = ()) -> IssueState:
            return IssueState(
                repository=REPO,
                number=number,
                url=f"https://github.com/{REPO}/issues/{number}",
                title=f"Issue {number}",
                labels=labels,
                assignees=assignees,
                milestone="1.1.0",
                created_at=NOW,
                updated_at=NOW,
            )

        reader = FakeReader(
            issues=[
                issue(1, ("readiness:ready", "P2")),
                issue(2, ("readiness:ready", "P0")),
                issue(3, ("readiness:ready", "readiness:blocked")),
                issue(4, ("readiness:ready",), assignees=("jane-doe",)),
            ]
        )
        outcome = await run(
            {
                "checks": [
                    {
                        "id": "ready",
                        "type": "issues",
                        "labels": {
                            "all": ["readiness:ready"],
                            "none": ["readiness:blocked"],
                        },
                        "unassigned": True,
                        "order_by_labels": ["P0", "P1", "P2"],
                    }
                ]
            },
            reader,
        )
        numbers = [i["number"] for i in outcome.document["checks"]["ready"]["items"]]
        assert numbers == [2, 1]


# -- engine -------------------------------------------------------------------


TWO = {
    "checks": [
        {"id": "stale_prs", "type": "open_pull_requests"},
        {"id": "main_red", "type": "default_branch_ci"},
    ]
}


class TestEngine:
    async def test_run_if_any_and_all(self, facts) -> None:
        reader = FakeReader(prs=[pr(1)], branch=green_branch())
        assert (await run(TWO, reader)).action == "run"
        assert (await run({**TWO, "run_if": "all"}, reader)).action == "skip"
        reader = FakeReader(prs=[pr(1)], branch=red_branch())
        assert (await run({**TWO, "run_if": "all"}, reader)).action == "run"

    async def test_skip_summary_joins_the_checks(self, facts) -> None:
        outcome = await run(TWO, FakeReader(branch=green_branch()))
        assert outcome.action == "skip"
        assert outcome.summary == (
            f"No open pull requests need attention; main is green on {REPO}"
        )
        assert outcome.document["decision"] == "skip"
        assert outcome.document["reason"] == "nothing_to_do"

    async def test_max_skip_hours_forces_a_run(self, facts) -> None:
        facts["last_real_run_at"] = NOW - timedelta(hours=30)
        outcome = await run({**PRS, "max_skip_hours": 24}, FakeReader())
        assert outcome.action == "run"
        assert outcome.document["forced"] is True

        facts["last_real_run_at"] = NOW - timedelta(hours=2)
        outcome = await run({**PRS, "max_skip_hours": 24}, FakeReader())
        assert outcome.action == "skip"

    async def test_a_tracker_error_fails_by_default(self, facts) -> None:
        reader = FakeReader(error=PrecheckError("server_error", "GitHub returned 502"))
        outcome = await run(PRS, reader)
        assert outcome.action == "fail"
        assert outcome.failure_category == "precheck_failed"
        assert "stale_prs" in outcome.error_message
        assert "server_error" in outcome.error_message
        assert outcome.document["precheck_error"] == {
            "check": "stale_prs",
            "cause": "server_error",
        }

    async def test_on_error_run_starts_and_names_the_error(self, facts) -> None:
        reader = FakeReader(error=PrecheckError("rate_limited", "quota"))
        outcome = await run({**PRS, "on_error": "run"}, reader)
        assert outcome.action == "run"
        assert outcome.document["precheck_error"]["cause"] == "rate_limited"

    async def test_an_engine_bug_fails_visibly(self, facts, monkeypatch) -> None:
        async def broken(check: Any, ctx: Any) -> Any:
            raise KeyError("boom")

        engine._ensure_checks_loaded()
        monkeypatch.setitem(engine.CHECKS, "open_pull_requests", broken)
        outcome = await run(PRS, FakeReader())
        assert outcome.action == "fail"
        assert outcome.document["precheck_error"]["cause"] == "engine_error"

    async def test_the_wall_clock_budget(self, facts, monkeypatch) -> None:
        monkeypatch.setenv("PRELOOP_PRECHECK_TIMEOUT_SECONDS", "1")
        outcome = await run(PRS, FakeReader(delay=5))
        assert outcome.action == "fail"
        assert outcome.document["precheck_error"] == {
            "check": "stale_prs",
            "cause": "timeout",
        }

    async def test_an_invalid_stored_precheck_is_a_config_failure(self, facts) -> None:
        outcome = await run({"checks": [{"id": "x", "type": "removed"}]}, FakeReader())
        assert outcome.action == "fail"
        assert outcome.failure_category == "flow_config"

    async def test_a_repository_out_of_scope_at_evaluation(self, facts) -> None:
        facts["repositories"] = []
        outcome = await run(PRS, FakeReader())
        assert outcome.failure_category == "flow_config"

    async def test_applies_to_and_the_kill_switch(self, facts, monkeypatch) -> None:
        assert await run(PRS, FakeReader(), kind="manual") is None
        manual = {**PRS, "applies_to": ["manual"]}
        assert (await run(manual, FakeReader(), kind="manual")).action == "skip"
        assert await run(manual, FakeReader(), kind="schedule") is None
        assert (await run(manual, FakeReader(), kind="dry_run")).action == "skip"

        monkeypatch.setenv("PRELOOP_FLOW_PRECHECK_ENABLED", "false")
        assert await run(PRS, FakeReader()) is None

    async def test_no_precheck_is_none(self, facts) -> None:
        assert (
            await evaluate_precheck(
                None,  # type: ignore[arg-type]
                flow_with(None),  # type: ignore[arg-type]
                {},
                trigger_kind="schedule",
            )
            is None
        )

    async def test_the_document_is_capped(self, facts) -> None:
        many = [pr(n, title="t" * 120) for n in range(1, 51)]
        checks = {
            "checks": [
                {"id": f"c{i}", "type": "open_pull_requests", "max_items": 50}
                for i in range(8)
            ]
        }
        outcome = await run(checks, FakeReader(prs=many))
        import json

        assert len(json.dumps(outcome.document).encode()) <= MAX_CONTEXT_BYTES
        assert any(e["truncated"] for e in outcome.document["checks"].values())


class TestDigest:
    def test_stable_across_titles_and_order(self) -> None:
        one = {
            "a": {
                "items": [
                    {"repository": REPO, "number": 1, "head_sha": "x", "title": "A"},
                    {"repository": REPO, "number": 2, "head_sha": "y", "title": "B"},
                ]
            }
        }
        two = {
            "a": {
                "items": [
                    {"repository": REPO, "number": 2, "head_sha": "y", "title": "b"},
                    {"repository": REPO, "number": 1, "head_sha": "x", "title": "a"},
                ]
            }
        }
        assert compute_digest(one) == compute_digest(two)
        two["a"]["items"][0]["head_sha"] = "z"
        assert compute_digest(one) != compute_digest(two)
