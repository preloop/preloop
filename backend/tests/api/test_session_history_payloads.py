"""Session payload size on the Sessions page (#1586).

The activity timeline leaves a model call's replayed history out, and the
gateway-events page with ``history=delta`` carries each message once.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from preloop.models import models
from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity

BASE = "/api/v1/runtime-sessions"
STARTED = datetime.now(timezone.utc) - timedelta(hours=1)


def _session(db_session, account_id):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=f"history-{uuid.uuid4()}",
        session_reference="history",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=STARTED,
        last_activity_at=STARTED,
    )


def _gateway_call(db_session, account_id, session_id, index, history):
    db_session.add(
        models.RuntimeSessionActivity(
            id=uuid.uuid4(),
            account_id=account_id,
            runtime_session_id=session_id,
            activity_type="model_gateway_call",
            status="success",
            timestamp=STARTED + timedelta(minutes=index),
            metadata_={
                "model_alias": "claude",
                "total_tokens": 100 + index,
                "conversation_preview": {
                    "messages": [
                        *history,
                        {
                            "role": "assistant",
                            "source": "response",
                            "text": f"answer {index}",
                        },
                    ],
                    "metadata": {},
                },
                "tools": [
                    {"kind": "call", "call_id": f"c{index}", "name": "ls", "text": "{}"}
                ],
                "request": {"messages": history},
                "response": {"id": f"r{index}"},
            },
        )
    )


def _seed(db_session, test_user, calls=3):
    session = _session(db_session, test_user.account_id)
    history = [{"role": "user", "source": "request", "text": "x" * 2000}]
    for index in range(calls):
        _gateway_call(db_session, test_user.account_id, session.id, index, history)
        history = [
            *history,
            {"role": "assistant", "source": "request", "text": f"answer {index}"},
        ]
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        server_name="filesystem",
        tool_name="read_file",
        status="success",
        summary="read",
        metadata={"arguments": {"path": "a.txt"}},
        timestamp=STARTED + timedelta(minutes=10),
        commit=False,
    )
    db_session.commit()
    return session


def test_timeline_leaves_model_call_history_out(client, db_session, test_user):
    session = _seed(db_session, test_user)

    response = client.get(f"{BASE}/{session.id}/activity")

    assert response.status_code == 200
    items = response.json()["items"]
    calls = [i for i in items if i["activity_type"] == "model_gateway_call"]
    assert len(calls) == 3
    for item in calls:
        for key in ("conversation_preview", "tools", "request", "response"):
            assert key not in item["metadata"]
        assert item["metadata"]["model_alias"] == "claude"
    assert {item["total_tokens"] for item in calls} == {100, 101, 102}
    tool = next(i for i in items if i["activity_type"] == "tool_call")
    assert tool["tool_name"] == "read_file"
    assert tool["metadata"]["arguments"] == {"path": "a.txt"}


def test_gateway_events_delta_carries_each_message_once(client, db_session, test_user):
    session = _seed(db_session, test_user)

    full = client.get(f"{BASE}/{session.id}/gateway-events?limit=25")
    delta = client.get(f"{BASE}/{session.id}/gateway-events?limit=25&history=delta")

    assert full.status_code == delta.status_code == 200
    assert len(delta.content) < len(full.content)
    full_logs, delta_logs = full.json()["logs"], delta.json()["logs"]
    assert [e["id"] for e in delta_logs] == [e["id"] for e in full_logs]
    assert delta.json()["pagination"] == full.json()["pagination"]
    newest = delta_logs[0]["payload"]["conversation_preview"]["messages"]
    # The 2,000-character prompt is kept once per event (it is the request
    # the event is titled by); the replayed answers are gone.
    assert [(m["position"], m["text"][:8]) for m in newest] == [
        (0, "x" * 8),
        (3, "answer 2"),
    ]
    oldest = delta_logs[-1]["payload"]["conversation_preview"]["messages"]
    assert [m["position"] for m in oldest] == [0, 1]
    # Without the flag nothing changes.
    assert (
        "position"
        not in (full_logs[0]["payload"]["conversation_preview"]["messages"][0])
    )


def test_gateway_events_history_window_bounds_a_long_session_page(
    client, db_session, test_user
):
    """#1689: the first page of a long session stays small."""
    session = _session(db_session, test_user.account_id)
    history = [{"role": "user", "source": "request", "text": "Fix the build"}]
    for index in range(60):
        _gateway_call(db_session, test_user.account_id, session.id, index, history)
        history = [
            *history,
            {"role": "assistant", "source": "request", "text": f"{index} " * 300},
        ]
    db_session.commit()
    url = f"{BASE}/{session.id}/gateway-events?limit=20&history=delta"

    unbounded = client.get(url)
    windowed = client.get(f"{url}&history_window=10")

    assert unbounded.status_code == windowed.status_code == 200
    assert len(windowed.content) < len(unbounded.content)
    whole = unbounded.json()["logs"][-1]["payload"]["conversation_preview"]
    assert len(json.dumps(whole["messages"])) > 20_000
    logs = windowed.json()["logs"]
    assert len(logs) == 20
    oldest = logs[-1]["payload"]
    assert len(oldest["conversation_preview"]["messages"]) <= 11
    assert len(json.dumps(oldest["conversation_preview"]["messages"])) < 12_000
    metadata = oldest["conversation_preview"]["metadata"]
    assert metadata["history_window_start"] > 0
    assert windowed.json()["pagination"]["has_more"] is True
