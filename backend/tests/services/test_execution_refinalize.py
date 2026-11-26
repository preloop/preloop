"""Stored review recovery uses no executor or model and preserves publication gates."""

# ruff: noqa: F811

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.services import execution_refinalize as recovery
from preloop.services.native_result import NATIVE_CANDIDATES_KEY
from tests.services.test_platform_review import (  # noqa: F401
    HEAD,
    RESULT,
    finished,
    github,
    identity,
    publish,
    world,
)


def saved(world: SimpleNamespace, *, inspected: bool = True) -> models.FlowExecution:
    """A complete answer stored as transport after a worker handover."""
    execution = finished(
        world,
        identity(world, world.projects[0]),
        status="FAILED",
        result={NATIVE_CANDIDATES_KEY: {"native": deepcopy(RESULT), "file": None}},
        valid=False,
        inspected=inspected,
    )
    return CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={
            "failure_category": "model_transient",
            "end_time": datetime.now(timezone.utc) - timedelta(minutes=5),
            "trigger_event_details": {
                **execution.trigger_event_details,
                "_native_result": {"value": True, "source": "flow"},
            },
            "startup_receipt": {
                "_completion": {
                    "capture_state": "captured",
                    "result_source": "native_structured_result",
                    "agent_finished": {"status": "FAILED", "exit_code": 0},
                }
            },
        },
    )


async def call(world: SimpleNamespace, execution: Any, **kwargs: Any) -> dict[str, Any]:
    """Call the service as the account owner."""
    return await recovery.refinalize_execution(
        world.db, actor=world.owner, execution_id=execution.id, **kwargs
    )


@pytest.fixture(autouse=True)
def provider_read(monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace) -> None:
    """Read the same in-memory PR as the fake publication provider."""

    async def read(tracker: Any, repository_id: str, pr_number: int) -> Any:
        return next(
            pull
            for pull in world.github.pulls.values()
            if str(pull["base"]["repo"]["id"]) == repository_id
            and pull["number"] == pr_number
        )

    monkeypatch.setattr(
        "preloop.services.event_review.read_pull_request_for_event", read
    )


@pytest.mark.asyncio
async def test_preview_apply_and_repeat(world: SimpleNamespace) -> None:
    execution = saved(world)
    original = deepcopy(execution.result)
    metrics = (execution.total_tokens, execution.estimated_cost, execution.end_time)
    preview = await call(world, execution)
    assert preview["eligible"] is True
    assert preview["selected_result"] == RESULT
    assert preview["publication"]["would_publish"] is True, preview["publication"]
    assert execution.status == "FAILED" and execution.result == original
    assert not world.github.writes
    result = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert result["outcome"] == "refinalized"
    assert execution.status == "SUCCEEDED" and execution.result == RESULT
    assert execution.failure_category is None
    assert execution.result_schema_validation["valid"] is True
    assert (
        execution.total_tokens,
        execution.estimated_cost,
        execution.end_time,
    ) == metrics
    assert result["publication"]["review"] == "posted"
    audit = (
        world.db.query(models.AuditLog).filter_by(action="execution_refinalize").one()
    )
    assert audit.user_id == world.owner.id
    assert audit.details["previous_status"] == "FAILED"
    assert audit.details["previous_failure_category"] == "model_transient"
    assert audit.details["new_status"] == "SUCCEEDED"
    again = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert again["outcome"] == "already_refinalized"
    await publish(world, execution)
    assert len(world.github.reviews[(world.projects[0].slug, 7)]) == 1


@pytest.mark.asyncio
async def test_moved_head_refinalizes_without_post(world: SimpleNamespace) -> None:
    execution = saved(world)
    world.github.pulls[(world.projects[0].slug, 7)]["head"]["sha"] = "c" * 40
    preview = await call(world, execution)
    assert preview["eligible"] is True
    assert preview["publication"]["reason"] == "head_moved"
    result = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert result["outcome"] == "refinalized"
    assert result["publication"]["reason"] == "head_moved"
    assert not world.github.reviews


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,gate",
    [
        ({"status": "STOPPED"}, "terminal_outcome"),
        ({"status": "TIMED_OUT"}, "terminal_outcome"),
        ({"failure_category": "timeout"}, "terminal_outcome"),
        ({"failure_category": "review_not_inspected"}, "terminal_outcome"),
        ({"failure_category": "review_binding_mismatch"}, "terminal_outcome"),
        ({"failure_category": "result_schema_invalid"}, "terminal_outcome"),
        ({"flow_revision": 999}, "flow_revision"),
        ({"result": {"verdict": "pass"}}, "schema"),
        ({"trigger_event_details": {}}, "capture_provenance"),
    ],
)
async def test_refusals(
    world: SimpleNamespace, change: dict[str, Any], gate: str
) -> None:
    execution = saved(world)
    CRUDBase(models.FlowExecution).update(world.db, db_obj=execution, obj_in=change)
    preview = await call(world, execution)
    assert preview["eligible"] is False
    assert preview["gates"][gate]["passed"] is False
    with pytest.raises(recovery.RefinalizeRefusedError):
        await call(world, execution, dry_run=False, expected_digest=preview["digest"])
    assert not world.github.writes


