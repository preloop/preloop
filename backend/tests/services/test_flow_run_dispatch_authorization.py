"""H4 flow:run and runner:accept stay on the worker dispatch path."""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.plugins.account_hooks import ACTION_FLOW_RUN, ACTION_RUNNER_ACCEPT
from preloop.services import flow_execution_dispatcher as dispatcher
from preloop.services.runner_service import _runner_may_accept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch", [dispatcher.dispatch_execute, dispatcher.dispatch_resume]
)
async def test_dispatch_refuses_a_denied_flow(
    monkeypatch: pytest.MonkeyPatch,
    dispatch: Callable[..., Awaitable[bool]],
) -> None:
    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.account_id = uuid.uuid4()
    execution = MagicMock(flow=flow)
    seen: dict[str, object] = {}

    def authorize(ctx, action, resource):
        seen["action"] = action
        seen["flow_id"] = ctx.attributes["flow_id"]
        seen["resource"] = resource
        return MagicMock(allowed=False, reason="denied")

    monkeypatch.setattr(
        "preloop.plugins.account_hooks.get_authorizer", lambda: object()
    )
    monkeypatch.setattr("preloop.plugins.account_hooks.authorize", authorize)
    monkeypatch.setattr("sqlalchemy.orm.Session", lambda engine: MagicMock())
    monkeypatch.setattr("preloop.models.db.session.get_engine", lambda: MagicMock())
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        lambda *args, **kwargs: execution,
    )
    publish = AsyncMock()
    monkeypatch.setattr(dispatcher, "_dispatch", publish)

    with pytest.raises(PermissionError, match="denied"):
        await dispatch(uuid.uuid4())

    publish.assert_not_called()
    assert seen["action"] == ACTION_FLOW_RUN
    assert seen["flow_id"] == str(flow.id)
    assert seen["resource"] is flow


def test_runner_accept_uses_the_execution_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_id = uuid.uuid4()
    seen: dict[str, object] = {}

    def authorize(ctx, action, runner):
        seen["action"] = action
        seen["flow_id"] = ctx.attributes["flow_id"]
        return MagicMock(allowed=True)

    monkeypatch.setattr(
        "preloop.plugins.account_hooks.get_authorizer", lambda: object()
    )
    monkeypatch.setattr("preloop.plugins.account_hooks.authorize", authorize)

    allowed = _runner_may_accept(
        MagicMock(),
        account_id=uuid.uuid4(),
        runner=MagicMock(),
        pool="default",
        execution_id=uuid.uuid4(),
        flow_id=str(flow_id),
    )

    assert allowed is True
    assert seen["action"] == ACTION_RUNNER_ACCEPT
    assert seen["flow_id"] == str(flow_id)
