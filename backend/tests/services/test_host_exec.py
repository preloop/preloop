"""Host execution profile advertisement, validation, and completion."""

from types import SimpleNamespace

import pytest

from preloop.models.schemas.flow_runner import HostExecProfileAdvertisement
from preloop.services.host_exec import (
    HOST_EXEC_AGENT_TYPE,
    finalize_runner_completion,
    host_exec_flow_error,
    host_exec_harness,
    host_exec_model_identifier,
    host_exec_profile_name,
    host_exec_unavailable_reason,
    normalize_host_exec_advertisements,
    is_host_exec_agent_type,
    runner_has_host_exec_profile,
    validate_host_exec_completion,
)


def test_normalize_strips_executables() -> None:
    stored = normalize_host_exec_advertisements(
        [
            {
                "name": "cursor-ask",
                "executable": "/bin/sh",
                "argv": ["-c", "id"],
                "capabilities": ["host_exec", "cursor_cli", "stdout", "cancel"],
            },
            {"name": "cursor-ask", "capabilities": ["host_exec"]},
        ]
    )
    assert stored == {
        "host_exec_profiles": [
            {
                "name": "cursor-ask",
                "capabilities": ["host_exec", "cursor_cli", "stdout", "cancel"],
            }
        ]
    }
    dumped = str(stored)
    assert "/bin/sh" not in dumped
    assert "argv" not in dumped


def test_normalize_accepts_pydantic_register_advertisements() -> None:
    stored = normalize_host_exec_advertisements(
        [
            HostExecProfileAdvertisement(
                name="cursor-ask",
                capabilities=["host_exec", "cursor_cli"],
                models=["composer-2.5"],
            )
        ]
    )
    assert stored == {
        "host_exec_profiles": [
            {
                "name": "cursor-ask",
                "capabilities": ["host_exec", "cursor_cli"],
                "models": ["composer-2.5"],
            }
        ]
    }


def test_runner_has_host_exec_profile() -> None:
    runner = SimpleNamespace(
        capabilities={
            "host_exec_profiles": [
                {"name": "cursor-ask", "capabilities": ["host_exec", "cursor_cli"]}
            ]
        }
    )
    assert runner_has_host_exec_profile(runner, "cursor-ask")
    assert not runner_has_host_exec_profile(runner, "missing")
    assert not runner_has_host_exec_profile(
        SimpleNamespace(capabilities={}), "cursor-ask"
    )


def test_flow_errors_for_hosted_and_harness_mismatch() -> None:
    assert host_exec_flow_error(
        agent_type="cursor",
        agent_config={"host_exec_profile": "cursor-ask"},
        runner_pool="server",
    )
    assert host_exec_flow_error(
        agent_type="codex",
        agent_config={"host_exec_profile": "cursor-ask"},
        runner_pool="office-mac",
    )
    assert host_exec_flow_error(
        agent_type="cursor",
        agent_config={},
        runner_pool="office-mac",
    )
    assert (
        host_exec_flow_error(
            agent_type="cursor",
            agent_config={"host_exec_profile": "cursor-ask"},
            runner_pool="office-mac",
        )
        is None
    )


_PULL_REQUEST_UNAVAILABLE = (
    "host execution cannot publish pull requests; isolated "
    "publication is unavailable on this path"
)


