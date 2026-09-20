"""Independent PostgreSQL regressions for triage authority and serialization."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue_lifecycle, crud_project
from preloop.models.crud.base import CRUDBase
from preloop.schemas.issue_triage import IssueTriageApply, TriageIssue
from preloop.services import issue_triage_controller as controller
from preloop.services.issue_triage import get_context
from tests.services.test_issue_lifecycle import rig as lifecycle_rig


class Provider:
    """Deterministic issue state; all controller persistence remains real."""

    kind = "github"

    def __init__(self, issue: models.Issue) -> None:
        self.issue = TriageIssue(
            title=issue.title,
            body=issue.description,
            state="open",
            url="https://github.com/example/project/issues/1",
            labels=["human-label"],
        )
        self.rows = [
            {"name": name, "description": ""}
            for name in ["complexity:low", "complexity:medium", "complexity:high"]
        ]
        self.operations: list[str] = []
        self.before_write: Any = None

    async def read_issue(self) -> TriageIssue:
        return self.issue.model_copy(deep=True)

    async def catalogue(self) -> list[dict[str, str]]:
        return deepcopy(self.rows)

    async def write_content(self, title: str, body: str) -> None:
        if self.before_write:
            await self.before_write()
        self.issue = self.issue.model_copy(update={"title": title, "body": body})
        self.operations.append("content")

    async def update_labels(self, add: list[str], remove: list[str]) -> None:
        self.issue.labels = sorted((set(self.issue.labels) | set(add)) - set(remove))
        self.operations.append("labels")


def _rig(db: Session, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    service, _, flow, _ = lifecycle_rig.__wrapped__(db)
    CRUDBase(models.Flow).update(
        db, db_obj=flow, obj_in={"name": controller.TRIAGE_NAME}
    )
    provider = Provider(service.issue)
    monkeypatch.setattr(
        controller, "authorized_provider", AsyncMock(return_value=provider)
    )
    project = crud_project.get(db, id=service.issue.project_id)
    return SimpleNamespace(
        db=db,
        account_id=service.account_id,
        issue=service.issue,
        project=project,
        flow=flow,
        provider=provider,
        event={"project_id": str(project.id), "payload": {"issue": {"number": 1}}},
    )


@contextmanager
def _committed_rig(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Keep independently visible fixtures isolated without dropping the schema."""
    account_id = None
    try:
        with Session(db_engine) as db:
            rig = _rig(db, monkeypatch)
            account_id = rig.account_id
            yield rig
    finally:
        if account_id is not None:
            with Session(db_engine) as cleanup:
                CRUDBase(models.Account).delete(cleanup, id=account_id)


async def _claim(rig: SimpleNamespace) -> tuple[models.FlowExecution, IssueTriageApply]:
    execution, _ = await controller.reserve_triage_execution(
        rig.db, flow=rig.flow, event=rig.event
    )
    request = IssueTriageApply(
        expected_revision=(await get_context(rig.provider)).expected_revision,
        assessment="Verify saved preferences remain available after restarting.",
        complexity_label="complexity:low",
    )
    return execution, request


async def _apply(
    rig: SimpleNamespace, execution: models.FlowExecution, request: IssueTriageApply
) -> Any:
    return await controller.apply_controlled_triage(
        rig.db,
        issue=rig.issue,
        provider=rig.provider,
        account_id=rig.account_id,
        request=request,
        execution_id=str(execution.id),
    )


