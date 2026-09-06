"""Host execution profile advertisement, validation, and completion."""

from types import SimpleNamespace

import pytest

from preloop.services.host_exec import (
    HOST_EXEC_AGENT_TYPE,
    finalize_runner_completion,
    host_exec_flow_error,
    host_exec_profile_name,
    host_exec_unavailable_reason,
    normalize_host_exec_advertisements,
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


def test_unavailable_publication_and_resume() -> None:
    assert host_exec_unavailable_reason(git_clone_config={"create_pull_request": True})
    assert host_exec_unavailable_reason(
        resume_from="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert host_exec_unavailable_reason(session_id="ses-untrusted")
    assert host_exec_unavailable_reason(git_clone_config={"enabled": True})


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
        result,
    )
    assert "model" not in result