def test_unavailable_publication_and_resume() -> None:
    assert (
        host_exec_unavailable_reason(git_clone_config={"create_pull_request": True})
        == _PULL_REQUEST_UNAVAILABLE
    )
    assert host_exec_unavailable_reason(
        resume_from="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert host_exec_unavailable_reason(session_id="ses-untrusted")
    assert (
        host_exec_unavailable_reason(git_clone_config={"setup_commands": ["make"]})
        == "host execution does not run remote clone setup commands"
    )


def test_unavailable_reason_allows_checkout_for_review_flows() -> None:
    """A clone without publication or setup commands can run on a host."""
    assert (
        host_exec_unavailable_reason(
            git_clone_config={
                "enabled": True,
                "repositories": [{"repository_url": "https://git.example.com/o/r"}],
                "create_pull_request": False,
            }
        )
        is None
    )


def test_unavailable_reason_rejects_isolated_publication_mode() -> None:
    """Isolated publication is unavailable on native host profiles."""
    from_config = host_exec_unavailable_reason(
        git_clone_config={"publication_mode": "isolated"}
    )
    from_keyword = host_exec_unavailable_reason(publication_mode="isolated")
    assert from_config is not None
    assert from_keyword == from_config
    assert "isolated publication" in from_config
    assert "native host" in from_config
    assert (
        host_exec_unavailable_reason(git_clone_config={"publication_mode": "legacy"})
        is None
    )
    assert host_exec_unavailable_reason(publication_mode="legacy") is None


def test_unavailable_reason_create_pull_request_unchanged() -> None:
    """create_pull_request keeps its existing host-exec reason."""
    assert (
        host_exec_unavailable_reason(git_clone_config={"create_pull_request": True})
        == _PULL_REQUEST_UNAVAILABLE
    )
    combined = host_exec_unavailable_reason(
        git_clone_config={
            "create_pull_request": True,
            "publication_mode": "isolated",
        }
    )
    assert combined == _PULL_REQUEST_UNAVAILABLE


def test_host_exec_success_requires_structured_result() -> None:
    failed, _, _ = validate_host_exec_completion(
        {"status": "SUCCEEDED", "exit_code": 0}
    )
    assert failed == "FAILED"
    failed, _, _ = validate_host_exec_completion(
        {
            "status": "SUCCEEDED",
            "completion_protocol": "host_exec",
            "exit_code": 0,
        }
    )
    assert failed == "FAILED"
    status, error, result = validate_host_exec_completion(
        {
            "status": "SUCCEEDED",
            "completion_protocol": "host_exec",
            "host_exec_profile": "cursor-ask",
            "exit_code": 0,
            "result": {"status": "success", "harness": "cursor_cli"},
        }
    )
    assert status == "SUCCEEDED"
    assert error is None
    assert result["harness"] == "cursor_cli"


def test_finalize_does_not_treat_docker_launch_as_host_exec() -> None:
    status, error, _ = finalize_runner_completion(
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "exit_code": 0,
            "result": {"status": "success"},
        },
        pending_job={
            "host_exec_profile": "cursor-ask",
            "agent_type": "cursor",
            "completion_protocol": "host_exec",
        },
    )
    assert status == "FAILED"
    assert error
    assert "host_exec" in error


def test_finalize_legacy_docker_complete_without_launch_version() -> None:
    status, error, result = finalize_runner_completion(
        {"status": "SUCCEEDED"},
        pending_job={
            "agent_type": "codex",
            "agent_config": {"image": "example/codex:1"},
        },
    )
    assert status == "FAILED"
    assert error
    assert result is None


def test_profile_name_from_payload() -> None:
    assert (
        host_exec_profile_name(
            {"image": "example/codex:1"},
            {"host_exec_profile": "cursor-ask", "agent_type": HOST_EXEC_AGENT_TYPE},
        )
        == "cursor-ask"
    )


def test_profile_requires_cursor_capability_and_explicit_model_mapping():
    runner = SimpleNamespace(
        capabilities={
            "host_exec_profiles": [{"name": "local", "capabilities": ["host_exec"]}]
        }
    )
    assert not runner_has_host_exec_profile(runner, "local")
    profile = runner.capabilities["host_exec_profiles"][0]
    profile["capabilities"].append("cursor_cli")
    assert runner_has_host_exec_profile(runner, "local")
    assert not runner_has_host_exec_profile(runner, "local", "requested-model")
    profile["models"] = ["requested-model"]
    assert runner_has_host_exec_profile(runner, "local", "requested-model")
    assert not runner_has_host_exec_profile(runner, "local", "another-model")


