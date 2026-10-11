"""GET /api/v1/flows/harness-options and the runner_id pin check (#1481)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from preloop.models.crud.flow_runner import crud_flow_runner

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "personal_runners"
    / "harness_inventory_copilot_signed_in.json"
)


def _runner(account_id, registered_by, name, *, online=True):
    return SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        name=name,
        labels=[],
        registered_by_user_id=registered_by,
        status="online" if online else "offline",
        last_heartbeat=datetime.now(timezone.utc) if online else None,
        harness_inventory=json.loads(FIXTURE.read_text()),
    )


def test_harness_options_lists_the_callers_runners(
    client, test_user, monkeypatch
) -> None:
    mine_online = _runner(test_user.account_id, test_user.id, "laptop")
    mine_offline = _runner(test_user.account_id, test_user.id, "desktop", online=False)
    colleague = _runner(test_user.account_id, uuid4(), "colleague")
    monkeypatch.setattr(
        crud_flow_runner,
        "list_for_account",
        lambda db, **kw: [mine_online, mine_offline, colleague],
    )
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: False
    )
    response = client.get("/api/v1/flows/harness-options")
    assert response.status_code == 200, response.text
    (copilot,) = response.json()["harnesses"]
    assert copilot["harness"] == "copilot_cli"
    assert copilot["agent_type"] == "copilot"
    assert copilot["billing"] == "seat"
    assert (copilot["runners_online"], copilot["runners_total"]) == (1, 2)
    assert [r["name"] for r in copilot["runners"]] == ["laptop", "desktop"]

    # An account admin may route to every account runner.
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: True
    )
    (copilot,) = client.get("/api/v1/flows/harness-options").json()["harnesses"]
    assert (copilot["runners_online"], copilot["runners_total"]) == (2, 3)


def test_runner_pin_must_be_a_runner_the_editor_may_use(
    client, db_session, test_user, monkeypatch
) -> None:
    foreign = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "someone-elses-laptop",
            "token_hash": uuid4().hex,
            "capabilities": {},
            "registered_by_user_id": None,
        },
    )
    monkeypatch.setattr(
        "preloop.api.auth.router._is_account_admin", lambda db, user: False
    )
    body = {
        "name": f"seat-review-{uuid4().hex[:6]}",
        "prompt_template": "review",
        "agent_type": "copilot",
        "trigger_event_source": "webhook",
        "agent_config": {"harness": "copilot_cli", "runner_id": str(foreign.id)},
    }
    response = client.post("/api/v1/flows", json=body)
    assert response.status_code == 400, response.text
    assert "runner you may use" in response.json()["detail"]

    body["agent_config"] = {"harness": "copilot_cli", "runner_id": str(uuid4())}
    assert client.post("/api/v1/flows", json=body).status_code == 400

    body["agent_config"] = {"harness": "copilot_cli"}
    response = client.post("/api/v1/flows", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["agent_config"]["harness"] == "copilot_cli"
