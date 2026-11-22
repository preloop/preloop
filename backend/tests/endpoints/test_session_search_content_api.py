"""Content search finds a session by what was said in it, not by its title.

Each test writes through the real indexing writers (the gateway call and the
console control message), then asks the search endpoint for a word that only
the session content holds. Writing chunks directly would pass even when the
writers never index the turn that holds the word.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from preloop.config import settings
from preloop.models.crud import (
    crud_api_usage,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.services.session_search_index import index_gateway_interaction

SEARCH_URL = "/api/v1/runtime-sessions/search"
STARTED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _session(db_session: Any, account_id: Any, source_id: str) -> Any:
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=STARTED_AT,
        last_activity_at=STARTED_AT,
    )


def _search(client: Any, query: str) -> list[str]:
    response = client.post(SEARCH_URL, json={"query": query})
    assert response.status_code == 200, response.text
    return [item["runtime_session_id"] for item in response.json()["results"]]


def test_late_turn_and_reply_of_a_long_conversation_are_found(
    client: Any, db_session: Any, test_user: Any
) -> None:
    """The newest turn and the reply survive the per call text ceiling.

    A long session resends its whole history on every call. Indexed in
    request order, the ceiling was spent on the opening turns, so the turn a
    call added and the model's reply were never searchable.
    """
    session = _session(db_session, test_user.account_id, "long-chat")
    filler = "background context for the release " * 40
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": filler}
        for index in range(60)
    ]
    messages.append({"role": "user", "content": "check the quokkaflux migration"})
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"endpoint_kind": "chat_completions"},
    )

    with patch.object(settings, "model_gateway_capture_content", True):
        index_gateway_interaction(
            db_session,
            usage=usage,
            request_payload={"model": "gpt-5", "messages": messages},
            response_payload={
                "choices": [{"message": {"content": "the zephyrine index is rebuilt"}}]
            },
        )
    db_session.flush()

    assert _search(client, "quokkaflux") == [str(session.id)]
    assert _search(client, "zephyrine") == [str(session.id)]


def test_console_message_to_an_agent_and_its_reply_are_found(
    client: Any, db_session: Any, test_user: Any
) -> None:
    """Operator messages and agent replies are indexed when they are stored."""
    session = _session(db_session, test_user.account_id, "console-chat")

    crud_runtime_session_activity.log_agent_control_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        message="please rotate the marmalith credentials",
        status="queued",
        metadata={"command_id": "cmd-content-1"},
    )
    crud_runtime_session_activity.log_agent_control_result(
        db_session,
        account_id=test_user.account_id,
        command_id="cmd-content-1",
        fallback_runtime_session_id=session.id,
        status="succeeded",
        message="rotated, the vexillon job is green",
    )

    assert _search(client, "marmalith") == [str(session.id)]
    assert _search(client, "vexillon") == [str(session.id)]
    # The title alone is not what matched.
    assert _search(client, "nonexistentword") == []