@pytest.mark.parametrize(
    "pending,message",
    [
        (
            {"launch_version": 1, "agent_type": "codex"},
            {"completion_protocol": "host_exec", "host_exec_profile": "local"},
        ),
        (
            {
                "completion_protocol": "host_exec",
                "agent_type": "cursor",
                "host_exec_profile": "local",
            },
            {"completion_protocol": "docker_v1", "launch_version": 1},
        ),
        (
            {
                "completion_protocol": "host_exec",
                "agent_type": "cursor",
                "host_exec_profile": "local",
            },
            {"completion_protocol": "host_exec", "host_exec_profile": "another"},
        ),
        (
            {
                "completion_protocol": "host_exec",
                "agent_type": "cursor",
                "host_exec_profile": "local",
            },
            {},
        ),
        ({}, {"completion_protocol": "host_exec", "host_exec_profile": "local"}),
        ({}, {}),
    ],
)
def test_protocol_and_profile_cannot_be_chosen_by_completion_message(pending, message):
    message = {
        "status": "SUCCEEDED",
        "exit_code": 0,
        "result": {"status": "success", "harness": "cursor_cli"},
        **message,
    }
    assert finalize_runner_completion(message, pending_job=pending)[0] == "FAILED"


def test_native_completion_matches_durable_lease_without_model_inference():
    result = {"status": "success", "harness": "cursor_cli"}
    message = {
        "status": "SUCCEEDED",
        "completion_protocol": "host_exec",
        "host_exec_profile": "local",
        "exit_code": 0,
        "result": result,
    }
    pending = {
        "completion_protocol": "host_exec",
        "agent_type": "cursor",
        "host_exec_profile": "local",
        "model_identifier": "requested-model",
    }
    assert finalize_runner_completion(message, pending_job=pending) == (
        "SUCCEEDED",
        None,
        {**result, "gateway_metered": False},
    )
    assert "model" not in result


def _copilot_runner(**profile: object) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities={
            "host_exec_profiles": [
                {
                    "name": "copilot-seat",
                    "capabilities": ["host_exec", "copilot_cli"],
                    **profile,
                }
            ]
        }
    )


def test_copilot_is_a_host_exec_agent_type() -> None:
    assert is_host_exec_agent_type("copilot")
    assert is_host_exec_agent_type(" Cursor ")
    assert not is_host_exec_agent_type("codex")
    assert not is_host_exec_agent_type(None)
    assert host_exec_harness("copilot") == "copilot_cli"
    assert host_exec_harness("cursor") == "cursor_cli"
    assert host_exec_harness("claude") is None


def test_normalize_keeps_copilot_capability() -> None:
    stored = normalize_host_exec_advertisements(
        [
            {
                "name": "copilot-seat",
                "capabilities": ["host_exec", "copilot_cli", "stdout", "cancel"],
                "models": ["claude-sonnet-4.6"],
            }
        ]
    )
    assert stored["host_exec_profiles"][0]["capabilities"] == [
        "host_exec",
        "copilot_cli",
        "stdout",
        "cancel",
    ]


def test_runner_profile_must_match_leased_harness() -> None:
    copilot = _copilot_runner(models=["team-default"])
    assert runner_has_host_exec_profile(copilot, "copilot-seat", agent_type="copilot")
    assert runner_has_host_exec_profile(
        copilot, "copilot-seat", "team-default", agent_type="copilot"
    )
    assert not runner_has_host_exec_profile(
        copilot, "copilot-seat", "other", agent_type="copilot"
    )
    # A Cursor lease never lands on a Copilot profile, nor the reverse.
    assert not runner_has_host_exec_profile(copilot, "copilot-seat")
    assert not runner_has_host_exec_profile(
        copilot, "copilot-seat", agent_type="cursor"
    )
    cursor = SimpleNamespace(
        capabilities={
            "host_exec_profiles": [
                {"name": "cursor-ask", "capabilities": ["host_exec", "cursor_cli"]}
            ]
        }
    )
    assert not runner_has_host_exec_profile(cursor, "cursor-ask", agent_type="copilot")
    assert not runner_has_host_exec_profile(copilot, "copilot-seat", agent_type="codex")