@pytest.mark.asyncio
async def test_uninspected_and_wrong_digest(world: SimpleNamespace) -> None:
    execution = saved(world, inspected=False)
    preview = await call(world, execution)
    assert preview["gates"]["inspection"]["passed"] is False
    assert preview["eligible"] is False
    with pytest.raises(recovery.RefinalizePreconditionFailedError):
        await call(world, execution, dry_run=False, expected_digest="0" * 64)


@pytest.mark.asyncio
async def test_succeeded_is_refused(world: SimpleNamespace) -> None:
    execution = finished(world, identity(world, world.projects[0]))
    preview = await call(world, execution)
    assert not preview["eligible"]
    assert preview["gates"]["terminal_outcome"]["reason"] == "already_succeeded"


@pytest.mark.asyncio
async def test_envelope_is_not_authority(world: SimpleNamespace) -> None:
    execution = saved(world)
    CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={
            "trigger_event_details": {
                **execution.trigger_event_details,
                "_native_result": {"value": False},
            },
        },
    )
    preview = await call(world, execution)
    assert not preview["eligible"]
    assert not preview["gates"]["capture_provenance"]["passed"]
    assert preview["selected_result"] is None
    assert not world.github.writes


@pytest.mark.asyncio
async def test_live_rejection_and_timeout_receipts_cannot_be_overridden(
    world: SimpleNamespace,
) -> None:
    execution = saved(world)
    original_receipt = deepcopy(execution.startup_receipt)
    for fact in (
        {"review_integrity": {"status": "rejected"}},
        {"agent_finished": {"status": "TIMEOUT", "exit_code": 0}},
        {"time_limit_after_result": True},
    ):
        receipt = deepcopy(original_receipt)
        receipt["_completion"].update(fact)
        CRUDBase(models.FlowExecution).update(
            world.db, db_obj=execution, obj_in={"startup_receipt": receipt}
        )
        preview = await call(world, execution)
        assert not preview["gates"]["terminal_outcome"]["passed"]


@pytest.mark.asyncio
async def test_tampered_evidence_cannot_promote(world: SimpleNamespace) -> None:
    execution = saved(world)
    row = (
        world.db.query(models.FlowExecutionLog)
        .filter_by(execution_id=execution.id, log_type="execution_tool_evidence")
        .one()
    )
    row.metadata_ = {**row.metadata_, "sha256": "0" * 64}
    world.db.commit()
    preview = await call(world, execution)
    assert not preview["eligible"]
    assert not preview["gates"]["tool_evidence"]["passed"]


@pytest.mark.asyncio
async def test_changed_result_invalidates_preview(world: SimpleNamespace) -> None:
    execution = saved(world)
    preview = await call(world, execution)
    changed = deepcopy(execution.result)
    changed[NATIVE_CANDIDATES_KEY]["native"]["summary"] = (
        "Another complete review summary."
    )
    CRUDBase(models.FlowExecution).update(
        world.db, db_obj=execution, obj_in={"result": changed}
    )
    with pytest.raises(recovery.RefinalizePreconditionFailedError):
        await call(world, execution, dry_run=False, expected_digest=preview["digest"])


@pytest.mark.asyncio
async def test_base_moved_cannot_post_even_on_periodic_pass(
    world: SimpleNamespace,
) -> None:
    execution = saved(world)
    preview = await call(world, execution)
    world.github.pulls[(world.projects[0].slug, 7)]["base"]["sha"] = "d" * 40
    result = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert result["outcome"] == "refinalized"
    assert result["publication"]["review_reason"] == "base_moved"
    await publish(world, execution)
    assert not world.github.reviews


