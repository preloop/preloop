"""Unpublished repair work resumes with the original publication authority."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from preloop.services.isolated_publication import _feedback_ancestor_publication
from preloop.services.trusted_publisher import PublicationError


def lineage() -> tuple[
    SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace, dict
]:
    flow = SimpleNamespace(id=uuid4(), account_id=uuid4())
    thread = SimpleNamespace(
        id=uuid4(),
        active_execution_id=uuid4(),
        latest_execution_id=uuid4(),
        provider="github",
        branch="preloop/issue-1",
        pr_number="1",
        pr_url="https://github.com/example/project/pull/1",
        context={},
    )
    receipt = {
        "provider": "github",
        "branch": thread.branch,
        "url": thread.pr_url,
        "repository_url": "https://github.com/example/project.git",
        "head_sha": "a" * 40,
    }
    original = SimpleNamespace(
        id=uuid4(),
        flow_id=flow.id,
        result={"trusted_publication": receipt},
        trigger_event_details={"_session_thread_id": str(thread.id)},
    )
    failed = SimpleNamespace(
        id=thread.latest_execution_id,
        flow_id=flow.id,
        result={"status": "failure"},
        trigger_event_details={
            "_thread_id": str(thread.id),
            "_resume": {"execution_id": str(original.id)},
        },
        cli_session={"session_id": "failed-repair-native-session"},
    )
    context = {
        "execution_id": str(thread.active_execution_id),
        "trigger_event_data": {
            "_resume": {"execution_id": str(failed.id), "thread_id": str(thread.id)},
        },
    }
    return flow, thread, original, failed, context


def test_failed_repair_keeps_workspace_source_and_recovers_only_published_binding() -> (
    None
):
    flow, thread, original, failed, context = lineage()
    snapshot = deepcopy(context)
    with (
        patch(
            "preloop.services.isolated_publication.crud_flow_feedback.owned_thread",
            return_value=thread,
        ) as owned,
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            return_value=original,
        ) as get,
    ):
        receipt = _feedback_ancestor_publication(
            None,
            flow=flow,
            context=context,
            prior=failed,
            resume=context["trigger_event_data"]["_resume"],
        )
    assert receipt is original.result["trusted_publication"]
    assert context == snapshot
    assert failed.cli_session["session_id"] == "failed-repair-native-session"
    assert "trusted_publication" not in failed.result
    assert get.call_args.kwargs["account_id"] == str(flow.account_id)
    assert owned.call_args.kwargs["flow_id"] == flow.id
    assert owned.call_args.kwargs["account_id"] == flow.account_id


@pytest.mark.parametrize(
    "damage",
    [
        "missing_thread",
        "other_active",
        "other_latest",
        "other_flow",
        "other_thread",
        "cycle",
        "missing_parent",
        "other_url",
        "other_branch",
        "other_repository",
        "wrong_sha",
        "missing_receipt",
        "missing_execution",
    ],
)
def test_unowned_or_broken_lineage_never_recovers_authority(damage: str) -> None:
    flow, thread, original, failed, context = lineage()
    if damage == "missing_thread":
        thread = None
    elif damage == "other_active":
        thread.active_execution_id = uuid4()
    elif damage == "other_latest":
        thread.latest_execution_id = uuid4()
    elif damage == "other_flow":
        original.flow_id = uuid4()
    elif damage == "other_thread":
        original.trigger_event_details["_session_thread_id"] = str(uuid4())
    elif damage == "cycle":
        original = failed
    elif damage == "missing_parent":
        failed.trigger_event_details.pop("_resume")
    elif damage == "missing_receipt":
        original.result = {}
    elif damage == "missing_execution":
        original = None
    else:
        key = {
            "other_url": "url",
            "other_branch": "branch",
            "other_repository": "repository_url",
            "wrong_sha": "head_sha",
        }[damage]
        original.result["trusted_publication"][key] = "wrong"
    with (
        patch(
            "preloop.services.isolated_publication.crud_flow_feedback.owned_thread",
            return_value=thread,
        ),
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            return_value=original,
        ),
        pytest.raises(PublicationError, match="trusted publication binding"),
    ):
        _feedback_ancestor_publication(
            None,
            flow=flow,
            context=context,
            prior=failed,
            resume=context["trigger_event_data"]["_resume"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [True, False])
async def test_prepare_after_failed_repair_uses_current_gate_and_original_branch(
    durable: bool,
) -> None:
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    import httpx

    from preloop.services.isolated_publication import prepare_isolated_publication
    from preloop.services.trusted_publisher import PublicationLease

    flow, thread, original, failed, context = lineage()
    resume = context["trigger_event_data"]["_resume"]
    resume["cli_session"] = failed.cli_session
    if not durable:
        resume.pop("thread_id")
    snapshot = deepcopy(resume)
    context["git_clone_config"] = {
        "publication_mode": "isolated",
        "verification": {
            "mode": "gate",
            "image": "toolchain@sha256:" + "a" * 64,
            "profile": {
                "profile_id": "current-policy",
                "version": "v1",
                "always": [
                    {"id": "required", "command": "exit 7", "reason": "current gate"}
                ],
            },
        },
        "repositories": [
            {
                "repository_url": "https://github.com/example/project.git",
                "tracker_id": "tracker",
            }
        ],
    }
    read = PublicationLease(
        "fake-read-only",
        "https://github.com/example/project.git",
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    client = AsyncMock()
    client.get.side_effect = [
        httpx.Response(
            200, json=data, request=httpx.Request("GET", "https://api.github.com")
        )
        for data in [
            {"full_name": "example/project", "default_branch": "main"},
            {"object": {"sha": "b" * 40}},
            {"object": {"sha": "a" * 40}},
        ]
    ]
    with (
        patch("preloop.services.runner_service.resolve_runner_pool", return_value=None),
        patch(
            "preloop.services.isolated_publication.crud_flow_feedback.owned_thread",
            return_value=thread,
        ),
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            side_effect=[failed, original],
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker", tracker_type="github"),
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(return_value=read),
        ) as mint,
        patch("preloop.services.isolated_publication.httpx.AsyncClient") as factory,
    ):
        factory.return_value.__aenter__.return_value = client
        if not durable:
            with pytest.raises(PublicationError, match="trusted publication binding"):
                await prepare_isolated_publication(None, flow, context)
            mint.assert_not_awaited()
            return
        policy = await prepare_isolated_publication(None, flow, context)
    assert policy.execution_id == context["execution_id"]
    assert policy.branch == thread.branch
    assert (
        policy.expected_remote_sha == original.result["trusted_publication"]["head_sha"]
    )
    assert policy.verification_policy.profile.profile_id == "current-policy"
    assert policy.verification_policy.profile.always[0].command == "exit 7"
    assert context["trigger_event_data"]["_resume"] == {
        **snapshot,
        "source_branch": thread.branch,
    }
    assert mint.call_args.kwargs["write"] is False


@pytest.mark.parametrize(
    "binding", ["owned", "other_source", "no_receipt", "foreign_thread"]
)
@pytest.mark.parametrize("recovery_mode", ["native_resume", "published_branch_handoff"])
def test_explicit_adoption_only_accepts_exact_published_source(
    binding: str, recovery_mode: str
) -> None:
    flow, thread, original, failed, context = lineage()
    original.trigger_event_details = {}
    thread.context = {
        "adoption": {
            "source_execution_id": str(original.id),
            "recovery_mode": recovery_mode,
        }
    }
    if binding == "other_source":
        thread.context["adoption"]["source_execution_id"] = str(uuid4())
    elif binding == "no_receipt":
        original.result = {}
    elif binding == "foreign_thread":
        original.trigger_event_details = {"_session_thread_id": str(uuid4())}
    with (
        patch(
            "preloop.services.isolated_publication.crud_flow_feedback.owned_thread",
            return_value=thread,
        ),
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            return_value=original,
        ),
    ):

        def recover() -> dict:
            return _feedback_ancestor_publication(
                None,
                flow=flow,
                context=context,
                prior=failed,
                resume=context["trigger_event_data"]["_resume"],
            )

        if binding == "owned":
            assert recover() is original.result["trusted_publication"]
        else:
            with pytest.raises(PublicationError):
                recover()