def test_copilot_flow_errors() -> None:
    assert "copilot requires agent_config.host_exec_profile" in (
        host_exec_flow_error(
            agent_type="copilot", agent_config={}, runner_pool="office-mac"
        )
        or ""
    )
    assert host_exec_flow_error(
        agent_type="copilot",
        agent_config={"host_exec_profile": "copilot-seat"},
        runner_pool="server",
    )
    assert (
        host_exec_flow_error(
            agent_type="copilot",
            agent_config={"host_exec_profile": "copilot-seat"},
            runner_pool="office-mac",
        )
        is None
    )


def test_host_exec_model_identifier_reads_matching_key() -> None:
    config = {"cursor_model": " composer-2.5 ", "copilot_model": "gpt-5.2"}
    assert host_exec_model_identifier("cursor", config) == "composer-2.5"
    assert host_exec_model_identifier("copilot", config) == "gpt-5.2"
    assert host_exec_model_identifier("copilot", {"cursor_model": "x"}) is None
    assert host_exec_model_identifier("codex", config) is None
    assert host_exec_model_identifier("copilot", None) is None


def _copilot_completion(result: dict) -> dict:
    return {
        "status": "SUCCEEDED",
        "completion_protocol": "host_exec",
        "host_exec_profile": "copilot-seat",
        "exit_code": 0,
        "result": result,
    }


_COPILOT_LEASE = {
    "completion_protocol": "host_exec",
    "agent_type": "copilot",
    "host_exec_profile": "copilot-seat",
}


def test_copilot_completion_is_marked_not_gateway_metered() -> None:
    result = {
        "status": "success",
        "harness": "copilot_cli",
        "session_id": "33810b72-1e9a-4a02-bbcb-a125d10b886c",
        "premium_requests": 1,
        # Agent JSON cannot claim gateway metering; the server overrides it.
        "gateway_metered": True,
    }
    status, error, stored = finalize_runner_completion(
        _copilot_completion(result), pending_job=_COPILOT_LEASE
    )
    assert (status, error) == ("SUCCEEDED", None)
    assert stored == {**result, "gateway_metered": False}


def test_copilot_failure_keeps_named_error_and_marker() -> None:
    message = {
        **_copilot_completion({"status": "failure", "harness": "copilot_cli"}),
        "status": "FAILED",
        "exit_code": 1,
        "error": "copilot_not_logged_in: the runner user is not signed in",
    }
    status, error, stored = finalize_runner_completion(
        message, pending_job=_COPILOT_LEASE
    )
    assert status == "FAILED"
    assert error.startswith("copilot_not_logged_in")
    assert stored["gateway_metered"] is False


@pytest.mark.parametrize(
    "pending_type,harness",
    [("copilot", "cursor_cli"), ("cursor", "copilot_cli"), ("copilot", None)],
)
def test_completion_harness_must_match_leased_agent_type(pending_type, harness):
    result = {"status": "success"}
    if harness:
        result["harness"] = harness
    status, error, _ = finalize_runner_completion(
        _copilot_completion(result),
        pending_job={**_COPILOT_LEASE, "agent_type": pending_type},
    )
    assert status == "FAILED"
    assert "harness" in (error or "")


def test_unknown_host_exec_lease_agent_type_fails() -> None:
    status, error, _ = finalize_runner_completion(
        _copilot_completion({"status": "success", "harness": "codex"}),
        pending_job={**_COPILOT_LEASE, "agent_type": "codex"},
    )
    assert (status, error) == ("FAILED", "Invalid durable host execution lease")
