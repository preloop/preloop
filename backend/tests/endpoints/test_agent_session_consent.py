"""Owner consent gates every Agent Control entry point before dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
import pytest

from preloop.api.endpoints import agent_control as endpoint
from preloop.schemas.agent_control import (
    AgentControlSendMessageRequest,
    AgentControlSessionActionRequest,
    AgentControlVoiceTranscriptRequest,
)


@pytest.fixture
def agent() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        owner_user_id=uuid4(),
        runtime_session_id=uuid4(),
        others_may_start_sessions="ask_me",
        lifecycle_state="active",
        agent_kind="openclaw",
        display_name="Example agent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask_me", "allow", "deny"])
async def test_owner_can_start_own_session(agent: SimpleNamespace, mode: str) -> None:
    agent.others_may_start_sessions = mode
    user = SimpleNamespace(id=agent.owner_user_id, account_id=agent.account_id)
    with patch.object(endpoint.crud_audit_log, "log_action") as audit:
        await endpoint._require_session_start_consent(
            MagicMock(),
            agent=agent,
            current_user=user,
            request=AgentControlSendMessageRequest(
                message="Example", start_new_session=True
            ),
        )
    assert audit.call_args.kwargs["details"]["decision"] == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,denied", [("allow", False), ("deny", True)])
async def test_other_member_modes(
    agent: SimpleNamespace, mode: str, denied: bool
) -> None:
    agent.others_may_start_sessions = mode
    user = SimpleNamespace(id=uuid4(), account_id=agent.account_id, is_superuser=True)
    with patch.object(endpoint.crud_audit_log, "log_action"):
        if denied:
            with pytest.raises(HTTPException) as exc:
                await endpoint._require_session_start_consent(
                    MagicMock(),
                    agent=agent,
                    current_user=user,
                    request=AgentControlSendMessageRequest(
                        message="Example", start_new_session=True
                    ),
                )
            assert exc.value.status_code == 403
        else:
            await endpoint._require_session_start_consent(
                MagicMock(),
                agent=agent,
                current_user=user,
                request=AgentControlSendMessageRequest(
                    message="Example", start_new_session=True
                ),
            )


@pytest.mark.asyncio
async def test_ask_me_requires_retry_intent(agent: SimpleNamespace) -> None:
    with pytest.raises(HTTPException) as exc:
        await endpoint._require_session_start_consent(
            MagicMock(),
            agent=agent,
            current_user=SimpleNamespace(id=uuid4(), account_id=agent.account_id),
            request=AgentControlSendMessageRequest(
                message="Example", start_new_session=True
            ),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_exact_intent_bound_to_requester_and_payload(
    agent: SimpleNamespace,
) -> None:
    user = SimpleNamespace(id=uuid4(), account_id=agent.account_id)
    request = AgentControlSendMessageRequest(
        message="Example", start_new_session=True, consent_request_id=uuid4()
    )
    with patch(
        "preloop.services.owner_consent.require_owner_consent", new_callable=AsyncMock
    ) as consent:
        keys = []
        for payload, caller in [
            (request, user),
            (request, user),
            (request.model_copy(update={"message": "Changed"}), user),
            (request, SimpleNamespace(id=uuid4(), account_id=agent.account_id)),
        ]:
            await endpoint._require_session_start_consent(
                MagicMock(), agent=agent, request=payload, current_user=caller
            )
            keys.append(consent.call_args.kwargs["request_key"])
    assert keys[0] == keys[1]
    assert len(set(keys)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [True, False])
async def test_existing_sessions_unaffected(
    agent: SimpleNamespace, targeted: bool
) -> None:
    agent.others_may_start_sessions = "deny"
    request = AgentControlSendMessageRequest(
        message="Example", target_session_id=uuid4() if targeted else None
    )
    with patch.object(
        endpoint.crud_runtime_session,
        "get_account_session",
        return_value=SimpleNamespace(ended_at=None),
    ):
        await endpoint._require_session_start_consent(
            MagicMock(),
            agent=agent,
            request=request,
            current_user=SimpleNamespace(id=uuid4(), account_id=agent.account_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn,ended", [(True, False), (False, True)])
async def test_implicit_start_cannot_bypass(
    agent: SimpleNamespace, spawn: bool, ended: bool
) -> None:
    agent.others_may_start_sessions = "deny"
    with (
        patch.object(
            endpoint.crud_runtime_session,
            "get_account_session",
            return_value=SimpleNamespace(ended_at=object() if ended else None),
        ),
        patch.object(endpoint.crud_audit_log, "log_action"),
    ):
        with pytest.raises(HTTPException) as exc:
            await endpoint._require_session_start_consent(
                MagicMock(),
                agent=agent,
                request=AgentControlSendMessageRequest(
                    message="Example", spawn_worktree=spawn
                ),
                current_user=SimpleNamespace(id=uuid4(), account_id=agent.account_id),
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["prompt", "takeover", "voice"])
async def test_pending_approval_never_dispatches(
    agent: SimpleNamespace, route: str
) -> None:
    user = SimpleNamespace(id=uuid4(), account_id=agent.account_id)
    request_id = uuid4()
    with (
        patch.object(
            endpoint.crud_managed_agent, "get_visible_target", return_value=agent
        ),
        patch.object(
            endpoint.crud_managed_agent, "get_for_account", return_value=agent
        ),
        patch.object(endpoint, "_agent_has_control_config", return_value=True),
        patch.object(
            endpoint,
            "_require_session_start_consent",
            new_callable=AsyncMock,
            side_effect=HTTPException(409, "pending"),
        ) as consent,
        patch.object(
            endpoint, "dispatch_operator_message", new_callable=AsyncMock
        ) as dispatch,
        patch.object(
            endpoint, "persist_and_deliver_command", new_callable=AsyncMock
        ) as persist,
    ):
        with pytest.raises(HTTPException):
            if route == "prompt":
                await endpoint._route_managed_agent_prompt(
                    agent_id=str(agent.id),
                    request=AgentControlSendMessageRequest(
                        message="Example",
                        start_new_session=True,
                        consent_request_id=request_id,
                    ),
                    current_user=user,
                    db=MagicMock(),
                )
            elif route == "takeover":
                await endpoint._route_session_action(
                    agent_id=str(agent.id),
                    name="request_takeover",
                    request=AgentControlSessionActionRequest(
                        start_new_session=True, consent_request_id=request_id
                    ),
                    current_user=user,
                    db=MagicMock(),
                )
            else:
                await endpoint.send_managed_agent_voice_transcript.__wrapped__(
                    agent_id=str(agent.id),
                    request=AgentControlVoiceTranscriptRequest(
                        transcript="Example",
                        start_new_session=True,
                        consent_request_id=request_id,
                    ),
                    current_user=user,
                    db=MagicMock(),
                )
        assert consent.call_args.kwargs["request"].consent_request_id == request_id
        dispatch.assert_not_called()
        persist.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask_me", "allow", "deny"])
async def test_only_owner_can_change_setting(agent: SimpleNamespace, mode: str) -> None:
    from preloop.api.endpoints import account as account_endpoint
    from preloop.schemas.gateway_usage import ManagedAgentUpdateRequest

    agent.lifecycle_reason = None
    agent.tags = {}
    user = SimpleNamespace(id=uuid4(), account_id=agent.account_id, is_superuser=True)
    with (
        patch.object(
            account_endpoint.crud_managed_agent, "get_for_account", return_value=agent
        ),
        patch.object(
            account_endpoint.crud_managed_agent, "update_operator_state"
        ) as update,
    ):
        with pytest.raises(HTTPException) as exc:
            await account_endpoint.update_account_managed_agent.__wrapped__(
                agent_id=str(agent.id),
                update=ManagedAgentUpdateRequest(others_may_start_sessions=mode),
                account=SimpleNamespace(id=agent.account_id),
                request=MagicMock(),
                current_user=user,
                db=MagicMock(),
            )
    assert exc.value.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_owner_cannot_clear_setting(agent: SimpleNamespace) -> None:
    from preloop.api.endpoints import account as account_endpoint
    from preloop.schemas.gateway_usage import ManagedAgentUpdateRequest

    agent.lifecycle_reason = None
    agent.tags = {}
    with patch.object(
        account_endpoint.crud_managed_agent, "get_for_account", return_value=agent
    ):
        with pytest.raises(HTTPException) as exc:
            await account_endpoint.update_account_managed_agent.__wrapped__(
                agent_id=str(agent.id),
                update=ManagedAgentUpdateRequest(others_may_start_sessions=None),
                account=SimpleNamespace(id=agent.account_id),
                request=MagicMock(),
                current_user=SimpleNamespace(
                    id=agent.owner_user_id,
                    account_id=agent.account_id,
                    is_superuser=True,
                ),
                db=MagicMock(),
            )
    assert exc.value.status_code == 422


@pytest.mark.parametrize("mode", ["ask_me", "allow", "deny"])
def test_owner_setting_round_trip(client, db_session, test_user, mode: str) -> None:
    created = client.post("/api/v1/agents", json={"display_name": "Example agent"})
    assert created.status_code == 201, created.text
    assert created.json()["others_may_start_sessions"] == "ask_me"
    agent_id = created.json()["id"]
    updated = client.patch(
        f"/api/v1/agents/{agent_id}", json={"others_may_start_sessions": mode}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["others_may_start_sessions"] == mode
    detail = client.get(f"/api/v1/agents/{agent_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["agent"]["others_may_start_sessions"] == mode
