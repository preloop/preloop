"""Execution attribution for host-profile gateway traffic.

A private-runner Codex or Claude Code host CLI onboarded through Preloop
calls the gateway with the agent's own key and, per run, the signed
``X-Preloop-Execution`` marker. These tests pin that:

* usage rows of a marked request carry the flow execution (and the agent key);
* the flow's per-execution ceilings stop a marked run;
* a request without the marker still attributes to the agent only;
* a forged, foreign or finished marker is refused, never ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest

from preloop.config import settings
from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.host_exec_gateway import (
    EXECUTION_MARKER_HEADER,
    ExecutionMarkerError,
    decode_execution_marker,
    mint_execution_marker,
    resolve_execution_marker,
    with_execution_context,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

_COMPLETION = {
    "id": "chatcmpl_marker",
    "created": 1710000000,
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _model(db_session, account_id):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Marker Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "unused-synthetic-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                    "responses_api": "transcode",
                }
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _flow(db_session, test_user, *, limits=None):
    agent_config = {"host_exec_profile": "codex-review"}
    if limits is not None:
        agent_config["limits"] = limits
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Host Review",
            prompt_template="Review",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
            agent_type="codex",
            agent_config=agent_config,
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )


def _execution(db_session, flow, status="RUNNING"):
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status=status)
    )
    db_session.flush()
    return execution


def _agent_context(db_session, test_user):
    """A detached agent-key context, as HTTP authentication returns it."""
    key, _token = crud_api_key.create_runtime_key(
        db_session,
        name="Codex agent key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    return ModelGatewayAuthContext(token="t", user=test_user, api_key=key).snapshot()


@pytest.fixture
def agent_auth(app, db_session, test_user, monkeypatch):
    """Serve gateway requests as the agent key, keeping marker handling real."""
    context = _agent_context(db_session, test_user)

    async def authenticate(*_args, **_kwargs):
        return context

    monkeypatch.setattr(
        "preloop.api.gateway_auth_dependency.authenticate_request", authenticate
    )
    return context


def _post(client, headers=None):
    return client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": "Bearer agent", **(headers or {})},
        json={"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
    )


def _rows(db_session, test_user):
    db_session.expire_all()
    return (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == test_user.account_id,
            ApiUsage.action_type == "model_gateway",
        )
        .all()
    )


def test_marked_request_is_attributed_to_execution_and_agent(
    agent_auth, client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    flow = _flow(db_session, test_user)
    execution = _execution(db_session, flow)
    db_session.commit()
    marker = mint_execution_marker(
        account_id=test_user.account_id, flow_id=flow.id, execution_id=execution.id
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion", return_value=_COMPLETION
    ):
        response = _post(client, {EXECUTION_MARKER_HEADER: marker})

    assert response.status_code == 200, response.text
    rows = _rows(db_session, test_user)
    assert len(rows) == 1
    assert str(rows[0].flow_execution_id) == str(execution.id)
    assert str(rows[0].flow_id) == str(flow.id)
    assert str(rows[0].api_key_id) == str(agent_auth.api_key.id)


def test_unmarked_request_still_attributes_to_the_agent(
    agent_auth, client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    db_session.commit()

    with patch(
        "preloop.services.openai_gateway.litellm.completion", return_value=_COMPLETION
    ):
        response = _post(client)

    assert response.status_code == 200, response.text
    rows = _rows(db_session, test_user)
    assert len(rows) == 1
    assert rows[0].flow_execution_id is None
    assert str(rows[0].api_key_id) == str(agent_auth.api_key.id)


def test_marked_request_over_the_ceiling_stops_the_execution(
    agent_auth, client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    flow = _flow(db_session, test_user, limits={"max_total_tokens": 10})
    execution = _execution(db_session, flow)
    marker = mint_execution_marker(
        account_id=test_user.account_id, flow_id=flow.id, execution_id=execution.id
    )
    from preloop.models.crud import crud_api_usage

    # Earlier marked traffic of this run already spent the token ceiling.
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=50,
        completion_tokens=0,
        total_tokens=50,
        estimated_cost=None,
        cost_source="unpriced",
    )
    db_session.commit()

    with patch("preloop.services.openai_gateway.litellm.completion") as provider:
        second = _post(client, {EXECUTION_MARKER_HEADER: marker})

    assert second.status_code == 429, second.text
    assert second.json()["error"]["preloop_code"] == "execution_budget_exceeded"
    provider.assert_not_called()
    db_session.expire_all()
    stored = crud_flow_execution.get(db_session, id=execution.id)
    assert stored.status == "FAILED"
    assert stored.failure_category == "budget_exceeded"


def test_forged_marker_is_refused(agent_auth, client, db_session, test_user):
    _model(db_session, test_user.account_id)
    flow = _flow(db_session, test_user)
    execution = _execution(db_session, flow)
    db_session.commit()
    forged = jwt.encode(
        {
            "aud": "preloop:host-exec-gateway",
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "account_id": str(test_user.account_id),
            "flow_id": str(flow.id),
            "execution_id": str(execution.id),
        },
        "not-the-server-secret",
        algorithm="HS256",
    )

    with patch("preloop.services.openai_gateway.litellm.completion") as provider:
        response = _post(client, {EXECUTION_MARKER_HEADER: forged})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "execution_marker_invalid"
    provider.assert_not_called()


def test_marker_for_a_finished_execution_is_refused(db_session, test_user):
    flow = _flow(db_session, test_user)
    execution = _execution(db_session, flow, status="SUCCEEDED")
    marker = mint_execution_marker(
        account_id=test_user.account_id, flow_id=flow.id, execution_id=execution.id
    )
    with pytest.raises(ExecutionMarkerError, match="finished"):
        resolve_execution_marker(
            db_session,
            account_id=test_user.account_id,
            api_key_context={},
            marker=marker,
        )


def test_marker_for_another_account_or_bound_key_is_refused(db_session, test_user):
    import uuid

    flow = _flow(db_session, test_user)
    execution = _execution(db_session, flow)
    marker = mint_execution_marker(
        account_id=test_user.account_id, flow_id=flow.id, execution_id=execution.id
    )
    with pytest.raises(ExecutionMarkerError, match="does not match"):
        resolve_execution_marker(
            db_session, account_id=uuid.uuid4(), api_key_context={}, marker=marker
        )
    with pytest.raises(ExecutionMarkerError, match="another execution"):
        resolve_execution_marker(
            db_session,
            account_id=test_user.account_id,
            api_key_context={"flow_execution_id": str(uuid.uuid4())},
            marker=marker,
        )


def test_marker_round_trip_and_expiry():
    import uuid

    ids = {"account_id": uuid.uuid4(), "flow_id": uuid.uuid4()}
    execution_id = uuid.uuid4()
    marker = mint_execution_marker(execution_id=execution_id, **ids)
    assert decode_execution_marker(marker)["execution_id"] == execution_id
    expired = jwt.encode(
        {
            "aud": "preloop:host-exec-gateway",
            "exp": datetime.now(UTC) - timedelta(seconds=1),
            "account_id": str(ids["account_id"]),
            "flow_id": str(ids["flow_id"]),
            "execution_id": str(execution_id),
        },
        settings.security.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(ExecutionMarkerError):
        decode_execution_marker(expired)


def test_execution_context_is_added_to_a_copy(db_session, test_user):
    context = _agent_context(db_session, test_user)
    marked = with_execution_context(
        context, {"flow_execution_id": "e", "flow_id": "f", "execution_marker": "x"}
    )
    assert marked.api_key.context_data["flow_execution_id"] == "e"
    assert "flow_execution_id" not in context.api_key.context_data