@pytest.mark.asyncio
async def test_apply_lock_survives_durable_intent_and_serializes_competing_claim(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate database connections observe intent but cannot overtake the apply."""
    with _committed_rig(db_engine, monkeypatch) as rig, Session(db_engine) as second:
        first = rig.db
        execution, request = await _claim(rig)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_write() -> None:
            with Session(db_engine) as observer:
                issue = crud_issue_lifecycle.get_issue(
                    observer, account_id=rig.account_id, issue_id=rig.issue.id
                )
                assert issue.meta_data["preloop_triage"]["expected_revisions"]
                row = crud_issue_lifecycle.triage_for_execution(
                    observer, account_id=rig.account_id, execution_id=execution.id
                )
                assert row.state == "applying" and row.data["pending_request"]
            entered.set()
            await release.wait()

        rig.provider.before_write = blocked_write
        apply_task = asyncio.create_task(_apply(rig, execution, request))
        await asyncio.wait_for(entered.wait(), 3)
        other_flow = crud_issue_lifecycle.triage_flow(
            second, account_id=rig.account_id, flow_id=rig.flow.id
        )
        reserve_task = asyncio.create_task(
            controller.reserve_triage_execution(
                second, flow=other_flow, event=rig.event
            )
        )
        await asyncio.sleep(0.15)
        escaped_lock = reserve_task.done()
        release.set()
        result, (repeated, coalesced) = await asyncio.wait_for(
            asyncio.gather(apply_task, reserve_task), 4
        )
        assert not escaped_lock, (
            "A competing claim escaped the lock after the intent commit"
        )
        assert result.status == "updated" and result.cache_updated
        assert coalesced and repeated.id == execution.id
        assert rig.provider.operations == ["content", "labels"]


@pytest.mark.asyncio
async def test_session_lock_releases_after_error_without_losing_committed_receipt(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _committed_rig(db_engine, monkeypatch) as rig, Session(db_engine) as second:
        first = rig.db
        with pytest.raises(RuntimeError, match="simulated"):
            async with crud_issue_lifecycle.triage_locked(
                first, rig.account_id, rig.issue.id
            ):
                crud_issue_lifecycle.triage_receipt(
                    first,
                    issue=rig.issue,
                    receipt={"expected_revisions": ["durable-intent"]},
                )
                crud_issue_lifecycle.commit(first)
                raise RuntimeError("simulated provider failure")
        async with asyncio.timeout(2):
            async with crud_issue_lifecycle.triage_locked(
                second, rig.account_id, rig.issue.id
            ):
                issue = crud_issue_lifecycle.get_issue(
                    second, account_id=rig.account_id, issue_id=rig.issue.id
                )
                assert issue.meta_data["preloop_triage"]["expected_revisions"] == [
                    "durable-intent"
                ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "version",
        "account_id",
        "project_id",
        "issue_id",
        "execution_id",
        "policy_identity",
        "context_identity",
        "resulting_lifecycle_revision",
        "flow_id",
    ],
)
async def test_packet_loader_rejects_forged_provenance(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    assert (await _apply(rig, execution, request)).status == "updated"
    revision = controller.lifecycle_revision(
        rig.provider.issue.title, rig.provider.issue.body
    )
    lookup = dict(
        account_id=rig.account_id, issue_id=rig.issue.id, lifecycle_revision=revision
    )
    assert controller.applicable_triage_packet(db_session, **lookup) is not None
    assert (
        controller.applicable_triage_packet(
            db_session, **{**lookup, "account_id": uuid4()}
        )
        is None
    )
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    packet = {**row.data["packet"], field: 999 if field == "version" else str(uuid4())}
    crud_issue_lifecycle.put(
        db_session,
        account_id=rig.account_id,
        issue_id=rig.issue.id,
        kind="triage",
        revision=row.revision,
        state="assessed",
        data={**row.data, "packet": packet},
    )
    assert controller.applicable_triage_packet(db_session, **lookup) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "identifier", "tracker_settings"])
async def test_changed_project_context_rejects_old_apply_and_permits_new_claim(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    values = {
        "policy": {"settings": {"issue_lifecycle": {"ready_enabled": False}}},
        "identifier": {"identifier": "different/project"},
        "tracker_settings": {
            "tracker_settings": {"owner": "different", "repo": "project"}
        },
    }[change]
    crud_project.update(db_session, db_obj=rig.project, obj_in=values)
    with pytest.raises(ValueError, match="context_changed"):
        await _apply(rig, execution, request)
    assert rig.provider.operations == []
    fresh, coalesced = await controller.reserve_triage_execution(
        db_session, flow=rig.flow, event=rig.event
    )
    assert fresh.id != execution.id and not coalesced


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["account", "execution", "issue"])
async def test_apply_rejects_foreign_authority_before_provider_mutation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, foreign: str
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    issue = rig.issue
    if foreign == "issue":
        issue = CRUDBase(models.Issue).create(
            db_session,
            obj_in={
                "title": "Another issue",
                "description": "Unrelated requirements",
                "status": "open",
                "external_id": "1002",
                "key": "example/project#2",
                "project_id": rig.project.id,
                "tracker_id": rig.issue.tracker_id,
            },
        )
    with pytest.raises(
        ValueError, match="triage_(issue_not_found|execution_issue_mismatch)"
    ):
        await controller.apply_controlled_triage(
            db_session,
            issue=issue,
            provider=rig.provider,
            account_id=uuid4() if foreign == "account" else rig.account_id,
            request=request,
            execution_id=str(uuid4() if foreign == "execution" else execution.id),
        )
    assert rig.provider.operations == []


@pytest.mark.asyncio
async def test_changed_complexity_family_after_partial_assessment_gets_new_claim(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt for managed prose must not hide newly available classification."""
    rig = _rig(db_session, monkeypatch)
    rig.provider.rows = [
        {"name": "complexity:low", "description": ""},
        {"name": "size:S", "description": ""},
    ]
    execution, request = await _claim(rig)
    request.complexity_label = None
    assert (await _apply(rig, execution, request)).status == "partial"
    # Resolving an ambiguous catalogue changes context without editing the issue.
    rig.provider.rows = [{"name": "size:S", "description": ""}]
    fresh, coalesced = await controller.reserve_triage_execution(
        db_session, flow=rig.flow, event=rig.event
    )
    assert fresh.id != execution.id and not coalesced


@pytest.mark.asyncio
async def test_oversized_family_never_persists_an_unbounded_applicable_packet(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    rig.provider.rows.extend(
        {"name": "complexity:" + "x" * 2048 + str(index), "description": ""}
        for index in range(80)
    )
    execution, request = await _claim(rig)
    result = await _apply(rig, execution, request)
    assert result.status == "partial"
    assert result.reason == "triage_context_packet_too_large"
    assert result.cache_updated
    assert rig.provider.operations == ["content", "labels"]
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    assert row.data.get("packet") is None
    assert (
        controller.applicable_triage_packet(
            db_session,
            account_id=rig.account_id,
            issue_id=rig.issue.id,
            lifecycle_revision=controller.lifecycle_revision(
                rig.provider.issue.title, rig.provider.issue.body
            ),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["project_policy", "tracker_scope", "organization_name", "tracker_url"]
)
async def test_packet_lookup_rechecks_context_changed_by_another_session(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Cached ORM relationships must not authenticate superseded provider scope."""
    with _committed_rig(db_engine, monkeypatch) as rig, Session(db_engine) as second:
        first = rig.db
        execution, request = await _claim(rig)
        assert (await _apply(rig, execution, request)).status == "updated"
        lookup = {
            "account_id": rig.account_id,
            "issue_id": rig.issue.id,
            "lifecycle_revision": controller.lifecycle_revision(
                rig.provider.issue.title, rig.provider.issue.body
            ),
        }
        assert controller.applicable_triage_packet(first, **lookup) is not None
        if change == "project_policy":
            project = crud_project.get(second, id=rig.project.id)
            crud_project.update(
                second,
                db_obj=project,
                obj_in={"settings": {"issue_lifecycle": {"ready_enabled": False}}},
            )
        elif change == "organization_name":
            organization = CRUDBase(models.Organization).get(
                second, id=rig.project.organization_id
            )
            CRUDBase(models.Organization).update(
                second, db_obj=organization, obj_in={"name": "different-organization"}
            )
        else:
            tracker = CRUDBase(models.Tracker).get(second, id=rig.issue.tracker_id)
            CRUDBase(models.Tracker).update(
                second,
                db_obj=tracker,
                obj_in={"url": "https://different.example.invalid"}
                if change == "tracker_url"
                else {"connection_details": {"owner": "other", "repo": "repository"}},
            )
        assert controller.applicable_triage_packet(first, **lookup) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["direct", "nested", "mixed", "double"])
async def test_persistent_triage_refused_before_claim_or_provider_access(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, wrapper: str
) -> None:
    """Agent Control metadata does not bind the managed agent's API credential."""
    rig = _rig(db_session, monkeypatch)
    config: dict[str, Any] = {
        "execution_path": "persistent",
        "target_agent_id": str(uuid4()),
    }
    if wrapper == "nested":
        config = {"agent_config": config}
    elif wrapper == "mixed":
        config = {"agent_config": config, "other": True}
    elif wrapper == "double":
        config = {"agent_config": {"agent_config": config}}
    CRUDBase(models.Flow).update(
        db_session,
        db_obj=rig.flow,
        obj_in={"agent_config": config},
    )
    provider_lookup = AsyncMock(
        side_effect=AssertionError("Unsupported executor must not read provider state")
    )
    monkeypatch.setattr(controller, "authorized_provider", provider_lookup)
    with pytest.raises(controller.TriageControllerError, match="persistent"):
        await controller.reserve_triage_execution(
            db_session, flow=rig.flow, event=rig.event
        )
    provider_lookup.assert_not_awaited()
    assert (
        crud_issue_lifecycle.list_for_issue(
            db_session, account_id=rig.account_id, issue_id=rig.issue.id
        )
        == []
    )


@pytest.mark.asyncio
async def test_duplicate_worker_delivery_cannot_reenter_same_triage_execution(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-process duplicate must retain the live owner's lease and drain entry."""
    from preloop.services import flow_execution_runner as runner

    with _committed_rig(db_engine, monkeypatch) as rig:
        execution, _ = await _claim(rig)
        execution_id = str(execution.id)
        entered, release = asyncio.Event(), asyncio.Event()
        runs = 0
        sessions: list[Session] = []

        def session_factory() -> Any:
            session = Session(db_engine)
            sessions.append(session)
            return iter([session])

        async def hold_execution(orchestrator: Any) -> None:
            nonlocal runs
            runs += 1
            entered.set()
            await release.wait()

        monkeypatch.setattr(runner, "get_db_session", session_factory)
        monkeypatch.setattr(
            runner, "get_orchestrator_worker_id", lambda: "same-triage-worker"
        )
        monkeypatch.setattr(runner, "get_nats_client", AsyncMock())
        monkeypatch.setattr(runner, "flows_halted", lambda *args: False)
        monkeypatch.setattr(
            runner,
            "FlowExecutionOrchestrator",
            lambda *args, **kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(runner, "run_existing_execution", hold_execution)
        first = asyncio.create_task(runner.claim_and_run_execution(execution_id))
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(runner.claim_and_run_execution(execution_id))
        try:
            await asyncio.sleep(0.15)
            duplicate_finished = second.done()
            owner_still_tracked = (
                execution_id in runner.get_active_claimed_execution_ids()
            )
        finally:
            release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(first, second), 4)
        assert runs == 1, "Duplicate delivery reentered orchestration for the same row"
        assert duplicate_finished and outcomes[1]["status"] == "skipped"
        assert owner_still_tracked, (
            "Denied duplicate removed the live owner's drain entry"
        )
        assert execution_id not in runner.get_active_claimed_execution_ids()
        # An explicit release remains recoverable; only fresh simultaneous ownership is denied.
        assert (await runner.claim_and_run_execution(execution_id))[
            "status"
        ] == "completed"
        assert runs == 2
        for session in sessions:
            session.close()
