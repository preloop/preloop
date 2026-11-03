"""The execution transcript reads its model calls a page at a time (#1208).

Every model call stores the conversation it sent, so on a long run call N
repeats calls 1..N-1 in ``conversation_preview`` and again in the sanitized
``request`` body. The transcript read used to fetch every call with its full
payload in one response; on a large review run that was tens of megabytes of
the same history, and the browser renderer ran out of memory building the
conversation from it. These tests pin the bounded contract: a page of the
newest calls, replayed history elided, a keyset cursor for older pages and the
total so the console can say "showing N of M".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution_log import FlowExecutionLog
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

# One message of a synthetic review run: a tool result carrying a slice of a
# diff. 4 KB each is in line with what a code review agent reads per call.
MESSAGE_TEXT = "x" * 4000


def _execution(db_session, test_user):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Large review",
            prompt_template="Review",
            trigger_event_source="github",
            trigger_event_types=["pull_request"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    db_session.flush()
    return execution


def _conversation(turns: int) -> list[dict]:
    messages: list[dict] = [{"role": "user", "text": "Review this pull request"}]
    for turn in range(turns):
        messages.append({"role": "assistant", "text": f"reading file {turn}"})
        messages.append({"role": "tool", "text": f"{turn}:{MESSAGE_TEXT}"})
    return messages


def _call_metadata(index: int) -> dict:
    messages = _conversation(index + 1)
    return {
        "api_usage_id": f"usage-{index}",
        "model_alias": "openai/gpt-5",
        "total_tokens": 1000 * (index + 1),
        "conversation_preview": {"messages": messages},
        "request": {
            "messages": [
                (
                    {"role": "tool", "content": m["text"], "tool_call_id": "c"}
                    if m["role"] == "tool"
                    else {"role": m["role"], "content": m["text"]}
                )
                for m in messages
            ]
        },
        "response": {"choices": [{"message": {"content": MESSAGE_TEXT}}]},
        "stream_events": [{"delta": MESSAGE_TEXT}],
    }


def _seed(db_session, execution, calls: int, *, same_timestamp: bool = False):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(calls):
        at = start if same_timestamp else start + timedelta(seconds=index)
        db_session.add(
            FlowExecutionLog(
                execution_id=execution.id,
                timestamp=at,
                log_type="model_gateway_call",
                metadata_=_call_metadata(index),
            )
        )
        # Agent log lines between the calls must not count as calls.
        db_session.add(
            FlowExecutionLog(
                execution_id=execution.id,
                timestamp=at + timedelta(milliseconds=500),
                log_type="agent_log_line",
                message=f"line {index}",
                metadata_={"line": f"line {index}"},
            )
        )
    db_session.flush()


def _url(execution, **params) -> str:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/api/v1/flows/executions/{execution.id}/gateway-events?{query}"


def _page(client, execution, **params) -> tuple[dict, int]:
    response = client.get(_url(execution, **params))
    assert response.status_code == 200, response.text
    return response.json(), len(response.content)


def test_delta_page_of_a_large_run_stays_small(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=60)

    # The old transcript read: every call with its whole payload.
    _, full_bytes = _page(client, execution, tail=5000)
    page, page_bytes = _page(
        client,
        execution,
        tail=25,
        model_calls_only="true",
        history="delta",
        history_window=40,
    )

    # 60 calls replaying up to 60 tool results of 4 KB each, twice per call:
    # about 15 MB, against about 0.2 MB for the page.
    assert full_bytes > 10_000_000
    # The bounded page carries each message once, within its window.
    assert page_bytes < 1_000_000
    # Newest first, like every read of this endpoint.
    assert page["logs"][0]["payload"]["api_usage_id"] == "usage-59"
    assert page["logs"][-1]["payload"]["api_usage_id"] == "usage-35"
    assert len(page["logs"]) == 25
    assert page["has_more"] is True
    assert page["pagination"]["total"] == 60
    assert page["pagination"]["has_more"] is True
    assert page["pagination"]["next_cursor"] == page["logs"][-1]["id"]


def test_delta_page_keeps_what_the_transcript_reads(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=3)

    page, _ = _page(
        client, execution, model_calls_only="true", history="delta", tail=25
    )
    newest = page["logs"][0]["payload"]

    # Messages an older call on the page carried are left out; positions stay.
    positions = [m["position"] for m in newest["conversation_preview"]["messages"]]
    assert positions[-2:] == [5, 6]
    assert 1 not in positions
    # The request keeps only the tool results, the part the transcript uses
    # to tell tool output from user prompts, and only the new ones.
    request_messages = newest["request"]["messages"]
    assert request_messages
    assert {m["role"] for m in request_messages} == {"tool"}
    assert all(m["content"].startswith("2:") for m in request_messages)
    # Bodies the transcript never reads are not sent.
    assert "response" not in newest
    assert "stream_events" not in newest
    assert newest["api_usage_id"] == "usage-2"


def test_cursor_pages_cover_every_call_once(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    # Same timestamp on every call: the cursor must still not skip or repeat.
    _seed(db_session, execution, calls=7, same_timestamp=True)

    seen: list[str] = []
    params = {"tail": 3, "model_calls_only": "true", "history": "delta"}
    page, _ = _page(client, execution, **params)
    seen = [event["id"] for event in page["logs"]] + seen
    while page["pagination"]["has_more"]:
        page, _ = _page(
            client, execution, before=page["pagination"]["next_cursor"], **params
        )
        seen = [event["id"] for event in page["logs"]] + seen

    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert page["pagination"]["next_cursor"] is None
    assert page["has_more"] is False


def test_full_payload_pages_are_capped(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=3)

    response = client.get(_url(execution, history="delta", tail=5000))

    assert response.status_code == 200
    assert response.json()["pagination"]["limit"] == 100


def test_cursor_from_another_execution_is_not_found(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=2)

    response = client.get(_url(execution, before=uuid.uuid4(), history="delta"))

    assert response.status_code == 404


def test_metadata_read_is_unchanged(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=2)

    page, _ = _page(client, execution, metadata_only="true", model_calls_only="true")

    assert "conversation_preview" not in page["logs"][0]["payload"]
    assert "request" not in page["logs"][0]["payload"]
    assert json.dumps(page["logs"])
