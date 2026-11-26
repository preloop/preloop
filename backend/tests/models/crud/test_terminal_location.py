"""Terminal inventory joins use native identity and enrolled device provenance."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.models.schemas.terminal_location import HerdrInventory
from preloop.services.herdr_inventory import record_herdr_inventory
from preloop.services.harness_inventory import redact_harness_inventory


def fixture_report() -> dict[str, Any]:
    """Normalize the shared sanitized protocol-22 snapshot to the wire contract."""
    path = (
        Path(__file__).resolve().parents[4]
        / "cli/internal/herdr/testdata/protocol22.json"
    )
    snapshot = json.loads(path.read_text())["result"]["snapshot"]
    workspaces = {w["workspace_id"]: w["label"] for w in snapshot["workspaces"]}
    tabs = {t["tab_id"]: t["label"] for t in snapshot["tabs"]}
    panes = {p["pane_id"]: p.get("label", "") for p in snapshot["panes"]}
    return {
        "protocol": 22,
        "observe_enabled": True,
        "prompt_enabled": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "agents": [
            {
                "harness_kind": "codex_cli" if a["agent"] == "codex" else "claude_code",
                "harness_session_id": a["agent_session"]["value"],
                "cwd": a["cwd"],
                "multiplexer": "herdr",
                "workspace_id": a["workspace_id"],
                "workspace_label": workspaces[a["workspace_id"]],
                "tab_id": a["tab_id"],
                "tab_label": tabs[a["tab_id"]],
                "pane_id": a["pane_id"],
                "pane_label": panes[a["pane_id"]],
                "terminal_title": a["terminal_title_stripped"],
                "state": a["agent_status"],
            }
            for a in snapshot["agents"]
        ],
    }


@pytest.fixture
def terminal_data(db_session: Session, test_user: models.User) -> dict[str, Any]:
    """Twenty panes, including four intentionally unjoinable identities."""
    report = fixture_report()
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    runner = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Example",
        token_hash="example-token-hash",
        workstation_fingerprint="a" * 64,
    )
    db_session.add(runner)
    rows = []
    now = datetime.now(UTC).replace(tzinfo=None)
    for i, pane in enumerate(report["agents"]):
        # Cross-account, cross-device, cross-kind and cross-native-id must not join.
        account_id = other.id if i == 16 else test_user.account_id
        kind = "codex" if pane["harness_kind"] == "codex_cli" else "claude_code"
        if i == 18:
            kind = "opencode"
        principal = f"example-agent-{i}"
        native = "different-native" if i == 19 else pane["harness_session_id"]
        agent = models.ManagedAgent(
            account_id=account_id,
            agent_kind=kind,
            session_source_type=kind,
            session_source_id=principal,
            display_name=principal,
            lifecycle_updated_at=now,
            last_seen_at=now,
        )
        db_session.add(agent)
        db_session.flush()
        db_session.add(
            models.DiscoveredAgentCandidate(
                account_id=account_id,
                workstation_fingerprint=("b" if i == 17 else "a") * 64,
                agent_kind=kind,
                config_path_hash=f"{i:064x}",
                managed_agent_id=agent.id,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        row = models.RuntimeSession(
            account_id=account_id,
            session_source_type=kind,
            session_source_id=f"{principal}:{native}",
            runtime_principal_type=kind,
            runtime_principal_id=principal if i < 10 else f"{principal}:{native}",
            started_at=now,
            last_activity_at=now,
            turn_state="your_turn",
        )
        db_session.add(row)
        rows.append(row)
    db_session.flush()
    return {"report": report, "runner": runner, "rows": rows}


def test_twenty_agent_contract_and_exact_join(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    data = terminal_data
    assert len(HerdrInventory.model_validate(data["report"]).agents) == 20
    assert record_herdr_inventory(db_session, data["runner"], data["report"])
    agents = data["runner"].harness_inventory["herdr"]["agents"]
    assert [a["managed"] for a in agents] == [True] * 16 + [False] * 4
    assert all(row.terminal_location for row in data["rows"][:16])
    assert all(row.terminal_location is None for row in data["rows"][16:])
    assert db_session.query(models.RuntimeSession).count() == 20
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["runner"].account_id),
        workspace_id="workspace-example",
        limit=50,
    )
    assert result["total"] == 16
    assert all(item["terminal_location"]["pane_id"] for item in result["items"])
    assert (
        crud_runtime_session.list_account_sessions(
            db_session,
            account_id=str(data["runner"].account_id),
            workspace_id="missing",
        )["total"]
        == 0
    )
    assert "herdr" not in redact_harness_inventory(data["runner"].harness_inventory)


def test_closure_preserves_turn_state_and_stale_reports(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    data = terminal_data
    record_herdr_inventory(db_session, data["runner"], data["report"])
    data["rows"][0].turn_state = "working"
    data["rows"][1].turn_state = "waiting_for_approval"
    later = copy.deepcopy(data["report"])
    later.update(
        agents=[], observed_at=(datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    )
    record_herdr_inventory(db_session, data["runner"], later)
    result = crud_runtime_session.list_account_sessions(
        db_session, account_id=str(data["runner"].account_id), limit=50
    )
    summaries = {item["id"]: item for item in result["items"]}
    for index, row in enumerate(data["rows"][:16]):
        assert row.terminal_location["pane_closed_at"]
        assert row.ended_at is None
        assert summaries[str(row.id)]["is_active_now"] == (index < 2)
    assert not record_herdr_inventory(db_session, data["runner"], data["report"])
    assert data["rows"][2].terminal_location["pane_closed_at"]


def test_invalid_unknown_and_disabled_do_not_invent_closure(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    data = terminal_data
    record_herdr_inventory(db_session, data["runner"], data["report"])
    malformed = copy.deepcopy(data["report"])
    malformed["agents"][0]["state"] = "not-a-state"
    assert not record_herdr_inventory(db_session, data["runner"], malformed)
    unknown = dict(data["report"], protocol=99, agents=[])
    assert record_herdr_inventory(db_session, data["runner"], unknown)
    assert (
        data["runner"].harness_inventory["herdr"]["status"]
        == "herdr_protocol_unsupported"
    )
    assert all(not row.terminal_location["pane_closed_at"] for row in data["rows"][:16])
    disabled = dict(data["report"], protocol=0, observe_enabled=False, agents=[])
    assert record_herdr_inventory(db_session, data["runner"], disabled)
    assert all(row.terminal_location is None for row in data["rows"])
    assert not data["runner"].harness_inventory["herdr"]["observe_enabled"]


def test_unknown_harness_no_id_and_untrusted_labels() -> None:
    report = fixture_report()
    report["agents"][0].update(
        harness_kind="future_agent",
        harness_session_id="",
        state="unknown",
        pane_label="x\x1b\n" * 500,
        content="must not persist",
        output="must not persist",
        text="must not persist",
    )
    parsed = HerdrInventory.model_validate(report).model_dump(mode="json")
    assert len(parsed["agents"][0]["pane_label"]) == 256
    assert "\x1b" not in parsed["agents"][0]["pane_label"]
    assert not {"content", "output", "text"} & parsed["agents"][0].keys()


def test_ambiguous_panes_and_missing_agents_are_not_closures(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    data = terminal_data
    record_herdr_inventory(db_session, data["runner"], data["report"])
    report = copy.deepcopy(data["report"])
    duplicate = dict(report["agents"][0], pane_id="another-pane")
    report["agents"].append(duplicate)
    assert record_herdr_inventory(db_session, data["runner"], report)
    agents = data["runner"].harness_inventory["herdr"]["agents"]
    assert agents[0]["managed"] is False and agents[-1]["managed"] is False
    assert data["rows"][0].terminal_location["pane_closed_at"] is None
    malformed = copy.deepcopy(report)
    malformed["agents"][-1]["pane_id"] = malformed["agents"][0]["pane_id"]
    assert not record_herdr_inventory(db_session, data["runner"], malformed)
    report.pop("agents")
    assert not record_herdr_inventory(db_session, data["runner"], report)
    assert data["rows"][0].terminal_location["pane_closed_at"] is None


def test_pending_approvals_override_terminal_closure(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    from unittest.mock import MagicMock
    from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService

    data = terminal_data
    record_herdr_inventory(db_session, data["runner"], data["report"])
    record_herdr_inventory(db_session, data["runner"], dict(data["report"], agents=[]))
    row = crud_runtime_session.get_account_session_summary(
        db_session,
        account_id=str(data["runner"].account_id),
        runtime_session_id=str(data["rows"][0].id),
    )
    item = RuntimeSessionExplorerService._summary_row_to_schema(row)
    assert not item.is_active_now
    db = MagicMock()
    db.query.return_value.filter.return_value.group_by.return_value.all.return_value = [
        (item.id, 1)
    ]
    RuntimeSessionExplorerService(db)._attach_pending_approval_counts(
        account=data["runner"], items=[item], session_ids=[item.id]
    )
    assert item.is_active_now and item.pending_approval_count == 1


def test_cached_observation_joins_late_session_without_new_freshness(
    db_session: Session, terminal_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock, patch
    from preloop.services.herdr_inventory import reconcile_cached_herdr_inventory
    from preloop.services import account_realtime

    data = terminal_data
    early = data["rows"][0]
    identity = {
        key: getattr(early, key)
        for key in (
            "account_id",
            "session_source_type",
            "session_source_id",
            "runtime_principal_type",
            "runtime_principal_id",
            "started_at",
            "last_activity_at",
            "turn_state",
        )
    }
    db_session.delete(early)
    db_session.flush()
    emitter = MagicMock()
    monkeypatch.setattr(account_realtime, "emit_account_event", emitter)
    record_herdr_inventory(db_session, data["runner"], data["report"])
    assert not data["runner"].harness_inventory["herdr"]["agents"][0]["managed"]
    late = models.RuntimeSession(**identity)
    db_session.add(late)
    db_session.flush()
    emitter.reset_mock()
    assert reconcile_cached_herdr_inventory(db_session, data["runner"])
    assert datetime.fromisoformat(
        late.terminal_location["observed_at"]
    ) == datetime.fromisoformat(data["report"]["observed_at"])
    assert late.last_activity_at == identity["last_activity_at"]
    assert late.terminal_location["pane_closed_at"] is None
    assert data["rows"][1].terminal_location["pane_closed_at"] is None
    event = emitter.call_args.args[0]
    assert event["account_id"] == str(data["runner"].account_id)
    assert event["runtime_session_id"] == str(late.id)
    assert event["type"] == "runtime_session_updated"
    assert event["payload"] == {
        "runtime_session_id": str(late.id),
        "terminal_location_changed": True,
    }
    emitter.reset_mock()
    with patch.object(db_session, "commit", wraps=db_session.commit) as commit:
        assert not reconcile_cached_herdr_inventory(db_session, data["runner"])
        commit.assert_not_called()
    emitter.assert_not_called()


def test_location_events_only_for_changed_sessions_after_commit(
    db_session: Session, terminal_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import account_realtime

    data = terminal_data
    events: list[dict[str, Any]] = []

    from sqlalchemy import event as sa_event

    commits: list[bool] = []
    emission_commit_counts: list[int] = []
    sa_event.listen(db_session, "after_commit", lambda db: commits.append(True))

    def receive(event: dict[str, Any]) -> None:
        emission_commit_counts.append(len(commits))
        events.append(event)

    monkeypatch.setattr(account_realtime, "emit_account_event", receive)
    record_herdr_inventory(db_session, data["runner"], data["report"])
    expected = {str(row.id) for row in data["rows"][:16]}
    assert {event["runtime_session_id"] for event in events} == expected
    assert emission_commit_counts == [1] * 16
    assert all(
        event["account_id"] == str(data["runner"].account_id) for event in events
    )
    events.clear()
    record_herdr_inventory(db_session, data["runner"], data["report"])
    record_herdr_inventory(db_session, data["runner"], {"protocol": 22})
    assert not events
    record_herdr_inventory(db_session, data["runner"], dict(data["report"], agents=[]))
    assert {event["runtime_session_id"] for event in events} == expected
    events.clear()
    record_herdr_inventory(
        db_session,
        data["runner"],
        dict(data["report"], observe_enabled=False, protocol=0, agents=[]),
    )
    assert {event["runtime_session_id"] for event in events} == expected


def test_cached_observation_never_selects_between_sessions_with_same_native_id(
    db_session: Session, terminal_data: dict[str, Any]
) -> None:
    from preloop.services.herdr_inventory import reconcile_cached_herdr_inventory

    data = terminal_data
    original = data["rows"][0]
    duplicate = models.RuntimeSession(
        account_id=original.account_id,
        session_source_type=original.session_source_type,
        session_source_id="another-runtime-generation",
        session_reference=data["report"]["agents"][0]["harness_session_id"],
        runtime_principal_type=original.runtime_principal_type,
        runtime_principal_id=original.runtime_principal_id,
        started_at=original.started_at + timedelta(seconds=1),
    )
    db_session.add(duplicate)
    db_session.flush()
    record_herdr_inventory(db_session, data["runner"], data["report"])
    assert not reconcile_cached_herdr_inventory(db_session, data["runner"])
    assert original.terminal_location is duplicate.terminal_location is None
    assert not data["runner"].harness_inventory["herdr"]["agents"][0]["managed"]
