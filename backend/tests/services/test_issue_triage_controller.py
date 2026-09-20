"""Durable triage retry, freshness, and bounded-packet controller regressions."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue_lifecycle
from preloop.models.crud.base import CRUDBase
from preloop.schemas.issue_triage import IssueTriageApply
from preloop.services import issue_triage_controller as controller
from preloop.services.issue_triage import get_context
from tests.services.test_issue_triage_controller_review import (
    _apply,
    _claim,
    _committed_rig,
    _rig,
)


@pytest.mark.asyncio
async def test_manual_and_event_claims_concurrently_reserve_one_revision(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _committed_rig(db_engine, monkeypatch) as rig, Session(db_engine) as second:
        first = rig.db
        entered, release = asyncio.Event(), asyncio.Event()
        original_read = rig.provider.read_issue
        first_read = True

        async def hold_first_read() -> Any:
            nonlocal first_read
            if first_read:
                first_read = False
                entered.set()
                await release.wait()
            return await original_read()

        rig.provider.read_issue = hold_first_read
        manual = asyncio.create_task(
            controller.reserve_triage_execution(
                first, flow=rig.flow, event={**rig.event, "type": "issue_run"}
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        other_flow = crud_issue_lifecycle.triage_flow(
            second, account_id=rig.account_id, flow_id=rig.flow.id
        )
        automatic = asyncio.create_task(
            controller.reserve_triage_execution(
                second, flow=other_flow, event={**rig.event, "type": "issue_updated"}
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert not automatic.done()
        finally:
            release.set()
        (execution, reused), (duplicate, repeated) = await asyncio.wait_for(
            asyncio.gather(manual, automatic), 3
        )
        assert execution.id == duplicate.id and not reused and repeated
        rows = crud_issue_lifecycle.list_for_issue(
            first, account_id=rig.account_id, issue_id=rig.issue.id
        )
        assert len([row for row in rows if row.kind == "triage"]) == 1


@pytest.mark.asyncio
async def test_completed_assessment_replays_without_reapplying_or_starting_work(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    assert (await _apply(rig, execution, request)).status == "updated"
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
    )
    duplicate, reused = await controller.reserve_triage_execution(
        db_session,
        flow=rig.flow,
        event={**rig.event, "triage_context": {"revision": "forged"}},
    )
    assert duplicate.id == execution.id and reused
    assert (await _apply(rig, execution, request)).status == "unchanged"
    assert rig.provider.operations == ["content", "labels"]
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    packet = row.data["packet"]
    assert packet["source_revision"] == request.expected_revision
    assert packet["resulting_provider_revision"] == rig.provider.issue.revision
    assert packet["source_provider_revision"] != packet["resulting_provider_revision"]
    assert packet["evidence"] == {
        "repository": "unknown",
        "pull_requests": "unknown",
        "label_catalogue": "complete",
    }
    assert packet["limitations"]
    with pytest.raises(
        controller.TriageControllerError, match="replay_request_mismatch"
    ):
        await _apply(
            rig,
            execution,
            request.model_copy(
                update={"assessment": "Replace the approved assessment"}
            ),
        )


@pytest.mark.asyncio
async def test_newer_edit_has_own_claim_and_old_output_cannot_overwrite_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    rig.provider.issue.body += "\nNew human acceptance criterion."
    newer, reused = await controller.reserve_triage_execution(
        db_session, flow=rig.flow, event=rig.event
    )
    assert newer.id != execution.id and not reused
    result = await _apply(rig, execution, request)
    assert result.status == "conflict" and result.reason == "stale_issue"
    assert rig.provider.operations == []
    assert "New human acceptance" in rig.provider.issue.body
    refreshed = request.model_copy(
        update={
            "expected_revision": (await get_context(rig.provider)).expected_revision
        }
    )
    assert (await _apply(rig, newer, refreshed)).status == "updated"


@pytest.mark.asyncio
async def test_lost_body_response_recovers_only_the_recorded_intent(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    original = rig.provider.write_content

    async def write_then_lose_response(title: str, body: str) -> None:
        await original(title, body)
        raise TimeoutError("Response lost after remote effect")

    rig.provider.write_content = write_then_lose_response
    partial = await _apply(rig, execution, request)
    assert partial.status == "partial" and rig.provider.operations == ["content"]
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    assert "packet" not in row.data
    assert rig.provider.issue.revision in row.data["receipt"]["expected_revisions"]
    with pytest.raises(
        controller.TriageControllerError, match="recovery_request_mismatch"
    ):
        await _apply(
            rig,
            execution,
            request.model_copy(update={"assessment": "Forged replacement"}),
        )
    rig.provider.write_content = original
    recovered = await _apply(rig, execution, request)
    assert recovered.status == "updated" and recovered.cache_updated
    assert rig.provider.operations == ["content", "labels"]
    assert row.data["packet"]["source_revision"] == request.expected_revision


@pytest.mark.asyncio
async def test_explicit_failed_retry_preserves_lineage_and_old_key_restrictions(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    old_id = execution.id
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "FAILED"}
    )
    ordinary, reused = await controller.reserve_triage_execution(
        db_session, flow=rig.flow, event=rig.event
    )
    assert reused and ordinary.id == old_id
    replacement, reused = await controller.reserve_triage_execution(
        db_session, flow=rig.flow, event=rig.event, retry_of_execution_id=old_id
    )
    assert not reused and replacement.id != old_id
    assert replacement.retry_of_execution_id == old_id
    with pytest.raises(
        controller.TriageControllerError, match="execution_issue_mismatch"
    ):
        await _apply(rig, execution, request)
    CRUDBase(models.Flow).update(
        db_session, db_obj=rig.flow, obj_in={"name": "Renamed flow"}
    )
    assert controller.is_triage_execution(
        db_session, execution_id=old_id, account_id=rig.account_id
    )
    rows = crud_issue_lifecycle.list_for_issue(
        db_session, account_id=rig.account_id, issue_id=rig.issue.id
    )
    assert any(
        row.kind == "triage_attempt" and row.execution_id == old_id for row in rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["SUCCEEDED", "FAILED", "CANCELLED", "STOPPED", "TIMED_OUT", "ABORTED"]
)
async def test_terminal_execution_cannot_apply_unfinished_assessment(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": status}
    )
    with pytest.raises(controller.TriageControllerError, match="execution_not_active"):
        await _apply(rig, execution, request)
    assert rig.provider.operations == []


@pytest.mark.asyncio
async def test_cache_failure_does_not_publish_applicable_packet_and_can_recover(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    original = crud_issue_lifecycle.triage_snapshot
    monkeypatch.setattr(
        crud_issue_lifecycle,
        "triage_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SQLAlchemyError("cache unavailable")
        ),
    )
    result = await _apply(rig, execution, request)
    assert result.status == "partial" and not result.cache_updated
    assert result.reason == "provider_result_cache_failed"
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    assert "packet" not in row.data and row.data["receipt"]
    monkeypatch.setattr(crud_issue_lifecycle, "triage_snapshot", original)
    recovered = await _apply(rig, execution, request)
    assert recovered.status == "unchanged" and recovered.cache_updated
    assert rig.provider.operations == ["content", "labels"]


@pytest.mark.asyncio
async def test_packet_byte_limit_is_honest_after_verified_provider_apply(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    monkeypatch.setattr(controller, "MAX_PACKET_BYTES", 100)
    result = await _apply(rig, execution, request)
    assert result.status == "partial" and result.cache_updated
    assert result.reason == "triage_context_packet_too_large"
    row = crud_issue_lifecycle.triage_for_execution(
        db_session, account_id=rig.account_id, execution_id=execution.id
    )
    assert "packet" not in row.data


@pytest.mark.asyncio
async def test_disabled_flow_cannot_claim_apply_or_supply_a_packet(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    assert (await _apply(rig, execution, request)).status == "updated"
    CRUDBase(models.Flow).update(
        db_session, db_obj=rig.flow, obj_in={"is_enabled": False}
    )
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
    with pytest.raises(controller.TriageControllerError, match="flow_unavailable"):
        await controller.reserve_triage_execution(
            db_session, flow=rig.flow, event=rig.event
        )
    with pytest.raises(controller.TriageControllerError, match="context_changed"):
        await _apply(rig, execution, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {},
        {"project_id": "not-a-uuid"},
        {"payload": ["bad"]},
        {"project_id": str(uuid4()), "payload": {"issue": "bad"}},
    ],
)
async def test_malformed_target_never_calls_provider(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
) -> None:
    rig = _rig(db_session, monkeypatch)
    with pytest.raises(controller.TriageControllerError):
        await controller.reserve_triage_execution(
            db_session, flow=rig.flow, event=event
        )
    controller.authorized_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_retry_receives_original_partial_write_request(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    execution, request = await _claim(rig)
    original = rig.provider.update_labels

    async def unavailable_labels(add: list[str], remove: list[str]) -> None:
        raise TimeoutError("Label response unavailable")

    rig.provider.update_labels = unavailable_labels
    assert (await _apply(rig, execution, request)).status == "partial"
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "FAILED"}
    )
    replacement, coalesced = await controller.reserve_triage_execution(
        db_session,
        flow=rig.flow,
        event=rig.event,
        retry_of_execution_id=execution.id,
    )
    assert not coalesced
    recovery = IssueTriageApply.model_validate(
        replacement.trigger_event_details["triage_context"]["recovery_request"]
    )
    assert recovery == request
    rig.provider.update_labels = original
    assert (await _apply(rig, replacement, recovery)).status == "updated"
    assert rig.provider.operations == ["content", "labels"]


@pytest.mark.asyncio
async def test_failed_context_read_does_not_reserve_untracked_execution(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(db_session, monkeypatch)
    rig.provider.read_issue = AsyncMock(
        side_effect=TimeoutError("provider unavailable")
    )
    with pytest.raises(controller.TriageControllerError, match="context_unavailable"):
        await controller.reserve_triage_execution(
            db_session, flow=rig.flow, event=rig.event
        )
    assert (
        crud_issue_lifecycle.list_for_issue(
            db_session, account_id=rig.account_id, issue_id=rig.issue.id
        )
        == []
    )


@pytest.mark.asyncio
async def test_lock_contention_is_a_bounded_controller_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    rig = _rig(db_session, monkeypatch)

    @asynccontextmanager
    async def busy(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("triage_operation_in_progress")
        yield

    monkeypatch.setattr(crud_issue_lifecycle, "triage_locked", busy)
    with pytest.raises(controller.TriageControllerError, match="operation_in_progress"):
        await controller.reserve_triage_execution(
            db_session, flow=rig.flow, event=rig.event
        )
    controller.authorized_provider.assert_not_awaited()
