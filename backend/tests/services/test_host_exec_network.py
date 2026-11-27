"""Network opt-in for host profiles and agent-reported failure status."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.models.crud import crud_flow_runner
from preloop.services.flow_failure_messages import agent_reported_failure_message
from preloop.services.host_exec import (
    HOST_EXEC_NETWORK_LEASE_KEY,
    NETWORK_UNAVAILABLE,
    host_exec_requires_network,
    normalize_host_exec_advertisements,
    runner_has_host_exec_profile,
    validate_host_exec_completion,
)
from preloop.services.runner_service import lease_job


def _runner(*caps: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={
            "host_exec_profiles": [
                {"name": "codex", "capabilities": ["host_exec", "codex_cli", *caps]}
            ]
        },
    )


def test_requires_network_is_an_explicit_true() -> None:
    assert host_exec_requires_network({"requires_network": True})
    assert not host_exec_requires_network({"requires_network": "yes"})
    assert not host_exec_requires_network({})
    assert not host_exec_requires_network(None)
    wrapped = {"agent_config": {"host_exec_profile": "codex", "requires_network": True}}
    assert host_exec_requires_network(wrapped)


def test_registration_keeps_host_network_capability() -> None:
    stored = normalize_host_exec_advertisements(
        [{"name": "codex", "capabilities": ["host_exec", "codex_cli", "host_network"]}]
    )
    assert "host_network" in stored["host_exec_profiles"][0]["capabilities"]


def test_network_lease_requires_advertised_capability() -> None:
    assert not runner_has_host_exec_profile(
        _runner(), "codex", None, "codex", require_network=True
    )
    assert runner_has_host_exec_profile(
        _runner("host_network"), "codex", None, "codex", require_network=True
    )
    assert runner_has_host_exec_profile(_runner(), "codex", None, "codex")


def _reject(
    monkeypatch: pytest.MonkeyPatch,
    runners: list,
    config: dict,
    clone: object = None,
) -> None:
    from preloop.api.endpoints.flows import _reject_host_exec_flow

    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: list(runners)
    )
    _reject_host_exec_flow(
        agent_type="codex",
        agent_config={"host_exec_profile": "codex", **config},
        runner_pool="office-mac",
        git_clone_config=clone,
        db=MagicMock(),
        account_id=uuid4(),
    )


def test_save_rejects_network_flow_without_network_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as caught:
        _reject(monkeypatch, [_runner()], {"requires_network": True})
    assert caught.value.status_code == 400
    assert caught.value.detail == NETWORK_UNAVAILABLE
    assert "allow_network" in NETWORK_UNAVAILABLE


def test_save_accepts_network_flow_with_network_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject(
        monkeypatch, [_runner(), _runner("host_network")], {"requires_network": True}
    )


def test_save_ignores_network_when_flow_does_not_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject(monkeypatch, [_runner()], {})


def test_network_lease_is_data_and_assigned_only_to_network_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RemoteRunnerExecutor(
        "codex",
        {"host_exec_profile": "codex", "requires_network": True},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
    )
    payload = executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="review CI",
        execution_context={
            "agent_type": "codex",
            "agent_config": {"host_exec_profile": "codex", "requires_network": True},
        },
    )
    assert payload[HOST_EXEC_NETWORK_LEASE_KEY] is True

    offline, capable = _runner(), _runner("host_network")
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [offline, capable]
    )

    def claim(db: object, *, runner_id: object) -> SimpleNamespace:
        if runner_id != capable.id:
            raise AssertionError("a profile without network must not be claimed")
        return capable

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
    lease = {
        "host_exec_profile": "codex",
        "agent_type": "codex",
        HOST_EXEC_NETWORK_LEASE_KEY: True,
    }
    kwargs = dict(account_id=uuid4(), pool="local", execution_id=uuid4())
    assert lease_job(MagicMock(), payload=lease, **kwargs) is capable


def test_agent_failure_message_carries_the_agent_summary() -> None:
    message = agent_reported_failure_message(
        {"status": "failure", "summary": "could not resolve api.github.com"},
        "status",
        "failure",
    )
    assert message.startswith("Agent reported an explicit failure in result.json")
    assert message.endswith("could not resolve api.github.com")
    bare = agent_reported_failure_message({"status": "failure"}, "status", "failure")
    assert bare == "Agent reported an explicit failure in result.json " + (
        "(status='failure')."
    )


def _host_completion(status: str, result: dict, error: str = "") -> dict:
    return {
        "status": status,
        "completion_protocol": "host_exec",
        "exit_code": 0,
        "result": result,
        "error": error or None,
    }


def test_host_completion_with_agent_failure_is_failed() -> None:
    """A runner that forwards SUCCEEDED with a failure result still fails."""
    status, _, _ = validate_host_exec_completion(
        _host_completion("SUCCEEDED", {"status": "failure", "harness": "codex_cli"})
    )
    assert status == "FAILED"


def test_host_completion_keeps_the_runner_agent_summary() -> None:
    error = "Agent reported an explicit failure in result.json. review failed"
    status, message, _ = validate_host_exec_completion(
        _host_completion("FAILED", {"status": "failure"}, error)
    )
    assert (status, message) == ("FAILED", error)


def test_host_agent_failure_is_classified_agent_reported() -> None:
    from preloop.services.flow_failure_category import derive_failure_category

    error = "Agent reported an explicit failure in result.json. review failed"
    category = derive_failure_category(status="FAILED", error_message=error)
    assert category == "agent_reported_failure"


def test_save_rejects_read_only_review_that_requires_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.host_exec import READ_ONLY_NETWORK_UNAVAILABLE

    clone = {
        "enabled": True,
        "publication_mode": "read_only",
        "repositories": [{"repository_url": "https://example.com/acme/app.git"}],
    }
    with pytest.raises(HTTPException) as caught:
        _reject(
            monkeypatch, [_runner("host_network")], {"requires_network": True}, clone
        )
    assert caught.value.detail == READ_ONLY_NETWORK_UNAVAILABLE
