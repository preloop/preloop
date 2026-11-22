"""A stop sent with an API token is not labelled as a person's stop.

A CI job's cleanup step stops a review with an account API token. Recording
that as ``manual`` made the console say "stopped by user" for a stop no
person issued.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from preloop.models.crud import crud_flow_execution
from preloop.services import flow_execution_stop as fes
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


class _User:
    """A user row as the auth dependency returns it."""

    def __init__(self, api_key: Any = None) -> None:
        self.id = uuid.uuid4()
        if api_key is not None:
            self._auth_api_key = api_key


def test_console_session_stop_stays_manual() -> None:
    attribution = fes.api_token_stop_attribution(_User())
    assert attribution.source is None
    assert attribution.reason is None
    assert attribution.error_message == fes.MANUAL_STOP_MESSAGE


def test_api_token_stop_names_the_token() -> None:
    attribution = fes.api_token_stop_attribution(
        _User(SimpleNamespace(name="ci-reviewer"))
    )
    assert attribution.source == fes.STOP_SOURCE_API_TOKEN
    assert attribution.reason == 'Stopped through the API with token "ci-reviewer"'
    assert attribution.error_message == attribution.reason
    assert "by user" not in attribution.error_message


def test_api_token_without_a_name_is_still_attributed() -> None:
    attribution = fes.api_token_stop_attribution(_User(SimpleNamespace(name="")))
    assert attribution.source == fes.STOP_SOURCE_API_TOKEN
    assert attribution.reason == "Stopped through the API with an account token"


def _orchestrator() -> Any:
    return SimpleNamespace(
        db=object(),
        execution_log=SimpleNamespace(id=uuid.uuid4()),
        _end_read_transaction=lambda: None,
        _durable_stop_message=FlowExecutionOrchestrator._durable_stop_message,
    )


def test_signalled_stop_uses_the_recorded_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = 'Stopped through the API with token "ci-reviewer"'
    monkeypatch.setattr(
        crud_flow_execution,
        "get_stop_request",
        lambda db, execution_id: {"source": "api_token", "reason": reason},
    )
    message = FlowExecutionOrchestrator._signalled_stop_message(_orchestrator(), 191)
    assert message == reason


@pytest.mark.parametrize("stop_request", [None, {"source": "manual"}])
def test_signalled_stop_by_a_person_keeps_the_user_message(
    monkeypatch: pytest.MonkeyPatch, stop_request: Any
) -> None:
    monkeypatch.setattr(
        crud_flow_execution,
        "get_stop_request",
        lambda db, execution_id: stop_request,
    )
    message = FlowExecutionOrchestrator._signalled_stop_message(_orchestrator(), 191)
    assert message == "Execution stopped by user request after 191 seconds."


def test_signalled_stop_falls_back_when_the_row_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(db: Any, execution_id: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(crud_flow_execution, "get_stop_request", boom)
    message = FlowExecutionOrchestrator._signalled_stop_message(_orchestrator(), 5)
    assert message == "Execution stopped by user request after 5 seconds."
