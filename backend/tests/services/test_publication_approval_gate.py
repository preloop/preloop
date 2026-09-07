"""Approval must gate writer leases, not terminal status after a remote write."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import os
import hashlib

import pytest
from sqlalchemy.orm import Session

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.verification import (
    ResolvedVerificationPolicy,
    VerificationProfile,
)
from preloop.services.flow_artifacts import EvidenceUnavailableError
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.isolated_publication import IsolatedPublicationPolicy
from preloop.services.multi_repo_publication import (
    IncompleteMultiRepoPublicationError,
    IsolatedPublicationTarget,
    finish_multi_repo_isolated_publication,
)
from preloop.services.publication_verification import VerifiedPublication
from preloop.services.runner_service import hash_runner_token
from preloop.services import private_publication as publication
from preloop.services.trusted_publisher import PublicationError, PublicationLease

from tests.services.test_multi_repo_publication import (
    APP,
    FIRMWARE,
    _archive,
    _init_repo,
)
from tests.services.test_private_publication import IMAGE, MANIFEST, message, receipt

BRANCH = "preloop/change"


def _candidate(
    url: str,
    sha: str,
    *,
    branch: str = BRANCH,
    base: str = "main",
) -> dict[str, str]:
    return {
        "repository_url": url,
        "branch": branch,
        "base": base,
        "head_sha": sha,
    }


def _store_approval(
    db: Session,
    user: models.User,
    execution_id: Any,
    candidates: list[dict[str, str]],
    *,
    status: str = "approved",
    decided_by_ai: bool = False,
    expires_at: datetime | None = None,
    auto_approved_reason: str | None = None,
) -> models.ApprovalRequest:
    workflow = models.ApprovalWorkflow(
        account_id=user.account_id, name=f"publication-gate-{uuid4()}"
    )
    tool = models.ToolConfiguration(
        account_id=user.account_id,
        tool_name="isolated_publication",
        tool_source="builtin",
    )
    db.add_all([workflow, tool])
    db.flush()
    row = models.ApprovalRequest(
        account_id=user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        execution_id=str(execution_id),
        tool_name="isolated_publication",
        tool_args={
            "action": "isolated_publication",
            "candidates": candidates,
        },
        status=status,
        decided_by_ai=decided_by_ai,
        auto_approved_reason=auto_approved_reason,
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    return row


def _private_case(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    *,
    publication_approval: bool = False,
    extra_targets: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    config: dict[str, Any] = {"publication_mode": "isolated"}
    if publication_approval:
        config["publication_approval"] = True
    flow = crud_flow.create(
        db_session,
        account_id=test_user.account_id,
        flow_in=FlowCreate(
            name="Private publication gate",
            account_id=test_user.account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="implement",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
            timeout_seconds=600,
            git_clone_config=config,
        ),
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    policy = IsolatedPublicationPolicy(
        tracker_id=str(uuid4()),
        account_id=str(test_user.account_id),
        repository_url="https://github.com/example/project.git",
        branch=BRANCH,
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
    db_session.refresh(execution)
    if extra_targets:
        state = deepcopy(execution.result[publication.STATE_KEY])
        state["policy"]["targets"] = extra_targets
        state["policy"]["repository_url"] = extra_targets[0]["repository_url"]
        execution.result = {publication.STATE_KEY: state}
        db_session.commit()
        db_session.refresh(execution)
    state = deepcopy(execution.result[publication.STATE_KEY])
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "private-gate",
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

    async def mint(
        tracker_obj: Any, url: str, *, write: bool, client: Any
    ) -> PublicationLease:
        return PublicationLease(
            "write-only-secret",
            url,
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

    broker = AsyncMock(side_effect=mint)
    revoke = AsyncMock()
    monkeypatch.setattr(publication, "mint_repository_lease", broker)
    monkeypatch.setattr(publication, "revoke_repository_lease", revoke)
    monkeypatch.setattr(
        publication.crud_tracker,
        "get_by_id_and_account",
        lambda *args, **kwargs: SimpleNamespace(id=policy.tracker_id),
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
        db=db_session,
        user=test_user,
    )


async def _verified(case: SimpleNamespace, **extra: Any) -> dict[str, Any]:
    verify = await case.controller.handle(
        message(
            case,
            "publication_candidate",
            changed_files=["backend/api.py"],
            **extra,
        )
    )
    return message(
        case,
        "publication_verified",
        checks=[{**check, "exit_code": 0} for check in verify["checks"]],
        agent_removed=True,
        verifiers_removed=True,
        **extra,
    )


@pytest.mark.asyncio
async def test_private_missing_approval_never_mints_write_lease(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=True)
    verified = await _verified(case)
    with pytest.raises(PublicationError, match="human platform approval"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_source_base_approval_never_mints_changed_head(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=True)
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [_candidate(case.policy.repository_url, case.policy.base_sha)],
    )
    verified = await _verified(case)
    with pytest.raises(PublicationError, match="human platform approval"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_swapped_pairs_never_mint(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_head, app_head = "a" * 40, "b" * 40
    case = _private_case(
        db_session,
        test_user,
        monkeypatch,
        publication_approval=True,
        extra_targets=[
            {
                "tracker_id": str(uuid4()),
                "repository_url": FIRMWARE,
                "clone_path": "firmware",
                "role": "code",
                "branch": BRANCH,
                "base": "main",
                "expected_remote_sha": None,
                "base_sha": "e" * 40,
                "previous_records": [],
            },
            {
                "tracker_id": str(uuid4()),
                "repository_url": APP,
                "clone_path": "companion-app",
                "role": "code",
                "branch": BRANCH,
                "base": "release",
                "expected_remote_sha": None,
                "base_sha": "d" * 40,
                "previous_records": [],
            },
        ],
    )
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [
            _candidate(FIRMWARE, app_head),
            _candidate(APP, firmware_head, base="release"),
        ],
    )
    verified = await _verified(case, repository_url=FIRMWARE)
    with pytest.raises(PublicationError, match="human platform approval"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_expired_and_ai_approvals_never_mint(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=True)
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [_candidate(case.policy.repository_url, MANIFEST["head_sha"])],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [_candidate(case.policy.repository_url, MANIFEST["head_sha"])],
        decided_by_ai=True,
    )
    verified = await _verified(case)
    with pytest.raises(PublicationError, match="human platform approval"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", " "])
async def test_private_empty_or_whitespace_auto_approved_reason_never_mints(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=True)
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [_candidate(case.policy.repository_url, MANIFEST["head_sha"])],
        auto_approved_reason=reason,
    )
    verified = await _verified(case)
    with pytest.raises(PublicationError, match="human platform approval"):
        await case.controller.handle(verified)
    case.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_matching_candidate_approval_mints_write_lease(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=True)
    _store_approval(
        db_session,
        test_user,
        case.execution.id,
        [_candidate(case.policy.repository_url, MANIFEST["head_sha"])],
    )
    verified = await _verified(case)
    reply = await case.controller.handle(verified)
    assert reply["lease"]["token"] == "write-only-secret"
    case.broker.assert_awaited()
    ack = await case.controller.handle(
        message(case, "publication_complete", publication=receipt())
    )
    assert ack["type"] == "publication_ack"


@pytest.mark.asyncio
async def test_private_default_flow_without_opt_in_still_mints(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _private_case(db_session, test_user, monkeypatch, publication_approval=False)
    verified = await _verified(case)
    reply = await case.controller.handle(verified)
    assert reply["lease"]["token"] == "write-only-secret"
    case.broker.assert_awaited()


def _hosted_policy(
    *,
    account_id: str,
    execution_id: str,
    targets: tuple[IsolatedPublicationTarget, ...],
) -> IsolatedPublicationPolicy:
    primary = targets[0]
    return IsolatedPublicationPolicy(
        primary.tracker_id,
        account_id,
        primary.repository_url,
        primary.branch,
        primary.base,
        primary.expected_remote_sha,
        execution_id,
        (),
        None,
        "title",
        "body",
        "",
        primary.base_sha,
        SimpleNamespace(mode="gate", profile=object(), gate_budget_seconds=30),  # type: ignore[arg-type]
        "toolchain@sha256:" + "a" * 64,
        False,
        "n" * 64,
        targets,
        (),
    )


def _hosted_flow(db: Session, user: models.User, *, approval: bool) -> tuple[Any, Any]:
    config: dict[str, Any] = {"publication_mode": "isolated"}
    if approval:
        config["publication_approval"] = True
    flow = crud_flow.create(
        db,
        account_id=user.account_id,
        flow_in=FlowCreate(
            name="Hosted publication gate",
            account_id=user.account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="implement",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
            timeout_seconds=600,
            git_clone_config=config,
        ),
    )
    execution = crud_flow_execution.create(
        db, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    return flow, execution


@pytest.mark.asyncio
async def test_hosted_denied_approval_never_mints_or_pushes(
    tmp_path: Any, db_session: Session, test_user: models.User
) -> None:
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, execution = _hosted_flow(db_session, test_user, approval=True)
    _store_approval(
        db_session,
        test_user,
        execution.id,
        [_candidate(FIRMWARE, "e" * 40), _candidate(APP, "d" * 40, base="release")],
    )
    targets = (
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=FIRMWARE,
            clone_path="firmware",
            role="code",
            branch=BRANCH,
            base="main",
            expected_remote_sha=None,
            base_sha="e" * 40,
        ),
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=APP,
            clone_path="companion-app",
            role="code",
            branch=BRANCH,
            base="release",
            expected_remote_sha=None,
            base_sha="d" * 40,
        ),
    )
    policy = _hosted_policy(
        account_id=str(test_user.account_id),
        execution_id=str(execution.id),
        targets=targets,
    )
    archive = _archive({"firmware": fw_bundle, "companion-app": app_bundle})
    mint = AsyncMock()
    publish = AsyncMock()

    async def verify(target_policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(str(execution.id), head, digest)
        )

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
    ):
        with pytest.raises(IncompleteMultiRepoPublicationError) as raised:
            await finish_multi_repo_isolated_publication(
                db=db_session,
                policy=policy,
                agent_result={"result": {"status": "success"}},
                archive=archive,
                verify=verify,
            )
    mint.assert_not_awaited()
    publish.assert_not_awaited()
    assert "human platform approval" in str(raised.value.receipt)


@pytest.mark.asyncio
async def test_hosted_swapped_pairs_never_mints_or_pushes(
    tmp_path: Any, db_session: Session, test_user: models.User
) -> None:
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, execution = _hosted_flow(db_session, test_user, approval=True)
    _store_approval(
        db_session,
        test_user,
        execution.id,
        [
            _candidate(FIRMWARE, app_head),
            _candidate(APP, fw_head, base="release"),
        ],
    )
    targets = (
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=FIRMWARE,
            clone_path="firmware",
            role="code",
            branch=BRANCH,
            base="main",
            expected_remote_sha=None,
            base_sha="e" * 40,
        ),
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=APP,
            clone_path="companion-app",
            role="code",
            branch=BRANCH,
            base="release",
            expected_remote_sha=None,
            base_sha="d" * 40,
        ),
    )
    policy = _hosted_policy(
        account_id=str(test_user.account_id),
        execution_id=str(execution.id),
        targets=targets,
    )
    archive = _archive({"firmware": fw_bundle, "companion-app": app_bundle})
    mint = AsyncMock()
    publish = AsyncMock()

    async def verify(target_policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(str(execution.id), head, digest)
        )

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
    ):
        with pytest.raises(IncompleteMultiRepoPublicationError) as raised:
            await finish_multi_repo_isolated_publication(
                db=db_session,
                policy=policy,
                agent_result={"result": {"status": "success"}},
                archive=archive,
                verify=verify,
            )
    mint.assert_not_awaited()
    publish.assert_not_awaited()
    assert "human platform approval" in str(raised.value.receipt)


def _single_bundle_archive(bundle: bytes) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("evidence/branch.bundle")
        info.size = len(bundle)
        tar.addfile(info, io.BytesIO(bundle))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_hosted_partial_resume_requires_remaining_candidate(
    tmp_path: Any, db_session: Session, test_user: models.User
) -> None:
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, execution = _hosted_flow(db_session, test_user, approval=True)
    _store_approval(
        db_session,
        test_user,
        execution.id,
        [_candidate(FIRMWARE, "a" * 40)],
    )
    remaining = IsolatedPublicationTarget(
        tracker_id="tracker",
        repository_url=APP,
        clone_path="companion-app",
        role="code",
        branch=BRANCH,
        base="release",
        expected_remote_sha=None,
        base_sha="d" * 40,
    )
    policy = _hosted_policy(
        account_id=str(test_user.account_id),
        execution_id=str(execution.id),
        targets=(remaining,),
    )
    archive = _single_bundle_archive(app_bundle)
    mint = AsyncMock()
    publish = AsyncMock()

    async def verify(target_policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        return SimpleNamespace(
            verification=VerifiedPublication(str(execution.id), app_head, digest)
        )

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.isolated_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
    ):
        with pytest.raises(PublicationError, match="human platform approval"):
            await finish_multi_repo_isolated_publication(
                db=db_session,
                policy=policy,
                agent_result={"result": {"status": "success"}},
                archive=archive,
                verify=verify,
            )
    mint.assert_not_awaited()
    publish.assert_not_awaited()

    _store_approval(
        db_session,
        test_user,
        execution.id,
        [_candidate(APP, app_head, base="release")],
    )
    mint.reset_mock()
    publish.reset_mock()
    mint.return_value = PublicationLease(
        "write",
        APP,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    async def fake_publish(**kwargs: Any) -> dict[str, Any]:
        lease = await kwargs["acquire_lease"]()
        assert lease.token == "write"
        return {
            "url": "https://github.com/example/companion-app/pull/2",
            "number": 2,
            "branch": BRANCH,
            "provider": "github",
            "head_sha": app_head,
            "metadata_warnings": [],
        }

    publish.side_effect = fake_publish
    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.isolated_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.multi_repo_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
        patch(
            "preloop.services.isolated_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        result = await finish_multi_repo_isolated_publication(
            db=db_session,
            policy=policy,
            agent_result={"result": {"status": "success"}},
            archive=archive,
            verify=verify,
        )
    assert result["head_sha"] == app_head
    mint.assert_awaited()
    publish.assert_awaited()


@pytest.mark.asyncio
async def test_hosted_missing_saved_authority_never_mints_or_pushes(
    tmp_path: Any, db_session: Session
) -> None:
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    targets = (
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=FIRMWARE,
            clone_path="firmware",
            role="code",
            branch=BRANCH,
            base="main",
            expected_remote_sha=None,
            base_sha="e" * 40,
        ),
        IsolatedPublicationTarget(
            tracker_id="tracker",
            repository_url=APP,
            clone_path="companion-app",
            role="code",
            branch=BRANCH,
            base="release",
            expected_remote_sha=None,
            base_sha="d" * 40,
        ),
    )
    policy = _hosted_policy(
        account_id=str(uuid4()),
        execution_id=str(uuid4()),
        targets=targets,
    )
    archive = _archive({"firmware": fw_bundle, "companion-app": app_bundle})
    mint = AsyncMock()
    publish = AsyncMock()

    async def verify(target_policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(str(policy.execution_id), head, digest)
        )

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
    ):
        with pytest.raises(IncompleteMultiRepoPublicationError) as raised:
            await finish_multi_repo_isolated_publication(
                db=db_session,
                policy=policy,
                agent_result={"result": {"status": "success"}},
                archive=archive,
                verify=verify,
            )
    mint.assert_not_awaited()
    publish.assert_not_awaited()
    assert "human platform approval" in str(raised.value.receipt)


@pytest.mark.asyncio
async def test_hosted_default_saved_flow_mints_without_approval(
    tmp_path: Any, db_session: Session, test_user: models.User
) -> None:
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, execution = _hosted_flow(db_session, test_user, approval=False)
    remaining = IsolatedPublicationTarget(
        tracker_id="tracker",
        repository_url=APP,
        clone_path="companion-app",
        role="code",
        branch=BRANCH,
        base="release",
        expected_remote_sha=None,
        base_sha="d" * 40,
    )
    policy = _hosted_policy(
        account_id=str(test_user.account_id),
        execution_id=str(execution.id),
        targets=(remaining,),
    )
    archive = _single_bundle_archive(app_bundle)
    mint = AsyncMock(
        return_value=PublicationLease(
            "write",
            APP,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    publish = AsyncMock()

    async def verify(target_policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        return SimpleNamespace(
            verification=VerifiedPublication(str(execution.id), app_head, digest)
        )

    async def fake_publish(**kwargs: Any) -> dict[str, Any]:
        lease = await kwargs["acquire_lease"]()
        assert lease.token == "write"
        return {
            "url": "https://github.com/example/companion-app/pull/2",
            "number": 2,
            "branch": BRANCH,
            "provider": "github",
            "head_sha": app_head,
            "metadata_warnings": [],
        }

    publish.side_effect = fake_publish
    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=mint,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.isolated_publication.publish_verified_bundle",
            new=publish,
        ),
        patch(
            "preloop.services.multi_repo_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
        patch(
            "preloop.services.isolated_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        result = await finish_multi_repo_isolated_publication(
            db=db_session,
            policy=policy,
            agent_result={"result": {"status": "success"}},
            archive=archive,
            verify=verify,
        )
    assert result["head_sha"] == app_head
    mint.assert_awaited()
    publish.assert_awaited()


def _ordinary_orchestrator() -> FlowExecutionOrchestrator:
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.flow = SimpleNamespace(
        account_id="11111111-1111-4111-8111-111111111111",
        git_clone_config={},
        name="ordinary",
    )
    orchestrator.db = MagicMock()
    orchestrator.execution_id = "11111111-1111-4111-8111-111111111111"
    orchestrator.execution_log = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111"
    )
    orchestrator.trigger_event_data = {}
    orchestrator._isolated_publication_policy = None
    orchestrator.execution_logger = MagicMock()
    return orchestrator


@pytest.mark.asyncio
async def test_ordinary_missing_result_is_unchanged() -> None:
    orchestrator = _ordinary_orchestrator()
    payload = {"status": "SUCCEEDED"}
    with (
        patch(
            "preloop.models.crud.crud_approval_request.get_multi_by_execution"
        ) as approvals,
        patch("preloop.services.flow_artifacts.load_evidence") as evidence,
    ):
        await orchestrator._finish_isolated_publication(payload)
    assert payload == {"status": "SUCCEEDED"}
    approvals.assert_not_called()
    evidence.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_success_object_is_unchanged() -> None:
    orchestrator = _ordinary_orchestrator()
    result = {"status": "success", "ok": True, "items": [1]}
    payload = {"status": "SUCCEEDED", "result": result}
    with (
        patch(
            "preloop.models.crud.crud_approval_request.get_multi_by_execution"
        ) as approvals,
        patch("preloop.services.flow_artifacts.load_evidence") as evidence,
    ):
        await orchestrator._finish_isolated_publication(payload)
    assert payload["result"] is result
    assert "dossier_manifest" not in result
    assert "product_provenance" not in result
    approvals.assert_not_called()
    evidence.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_supplied_context_opts_into_dossier() -> None:
    orchestrator = _ordinary_orchestrator()
    orchestrator._product_evidence_context = {"product_evidence": True}
    result = {"status": "success"}
    payload = {"status": "SUCCEEDED", "result": result}
    with (
        patch(
            "preloop.models.crud.crud_approval_request.get_multi_by_execution",
            return_value=[],
        ) as approvals,
        patch(
            "preloop.services.flow_artifacts.load_evidence",
            side_effect=EvidenceUnavailableError(
                "missing", {"status": "missing", "kind": "evidence"}
            ),
        ) as evidence,
    ):
        await orchestrator._finish_isolated_publication(payload)
    approvals.assert_called()
    evidence.assert_called()
    assert "dossier_manifest" in payload["result"]
