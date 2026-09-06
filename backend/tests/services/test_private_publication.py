"""Real database and WebSocket contracts for trusted private publication."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.verification import (
    ResolvedVerificationPolicy,
    VerificationProfile,
)
from preloop.services.isolated_publication import IsolatedPublicationPolicy
from preloop.services import private_publication as publication
from preloop.services.runner_service import hash_runner_token, lease_job
from preloop.services.trusted_publisher import PublicationError, PublicationLease

MANIFEST = {
    "version": 1,
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "bundle_sha256": "c" * 64,
}
IMAGE = "example/helper@sha256:" + "d" * 64


@pytest.fixture
def case(db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(publication.settings, "preloop_url", "http://localhost:8000")
    flow = crud_flow.create(
        db_session,
        account_id=test_user.account_id,
        flow_in=FlowCreate(
            name="Private publication",
            account_id=test_user.account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="implement",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
            timeout_seconds=600,
            git_clone_config={"publication_mode": "isolated"},
        ),
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    policy = IsolatedPublicationPolicy(
        tracker_id=str(uuid4()),
        account_id=str(test_user.account_id),
        repository_url="https://github.com/example/project.git",
        branch="preloop/change",
        base="main",
        expected_remote_sha=None,
        execution_id=str(execution.id),
        previous_records=(),
        read_lease=PublicationLease(
            "read-only",
            "https://github.com/example/project.git",
            datetime.now(timezone.utc) + timedelta(hours=1),
        ),
        configured_title="",
        configured_body="",
        issue_number="1",
        base_sha="e" * 40,
        verification_policy=ResolvedVerificationPolicy(
            mode="gate",
            gate_budget_seconds=30,
            profile=VerificationProfile(
                profile_id="tests",
                always=[
                    {
                        "id": "unit",
                        "command": "pytest -q",
                        "reason": "tests",
                        "timeout_seconds": 900,
                    }
                ],
            ),
        ),
        verification_image=IMAGE,
        private=True,
        nonce="f" * 64,
    )
    publication.persist_private_publication(db_session, flow, policy)
    state = deepcopy(execution.result[publication.STATE_KEY])
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "private",
            "token_hash": hash_runner_token("runner-token"),
            "status": "online",
            "reported_status": "RUNNING",
            "last_heartbeat": datetime.now(timezone.utc),
            "current_execution_id": execution.id,
            "pending_job": {
                "_publication": state,
                "launch_version": 1,
                "agent_type": "codex",
                "execution_id": str(execution.id),
            },
            "publication_capabilities": {
                "connection_id": "conn",
                "version": 1,
                "helper_ready": True,
                "helper_image": IMAGE,
            },
        },
    )
    execution.runner_id = runner.id
    db_session.commit()
    broker = AsyncMock(
        return_value=PublicationLease(
            "write-only-secret",
            policy.repository_url,
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    revoke = AsyncMock()
    monkeypatch.setattr(publication, "mint_repository_lease", broker)
    monkeypatch.setattr(publication, "revoke_repository_lease", revoke)
    monkeypatch.setattr(
        publication.crud_tracker,
        "get_by_id_and_account",
        lambda *args, **kwargs: SimpleNamespace(id=policy.tracker_id),
    )
    events = []
    monkeypatch.setattr(
        runners,
        "emit_runner_updated",
        lambda row, db: events.append((row.status, row.current_execution_id)),
    )
    controller = publication.PrivatePublicationController(
        db_session,
        runner_id=runner.id,
        account_id=runner.account_id,
        connection_id="conn",
    )
    return SimpleNamespace(
        flow=flow,
        execution=execution,
        policy=policy,
        runner=runner,
        controller=controller,
        broker=broker,
        revoke=revoke,
        events=events,
        db=db_session,
    )


def message(case, kind: str, **extra):
    return {
        **MANIFEST,
        "type": kind,
        "execution_id": str(case.execution.id),
        "nonce": case.policy.nonce,
        **extra,
    }


async def verified_reply(case):
    verify = await case.controller.handle(
        message(case, "publication_candidate", changed_files=["backend/api.py"])
    )
    return message(
        case,
        "publication_verified",
        checks=[{**check, "exit_code": 0} for check in verify["checks"]],
        agent_removed=True,
        verifiers_removed=True,
    )


def receipt():
    return {
        "url": "https://github.com/example/project/pull/7",
        "number": 7,
        "branch": "preloop/change",
        "provider": "github",
        "head_sha": "a" * 40,
        "metadata_warnings": [],
    }


@pytest.mark.asyncio
async def test_success_consumes_state_before_broker_and_preserves_independent_budgets(
    case,
):
    verified = await verified_reply(case)
    assert verified["checks"][0]["timeout_seconds"] == 900
    issued = case.broker.return_value

    async def mint(*args, **kwargs):
        case.db.refresh(case.execution)
        assert case.execution.result[publication.STATE_KEY]["phase"] == "publishing"
        return issued

    case.broker.side_effect = mint
    reply = await case.controller.handle(verified)
    assert reply["lease"]["token"] == "write-only-secret"
    assert "write-only-secret" not in json.dumps(case.runner.pending_job)
    assert "write-only-secret" not in json.dumps(case.execution.result)
    ack = await case.controller.handle(
        message(case, "publication_complete", publication=receipt())
    )
    assert ack["type"] == "publication_ack"
    case.revoke.assert_awaited_once()
    assert publication.trusted_private_receipt(case.execution)["number"] == 7
    await case.controller.close()
    case.db.refresh(case.execution)
    assert case.execution.result[publication.STATE_KEY]["phase"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["nonce", "execution", "account", "cancelled", "expired", "reconnected", "config"],
)
async def test_stale_authority_never_mints(case, mutation):
    verified = await verified_reply(case)
    if mutation == "nonce":
        verified["nonce"] = "0" * 64
    elif mutation == "execution":
        verified["execution_id"] = str(uuid4())
    elif mutation == "account":
        case.controller.account_id = uuid4()
    elif mutation == "cancelled":
        case.runner.halt_requested = True
    elif mutation == "expired":
        state = deepcopy(case.execution.result[publication.STATE_KEY])
        state["deadline"] = 0
        case.execution.result = {publication.STATE_KEY: state}
    elif mutation == "reconnected":
        case.runner.publication_capabilities = {
            "connection_id": "new",
            "helper_ready": True,
        }
    elif mutation == "config":
        case.flow.prompt_template = "new"
        case.flow.agent_config = {"model": "changed"}
    case.db.commit()
    with pytest.raises(PublicationError):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "command",
        "exit",
        "bool_exit",
        "agent",
        "verifier",
        "head",
        "tree",
        "bundle",
    ],
)
async def test_verification_requires_exact_manifest_checks_and_removal(case, mutation):
    verified = await verified_reply(case)
    if mutation == "missing":
        verified["checks"] = []
    elif mutation == "extra":
        verified["checks"].append(
            {"id": "extra", "command": "true", "timeout_seconds": 1, "exit_code": 0}
        )
    elif mutation == "command":
        verified["checks"][0]["command"] = "true"
    elif mutation == "exit":
        verified["checks"][0]["exit_code"] = 1
    elif mutation == "bool_exit":
        verified["checks"][0]["exit_code"] = False
    elif mutation == "agent":
        verified["agent_removed"] = False
    elif mutation == "verifier":
        verified["verifiers_removed"] = False
    else:
        verified[
            {"head": "head_sha", "tree": "tree_sha", "bundle": "bundle_sha256"}[
                mutation
            ]
        ] = "0" * (64 if mutation == "bundle" else 40)
    with pytest.raises(PublicationError):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paths",
    [["../escape"], ["/etc/passwd"], ["x", "x"], ["x\\y"], ["x\nsecret"], [None]],
)
async def test_invalid_manifest_rejected(case, paths):
    with pytest.raises(PublicationError):
        await case.controller.handle(
            message(case, "publication_candidate", changed_files=paths)
        )
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_verified_event_never_reissues_writer(case):
    verified = await verified_reply(case)
    await case.controller.handle(verified)
    with pytest.raises(PublicationError):
        await case.controller.handle(verified)
    assert case.broker.await_count == 1
    await case.controller.close()
    assert case.revoke.await_count == 1


@pytest.mark.asyncio
async def test_cancel_during_broker_revokes_before_delivery(case):
    verified = await verified_reply(case)
    lease = case.broker.return_value

    async def cancel(*args, **kwargs):
        case.runner.halt_requested = True
        case.db.commit()
        return lease

    case.broker.side_effect = cancel
    with pytest.raises(PublicationError):
        await case.controller.handle(verified)
    case.revoke.assert_awaited_once()
    assert case.controller.writer is None


def test_connection_cleanup_is_compare_and_clear(case):
    assert not crud_flow_runner.set_publication_capabilities(
        case.db,
        runner_id=case.runner.id,
        capabilities={},
        expected_connection_id="old",
        offline=True,
    )
    case.db.refresh(case.runner)
    assert case.runner.publication_capabilities["connection_id"] == "conn"
    assert case.runner.status == "online"


@pytest.mark.parametrize("capable", [False, True])
def test_lease_requires_ready_publication_helper(case, capable):
    case.runner.pending_job = None
    case.runner.current_execution_id = None
    case.execution.runner_id = None
    case.runner.publication_capabilities = {
        "version": 1,
        "helper_ready": capable,
        "helper_image": IMAGE,
    }
    case.db.commit()
    leased = lease_job(
        case.db,
        account_id=case.runner.account_id,
        pool="auto",
        execution_id=case.execution.id,
        payload={
            "_publication": case.execution.result[publication.STATE_KEY],
            "agent_type": "codex",
        },
    )
    assert (leased is not None) is capable
    case.db.refresh(case.execution)
    assert case.execution.runner_id == (case.runner.id if capable else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["success", "FAILED", "STOPPED", "disconnect", "unregister", "forged"]
)
async def test_real_ws_protocol_and_all_terminal_credential_paths(
    case, ending, monkeypatch
):
    frames = []
    requests = []
    initial_job = {
        "execution_id": str(case.execution.id),
        "agent_type": "codex",
        "launch_version": 1,
        "publication": publication.public_publication_descriptor(
            case.execution.result[publication.STATE_KEY]
        ),
    }
    verify = None

    async def send(frame):
        nonlocal verify
        frames.append(frame)
        if frame.get("type") == "publication_verify":
            verify = frame

    stage = 0

    async def receive():
        nonlocal stage
        stage += 1
        if stage == 1:
            return {
                "type": "heartbeat",
                "publication_capabilities": {
                    "version": 1,
                    "helper_ready": True,
                    "helper_image": IMAGE,
                },
            }
        if stage == 2 and ending == "forged":
            return message(
                case,
                "complete",
                status="SUCCEEDED",
                completion_protocol="docker_v1",
                launch_version=1,
                exit_code=0,
                result={
                    "status": "success",
                    "trusted_publication": receipt(),
                    "_private_publication": {"phase": "complete"},
                },
            )
        if stage == 2:
            return message(
                case, "publication_candidate", changed_files=["backend/api.py"]
            )
        if stage == 3:
            if ending == "forged":
                raise WebSocketDisconnect()
            return message(
                case,
                "publication_verified",
                checks=[{**check, "exit_code": 0} for check in verify["checks"]],
                agent_removed=True,
                verifiers_removed=True,
            )
        if stage == 4:
            if ending == "success":
                return message(case, "publication_complete", publication=receipt())
            if ending == "disconnect":
                raise WebSocketDisconnect()
            if ending == "unregister":
                return {"type": "unregister"}
            return message(case, "complete", status=ending, result={"status": "failed"})
        if stage == 5 and ending == "success":
            return message(
                case,
                "complete",
                status="SUCCEEDED",
                completion_protocol="docker_v1",
                launch_version=1,
                exit_code=0,
                result={
                    "status": "success",
                    "trusted_publication": {"forged": True},
                    "_private_publication": {"forged": True},
                },
            )
        raise WebSocketDisconnect()

    websocket = MagicMock()
    websocket.query_params = {"token": "runner-token"}
    websocket.headers = {}
    websocket.accept = AsyncMock()
    websocket.send_json = send

    async def record_receive():
        frame = await receive()
        if frame.get("type") in {
            "publication_candidate",
            "publication_verified",
            "publication_complete",
        }:
            requests.append(deepcopy(frame))
        return frame

    websocket.receive_json = record_receive
    monkeypatch.setattr(
        runners.crud_api_key, "deactivate_runtime_keys_for_flow_execution", MagicMock()
    )
    await runners.runner_ws(websocket, case.runner.id, case.db)
    case.db.refresh(case.execution)
    if ending == "success":
        assert case.execution.status == "SUCCEEDED"
        assert ("online", None) in case.events
        assert publication.trusted_private_receipt(case.execution)["number"] == 7
        captured = {
            "job": initial_job,
            "requests": requests,
            "replies": [
                frame
                for frame in frames
                if frame.get("type")
                in {"publication_verify", "publication_publish", "publication_ack"}
            ],
        }
        captured["replies"][1]["lease"]["expires_at"] = "2099-01-01T00:00:00+00:00"
        serialized = (
            json.dumps(captured, indent=2).replace(
                str(case.execution.id), "11111111-1111-4111-8111-111111111111"
            )
            + "\n"
        )
        if os.getenv("PRELOOP_WRITE_PUBLICATION_FIXTURE"):
            Path(os.environ["PRELOOP_WRITE_PUBLICATION_FIXTURE"]).write_text(serialized)
        fixture_path = (
            Path(__file__).parents[1] / "fixtures" / "private_publication_protocol.json"
        )
        assert json.loads(serialized) == json.loads(fixture_path.read_text())
        assert any(frame.get("type") == "publication_ack" for frame in frames)
    elif ending == "forged":
        assert case.execution.status == "FAILED"
        assert "trusted_publication" not in case.execution.result
        case.broker.assert_not_awaited()
    else:
        assert case.execution.result[publication.STATE_KEY]["phase"] == "failed"
    assert case.revoke.await_count == (0 if ending == "forged" else 1)


@pytest.mark.asyncio
async def test_zero_selected_checks_cannot_mint(case):
    state = deepcopy(case.execution.result[publication.STATE_KEY])
    state["policy"]["verification_policy"]["profile"]["always"] = []
    case.execution.result = {publication.STATE_KEY: state}
    case.db.commit()
    with pytest.raises(PublicationError, match="nonempty"):
        await case.controller.handle(
            message(case, "publication_candidate", changed_files=["README.md"])
        )
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_atomic_phase_compare_and_swap_rejects_duplicate_snapshot(case):
    old = deepcopy(case.execution.result[publication.STATE_KEY])
    await verified_reply(case)
    with pytest.raises(ValueError, match="already consumed"):
        crud_flow_runner.transition_publication(
            case.db,
            runner_id=case.runner.id,
            account_id=case.runner.account_id,
            execution_id=case.execution.id,
            nonce=case.policy.nonce,
            expected=old,
            updated={**old, "phase": "publishing"},
        )
    case.db.rollback()
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_uses_saved_binding_and_read_only_credentials(case):
    context = {
        "execution_id": str(case.execution.id),
        "git_clone_config": {},
        "trigger_event_data": {"_resume": {"source_branch": "untrusted"}},
    }
    restored = await publication.restore_private_publication(
        case.db, case.flow, context
    )
    assert restored.base_sha == case.policy.base_sha
    assert restored.nonce == case.policy.nonce
    assert context["git_clone_config"]["target_branch"] == case.policy.branch
    assert (
        context["trigger_event_data"]["_resume"]["source_branch"] == case.policy.branch
    )
    assert case.broker.await_args.kwargs["write"] is False
    publication.persist_private_publication(case.db, case.flow, restored)
    await verified_reply(case)
    with pytest.raises(PublicationError, match="consumed launch"):
        await publication.restore_private_publication(case.db, case.flow, context)
    assert case.broker.await_count == 1
    monitoring = publication.load_private_monitoring_policy(
        case.db, case.flow, case.execution
    )
    assert monitoring.read_lease is None
    assert case.broker.await_count == 1


@pytest.mark.asyncio
async def test_watchdog_serializes_with_messages_and_uses_independent_session(
    case, monkeypatch
):
    from preloop.models.db import session as sessions

    entered, release = asyncio.Event(), asyncio.Event()
    independent = MagicMock(spec=Session)

    def get_session():
        yield independent

    monkeypatch.setattr(sessions, "get_db_session", get_session)
    abandon = MagicMock()
    monkeypatch.setattr(crud_flow_runner, "abandon_publication", abandon)

    async def pending(_):
        entered.set()
        await release.wait()
        return {}

    monkeypatch.setattr(case.controller, "_handle", pending)
    case.controller.execution_id = case.execution.id
    case.controller.nonce = case.policy.nonce
    case.controller.writer = case.broker.return_value
    task = asyncio.create_task(case.controller.handle({}))
    await entered.wait()
    timer = asyncio.create_task(case.controller._expire_writer(0))
    await asyncio.sleep(0)
    case.revoke.assert_not_awaited()
    release.set()
    await task
    await timer
    case.revoke.assert_awaited_once()
    assert abandon.call_args.args[0] is independent
    assert abandon.call_args.args[0] is not case.db


@pytest.mark.asyncio
async def test_expire_writer_abandons_when_revoke_fails(case, monkeypatch, caplog):
    from preloop.models.db import session as sessions

    independent = MagicMock(spec=Session)

    def get_session():
        yield independent

    monkeypatch.setattr(sessions, "get_db_session", get_session)
    abandon = MagicMock()
    monkeypatch.setattr(crud_flow_runner, "abandon_publication", abandon)
    case.controller.execution_id = case.execution.id
    case.controller.nonce = case.policy.nonce
    case.revoke.side_effect = PublicationError("writer already gone")
    with caplog.at_level("WARNING"):
        await case.controller._expire_writer(0)
    case.revoke.assert_awaited_once()
    assert abandon.call_args.args[0] is independent
    assert "Background writer revocation failed" in caplog.text


@pytest.mark.asyncio
async def test_connection_replacement_during_unregister_preserves_new_owner(
    case, monkeypatch
):
    verified = await verified_reply(case)
    await case.controller.handle(verified)

    async def replace(*args):
        crud_flow_runner.set_publication_capabilities(
            case.db,
            runner_id=case.runner.id,
            capabilities={
                "connection_id": "replacement",
                "helper_ready": True,
                "version": 1,
            },
        )

    case.revoke.side_effect = replace
    await case.controller.close()
    assert not crud_flow_runner.set_publication_capabilities(
        case.db,
        runner_id=case.runner.id,
        capabilities={},
        expected_connection_id="conn",
        clear_lease=True,
        offline=True,
    )
    case.db.refresh(case.runner)
    assert case.runner.current_execution_id == case.execution.id
    assert case.runner.publication_capabilities["connection_id"] == "replacement"


@pytest.mark.asyncio
async def test_verification_deadline_does_not_include_publisher_allowance(case):
    verified = await verified_reply(case)
    state = deepcopy(case.execution.result[publication.STATE_KEY])
    state["verification_deadline"] = 0
    assert state["deadline"] > datetime.now(timezone.utc).timestamp()
    case.execution.result = {publication.STATE_KEY: state}
    case.db.commit()
    with pytest.raises(PublicationError, match="verification budget"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_delivery_strips_internal_snapshot_and_rejects_consumed_launch(
    case, monkeypatch
):
    from preloop.agents.runner_launch import prepare_runner_delivery

    monkeypatch.setattr(
        "preloop.agents.runner_launch.build_runner_launch",
        AsyncMock(return_value={"version": 1, "script": "bootstrap", "env": {}}),
    )
    job = deepcopy(case.runner.pending_job)
    delivered = await prepare_runner_delivery(case.db, job, {})
    assert "_publication" not in delivered
    assert "policy" not in json.dumps(delivered["publication"])
    assert delivered["publication"]["base_sha"] == case.policy.base_sha
    await verified_reply(case)
    delivered = await prepare_runner_delivery(case.db, case.runner.pending_job, {})
    assert "launch_error" in delivered
    assert "launch" not in delivered
    assert "_publication" not in delivered