@pytest.mark.asyncio
async def test_publication_race_posts_once(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = saved(world)
    preview = await call(world, execution)
    entered, release = asyncio.Event(), asyncio.Event()
    original = recovery.publication._mint

    async def mint(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(recovery.publication, "_mint", mint)
    apply = asyncio.create_task(
        call(world, execution, dry_run=False, expected_digest=preview["digest"])
    )
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await publish(world, execution)
        again = await call(
            world, execution, dry_run=False, expected_digest=preview["digest"]
        )
        assert again["outcome"] == "already_refinalized"
    finally:
        release.set()
    await apply
    assert len(world.github.reviews[(world.projects[0].slug, 7)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["prompt_template", "result_schema", "git_clone_config"]
)
async def test_flow_edit_cannot_reinterpret_old_answer(
    world: SimpleNamespace, field: str
) -> None:
    from preloop.models.schemas.flow import FlowUpdate

    execution = saved(world)
    values = {
        "prompt_template": "Review the change and its tests",
        "result_schema": {
            **world.flow.result_schema,
            "description": "Updated result contract",
        },
        "git_clone_config": {
            **world.flow.git_clone_config,
            "platform_review": {"enabled": True, "mode": "manual"},
        },
    }
    crud.crud_flow.update(
        world.db,
        db_obj=world.flow,
        flow_in=FlowUpdate(**{field: values[field]}),
        account_id=world.account.id,
    )
    preview = await call(world, execution)
    assert not preview["gates"]["flow_revision"]["passed"]
    assert not preview["eligible"]


@pytest.mark.asyncio
async def test_evidence_below_flow_level_is_refused(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = deepcopy(world.flow.git_clone_config)
    config["platform_review"]["evidence"] = "complete"
    CRUDBase(models.Flow).update(
        world.db, db_obj=world.flow, obj_in={"git_clone_config": config}
    )
    execution = saved(world)
    counts = recovery.publication.stored_tool_evidence(world.db, execution)
    monkeypatch.setattr(
        recovery.publication,
        "stored_tool_evidence",
        lambda *_: {**counts, "incomplete_records": 1, "masking_only_records": 1},
    )
    preview = await call(world, execution)
    assert not preview["eligible"]
    assert not preview["gates"]["tool_evidence"]["passed"]


@pytest.mark.asyncio
async def test_plain_stored_answer_is_recoverable(world: SimpleNamespace) -> None:
    execution = saved(world)
    CRUDBase(models.FlowExecution).update(
        world.db, db_obj=execution, obj_in={"result": RESULT}
    )
    preview = await call(world, execution)
    assert preview["eligible"]
    assert preview["selected_source"] == "stored_result"


@pytest.mark.asyncio
async def test_dry_run_and_repeat_do_not_write(world: SimpleNamespace) -> None:
    execution = saved(world)
    before = world.db.query(models.AuditLog).count()
    receipt = deepcopy(execution.startup_receipt)
    preview = await call(world, execution)
    assert world.db.query(models.AuditLog).count() == before
    assert execution.startup_receipt == receipt
    await call(world, execution, dry_run=False, expected_digest=preview["digest"])
    after = world.db.query(models.AuditLog).count()
    receipt = deepcopy(execution.startup_receipt)
    await call(world, execution, dry_run=False, expected_digest=preview["digest"])
    assert world.db.query(models.AuditLog).count() == after
    assert execution.startup_receipt == receipt


@pytest.mark.asyncio
async def test_unusable_succeeded_envelope_qualifies(world: SimpleNamespace) -> None:
    execution = saved(world)
    CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "failure_category": None,
        },
    )
    preview = await call(world, execution)
    assert preview["eligible"]
    applied = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert applied["outcome"] == "refinalized"
    assert execution.result == RESULT


@pytest.mark.asyncio
async def test_missing_inspection_read_names_the_gate(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = saved(world)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("stored evidence is unavailable")

    monkeypatch.setattr(recovery, "inspection_summary", unavailable)
    preview = await call(world, execution)
    assert not preview["eligible"]
    assert preview["gates"]["inspection"]["reason"] == "inspection_unavailable"


@pytest.mark.asyncio
async def test_atomic_audit_failure_preserves_original(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = saved(world)
    original = deepcopy(execution.result)
    preview = await call(world, execution)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(crud.crud_audit_log, "log_action", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await call(world, execution, dry_run=False, expected_digest=preview["digest"])
    world.db.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.result == original
    assert not recovery.refinalization_mark(execution)


@pytest.mark.asyncio
async def test_existing_publication_decision_is_reported(
    world: SimpleNamespace,
) -> None:
    execution = saved(world)
    await publish(world, execution)
    preview = await call(world, execution)
    assert preview["eligible"]
    assert not preview["publication"]["would_publish"]
    assert preview["publication"]["reason"] == "run_not_succeeded"
    applied = await call(
        world, execution, dry_run=False, expected_digest=preview["digest"]
    )
    assert applied["outcome"] == "refinalized"
    assert applied["publication"]["review"] == "skipped"
    assert not world.github.reviews
