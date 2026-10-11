"""Contract tests for personal runner wire shapes (#1480, contracts A and C).

The same fixtures live under ``cli/internal/cmd/testdata/personal_runners``
and are decoded by the Go runner tests, so both sides agree on the shapes and
on the inventory hash.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from preloop.models.schemas.flow_runner import (
    HarnessInventory,
    HarnessInventoryEntry,
    RunnerRegisterRequest,
    harness_inventory_hash,
)
from preloop.models.schemas.runner_session import (
    SESSION_MESSAGE_TYPES,
    SessionEventMessage,
    SessionStateMessage,
    is_valid_end_reason,
    parse_runner_session_message,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "personal_runners"
GO_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "cli"
    / "internal"
    / "cmd"
    / "testdata"
    / "personal_runners"
)
SESSION_FIXTURES = sorted(p.name for p in FIXTURES.glob("session_*.json"))


def _load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_go_and_python_fixture_copies_are_identical() -> None:
    if not GO_FIXTURES.is_dir():
        pytest.skip("CLI tree not present")
    ours = {p.name: p.read_bytes() for p in FIXTURES.glob("*.json")}
    theirs = {p.name: p.read_bytes() for p in GO_FIXTURES.glob("*.json")}
    assert ours == theirs


def test_inventory_fixture_round_trips_and_hash_matches() -> None:
    raw = _load("harness_inventory_copilot_signed_in.json")
    inventory = HarnessInventory.model_validate(raw)
    assert inventory.to_wire() == raw
    assert harness_inventory_hash(inventory.entries) == raw["hash"]
    copilot = inventory.entries[0]
    assert copilot.harness == "copilot_cli"
    assert copilot.login_state == "signed_in"
    assert copilot.generated_profile == "copilot"


def test_hash_ignores_key_order_and_changes_with_content() -> None:
    raw = _load("harness_inventory_copilot_signed_in.json")["entries"]
    reordered = [dict(reversed(list(entry.items()))) for entry in raw]
    entries = [HarnessInventoryEntry.model_validate(e) for e in raw]
    assert harness_inventory_hash(entries) == harness_inventory_hash(
        [HarnessInventoryEntry.model_validate(e) for e in reordered]
    )
    entries[0].login_state = "signed_out"
    assert (
        harness_inventory_hash(entries)
        != _load("harness_inventory_copilot_signed_in.json")["hash"]
    )


def test_register_without_inventory_still_validates() -> None:
    request = RunnerRegisterRequest.model_validate(_load("register_956_era.json"))
    assert request.harness_inventory is None
    assert request.host_exec_profiles[0].name == "copilot"


def test_register_accepts_null_inventory() -> None:
    body = _load("register_956_era.json") | {"harness_inventory": None}
    assert RunnerRegisterRequest.model_validate(body).harness_inventory is None


def test_register_with_inventory() -> None:
    request = RunnerRegisterRequest.model_validate(
        _load("register_with_inventory.json")
    )
    assert request.harness_inventory is not None
    assert [e.harness for e in request.harness_inventory.entries] == [
        "copilot_cli",
        "claude_desktop",
    ]


def test_heartbeat_fixtures_carry_matching_hash() -> None:
    full = _load("heartbeat_with_inventory.json")
    hash_only = _load("heartbeat_hash_only.json")
    inventory = HarnessInventory.model_validate(full["harness_inventory"])
    assert full["harness_inventory_hash"] == inventory.hash
    assert hash_only["harness_inventory_hash"] == inventory.hash
    assert "harness_inventory" not in hash_only
    assert _load("ack_inventory_wanted.json") == {
        "type": "ack",
        "inventory_wanted": True,
    }


def test_unknown_harness_from_newer_runner_is_dropped_not_rejected() -> None:
    raw = _load("harness_inventory_copilot_signed_in.json")
    raw["entries"].append(raw["entries"][1] | {"harness": "future_cli"})
    inventory = HarnessInventory.model_validate(raw)
    assert len(inventory.entries) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda inv: inv["entries"].extend([inv["entries"][1]] * 31),
        lambda inv: inv["entries"][0]["models"].extend(
            [{"id": "m", "source": "static"}] * 62
        ),
        lambda inv: inv["entries"][0]["models"].append(
            {"id": "x" * 129, "source": "static"}
        ),
        lambda inv: inv["entries"][0].update(login_state="token:abc"),
        lambda inv: inv.update(hash="md5:00"),
    ],
)
def test_inventory_limits_and_enums_are_enforced(mutate: Any) -> None:
    raw = _load("harness_inventory_copilot_signed_in.json")
    mutate(raw)
    with pytest.raises(ValidationError):
        HarnessInventory.model_validate(raw)


def test_every_session_message_type_has_a_fixture() -> None:
    seen = {_load(name)["type"] for name in SESSION_FIXTURES}
    assert seen == set(SESSION_MESSAGE_TYPES)


@pytest.mark.parametrize("name", SESSION_FIXTURES)
def test_session_message_fixture_round_trips(name: str) -> None:
    raw = _load(name)
    message = parse_runner_session_message(raw)
    assert message.type == raw["type"]
    assert message.model_dump(mode="json", exclude_none=True) == raw


def test_end_reasons() -> None:
    assert is_valid_end_reason("idle_timeout")
    assert is_valid_end_reason("runner_rejected:workspace_dirty")
    assert not is_valid_end_reason("runner_rejected:nope")
    with pytest.raises(ValidationError):
        SessionStateMessage(remote_session_id="s", state="ended", end_reason="bored")


def test_session_event_payload_capped_at_64_kb() -> None:
    with pytest.raises(ValidationError):
        SessionEventMessage(
            remote_session_id="s",
            turn_id="t",
            seq=0,
            kind="stderr",
            payload={"text": "x" * (64 * 1024)},
        )


def test_runner_hash_is_an_opaque_token_kept_when_unknown_entries_drop() -> None:
    """The server stores the runner's hash verbatim and never recomputes it.

    A newer runner hashes entries this server does not know; recomputing
    would never match the hash the runner keeps sending on heartbeat.
    """
    raw = _load("harness_inventory_copilot_signed_in.json")
    runner_hash = raw["hash"]
    raw["entries"].append(raw["entries"][1] | {"harness": "future_cli"})
    inventory = HarnessInventory.model_validate(raw)
    assert inventory.hash == runner_hash
    assert inventory.to_wire()["hash"] == runner_hash


def test_unicode_fixture_hash_matches_go() -> None:
    """U+2028/U+2029 are escaped like Go's encoding/json; other runes are not."""
    raw = _load("harness_inventory_unicode.json")
    inventory = HarnessInventory.model_validate(raw)
    assert " " in inventory.entries[0].display_name
    assert harness_inventory_hash(inventory.entries) == raw["hash"]


@pytest.mark.parametrize("state", ["ended", "failed"])
def test_terminal_session_state_requires_end_reason(state: str) -> None:
    with pytest.raises(ValidationError):
        SessionStateMessage(remote_session_id="s", state=state)


def test_rejected_end_reason_must_match_error_code() -> None:
    with pytest.raises(ValidationError):
        SessionStateMessage(
            remote_session_id="s",
            state="failed",
            end_reason="runner_rejected:workspace_dirty",
            error_code="checkout_failed",
        )
    assert SessionStateMessage(
        remote_session_id="s",
        state="failed",
        end_reason="runner_rejected:copilot_approval_hook_missing",
        error_code="copilot_approval_hook_missing",
    )
