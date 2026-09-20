"""Trust boundaries and failure cleanup for real agent deployments."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncssh
import httpx
import pytest
from pydantic import ValidationError

from preloop.schemas.agent_deployment import AgentDeploymentRequest, AgentDeploymentSSH
from preloop.services import agent_deployment as service
from preloop.services import agent_deployment_gcp as gcp


def ssh_input(**overrides):
    values = dict(
        host="203.0.113.10",
        username="operator",
        host_key=asyncssh.generate_private_key("ssh-ed25519")
        .export_public_key()
        .decode(),
        private_key=asyncssh.generate_private_key("ssh-ed25519")
        .export_private_key()
        .decode(),
    )
    return AgentDeploymentSSH(**(values | overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"password": "secret"},
        {"private_key": None},
        {"username": "root; touch /tmp/bad"},
        {"port": 0},
    ],
)
def test_ssh_rejects_ambiguous_or_invalid_credentials(overrides):
    with pytest.raises(ValidationError):
        ssh_input(**overrides)


def test_request_rejects_unused_secrets_and_unknown_runtime():
    base = dict(
        idempotency_key=uuid4(), model_id=uuid4(), target="gcp", runtime="hermes"
    )
    with pytest.raises(ValidationError):
        AgentDeploymentRequest(**base, ssh=ssh_input())
    with pytest.raises(ValidationError):
        AgentDeploymentRequest(**(base | {"runtime": "shell"}))


def test_secret_fields_do_not_leak_in_repr():
    data = ssh_input(private_key=None, password="must-never-log")
    assert "must-never-log" not in repr(data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "::1",
        "::ffff:169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
async def test_network_policy_rejects_internal_and_metadata_destinations(
    address, monkeypatch
):
    monkeypatch.delenv("PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS", raising=False)
    with patch.object(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(None, None, None, None, (address, 22))]),
    ):
        with pytest.raises(service.DeploymentError):
            await service.resolve_ssh_address("attacker.example", 22)


@pytest.mark.asyncio
async def test_dns_mixed_public_private_is_rejected_and_cannot_rebind(monkeypatch):
    monkeypatch.delenv("PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS", raising=False)
    answers = [
        (None, None, None, None, (address, 22)) for address in ["8.8.8.8", "10.0.0.1"]
    ]
    with patch.object(
        asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=answers)
    ) as resolve:
        with pytest.raises(service.DeploymentError):
            await service.resolve_ssh_address("mixed.example", 22)
        resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_can_allow_private_network_but_never_metadata(monkeypatch):
    monkeypatch.setenv("PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS", "10.0.0.0/8,169.254.0.0/16")
    with patch.object(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(None, None, None, None, ("10.2.3.4", 22))]),
    ):
        assert await service.resolve_ssh_address("private.example", 22) == "10.2.3.4"
    with patch.object(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(None, None, None, None, ("169.254.169.254", 22))]),
    ):
        with pytest.raises(service.DeploymentError):
            await service.resolve_ssh_address("metadata.google.internal", 22)


def test_host_key_is_explicit_and_known_hosts_prefix_supported():
    key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    assert service.parse_host_key(key) == service.parse_host_key("example.org " + key)
    with pytest.raises(service.DeploymentError):
        service.parse_host_key("SHA256:fingerprint-is-not-a-public-key")


def test_script_quotes_alias_and_does_not_use_cli_secrets_in_argv(monkeypatch):
    monkeypatch.delenv("PRELOOP_DEPLOY_CLI_URL", raising=False)
    script = service.installation_script(
        "hermes", "x'; echo owned; '", "https://test.example", "private-token", uuid4()
    )
    assert "deploy_alias='x'\"'\"'; echo owned; '\"'\"''" in script
    assert "--token" not in script
    assert 'PRELOOP_TOKEN="$deploy_token"' in script
    assert "flock -n 9" in script
    assert 'rm -rf "$work"' in script


@pytest.mark.parametrize(
    "url,checksum",
    [("http://example.com/bin", "a" * 64), ("https://example.com/bin", "bad")],
)
def test_override_download_requires_https_and_checksum(monkeypatch, url, checksum):
    monkeypatch.setenv("PRELOOP_DEPLOY_CLI_URL", url)
    monkeypatch.setenv("PRELOOP_DEPLOY_CLI_SHA256", checksum)
    with pytest.raises(service.DeploymentError):
        service.installation_script(
            "hermes", "alias", "https://example.com", "token", uuid4()
        )


def test_idempotency_name_stable_and_account_scoped():
    request_id = uuid4()
    assert service.deployment_vm_name("a", request_id) == service.deployment_vm_name(
        "a", request_id
    )
    assert service.deployment_vm_name("a", request_id) != service.deployment_vm_name(
        "b", request_id
    )


@pytest.fixture
def gcp_env(monkeypatch):
    monkeypatch.setenv("PRELOOP_DEPLOY_GCP_PROJECT", "test-project")
    monkeypatch.setenv("PRELOOP_DEPLOY_GCP_ZONE", "us-central1-a")


@pytest.mark.asyncio
async def test_gcp_create_has_no_child_identity_and_pins_host_key(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    host_key = (
        asyncssh.generate_private_key("ssh-ed25519")
        .export_public_key()
        .decode()
        .strip()
    )
    vm.request = AsyncMock(
        side_effect=[
            {"name": "operation"},
            {"networkInterfaces": [{"accessConfigs": [{"natIP": "8.8.8.8"}]}]},
            {"contents": f"PRELOOP_HOSTKEY_{vm.request_id.hex}={host_key}"},
        ]
    )
    vm.operation = AsyncMock()
    ssh = await vm.create()
    body = vm.request.await_args_list[0].args[2]
    assert body["serviceAccounts"] == []
    assert body["labels"]["preloop-attempt"] == vm.attempt
    assert "token" not in json.dumps(body).lower()
    assert ssh.host_key == host_key.split(" ")[0] + " " + host_key.split(" ")[1]
    assert ssh.username == "preloop"


@pytest.mark.asyncio
async def test_conflicting_create_cannot_cleanup_existing_vm(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    vm.created = True
    vm.client = AsyncMock()
    vm.client.request.return_value = httpx.Response(409)
    with pytest.raises(service.DeploymentError):
        await vm.request("POST", "instances", {})
    assert not vm.created
    await vm.cleanup()
    vm.client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_refuses_other_attempt_even_same_idempotency_key(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    vm.created = True
    vm.client = AsyncMock()
    vm.client.get.return_value = httpx.Response(
        200,
        json={
            "labels": {
                "preloop-deployment": vm.request_id.hex,
                "preloop-account": vm.account_id,
                "preloop-attempt": "another",
            }
        },
    )
    with pytest.raises(service.DeploymentError):
        await vm.cleanup()
    vm.client.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        service.DeploymentError("install failed"),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
)
async def test_failed_timed_out_and_cancelled_deployments_cleanup(gcp_env, failure):
    ssh = ssh_input(host="8.8.8.8")
    vm = MagicMock()
    vm.create = AsyncMock(return_value=ssh)
    vm.cleanup = AsyncMock()
    vm.name = "preloop-agent-test"
    connection = MagicMock()
    connection.__aenter__ = AsyncMock()
    connection.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(gcp, "GCPDeployment", return_value=vm),
        patch.object(gcp, "_access_token", AsyncMock(return_value="cloud-token")),
        patch.object(gcp.asyncssh, "connect", return_value=connection),
    ):
        with pytest.raises(type(failure)):
            async with gcp.provision_gcp("account", uuid4(), "standard"):
                raise failure
    vm.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_gcp_deployment_is_retained(gcp_env):
    vm = MagicMock()
    vm.create = AsyncMock(return_value=ssh_input(host="8.8.8.8"))
    vm.cleanup = AsyncMock()
    connection = MagicMock()
    connection.__aenter__ = AsyncMock()
    connection.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(gcp, "GCPDeployment", return_value=vm),
        patch.object(gcp, "_access_token", AsyncMock(return_value="cloud-token")),
        patch.object(gcp.asyncssh, "connect", return_value=connection),
    ):
        async with gcp.provision_gcp("account", uuid4(), "standard"):
            pass
    vm.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_ipv4_mapped_metadata_is_forbidden_even_with_broad_allowlist(monkeypatch):
    monkeypatch.setenv("PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS", "::/0,0.0.0.0/0")
    with patch.object(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(
            return_value=[(None, None, None, None, ("::ffff:169.254.169.254", 22))]
        ),
    ):
        with pytest.raises(service.DeploymentError):
            await service.resolve_ssh_address("metadata.example", 22)


def ssh_connection(output, exit_status=0):
    process = MagicMock()
    process.__aenter__ = AsyncMock(return_value=process)
    process.__aexit__ = AsyncMock(return_value=False)
    process.stdout.read = AsyncMock(return_value=output)
    process.wait_closed = AsyncMock()
    process.exit_status = exit_status
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=False)
    connection.create_process.return_value = process
    return connection, process


@pytest.mark.asyncio
async def test_untrusted_failure_text_is_not_returned_or_logged():
    connection, _ = ssh_connection("PRELOOP_DEPLOY_bearer-secret\n", 1)
    with (
        patch.object(service, "resolve_ssh_address", AsyncMock(return_value="8.8.8.8")),
        patch.object(service.asyncssh, "connect", return_value=connection),
    ):
        with pytest.raises(service.DeploymentError) as exc:
            await service.install_over_ssh(
                ssh_input(),
                runtime="hermes",
                alias="model",
                url="https://test.example",
                token="private-token",
                request_id=uuid4(),
            )
    assert "bearer-secret" not in str(exc.value)
    assert "PRELOOP_DEPLOY_FAILED" in str(exc.value)
    assert connection.create_process.call_args.kwargs["stderr"] == asyncssh.DEVNULL


@pytest.mark.asyncio
async def test_remote_version_is_reduced_to_runtime_and_semver():
    agent_id = str(uuid4())
    connection, _ = ssh_connection(
        json.dumps(
            {
                "agent_id": agent_id,
                "runtime_version": "Hermes v2026.9.14\nprivate-bearer-token",
                "model_alias": "model",
            }
        )
    )
    with (
        patch.object(service, "resolve_ssh_address", AsyncMock(return_value="8.8.8.8")),
        patch.object(service.asyncssh, "connect", return_value=connection),
    ):
        result = await service.install_over_ssh(
            ssh_input(),
            runtime="hermes",
            alias="model",
            url="https://test.example",
            token="private-token",
            request_id=uuid4(),
        )
    assert result.runtime_version == "hermes v2026.9.14"
    assert result.agent_id == agent_id


@pytest.mark.asyncio
async def test_excess_remote_output_terminates_session():
    connection, process = ssh_connection("x" * 65537)
    with (
        patch.object(service, "resolve_ssh_address", AsyncMock(return_value="8.8.8.8")),
        patch.object(service.asyncssh, "connect", return_value=connection),
    ):
        with pytest.raises(service.DeploymentError, match="excessive"):
            await service.install_over_ssh(
                ssh_input(),
                runtime="hermes",
                alias="model",
                url="https://test.example",
                token="private-token",
                request_id=uuid4(),
            )
    process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_ambiguous_creation_is_never_reported_as_successful_cleanup(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    vm.created = True
    vm.client = AsyncMock()
    vm.client.get.return_value = httpx.Response(404)
    with patch.object(gcp.asyncio, "sleep", AsyncMock()):
        with pytest.raises(service.DeploymentError, match="uncertain"):
            await vm.cleanup()
    vm.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_failed_insert_without_resource_needs_no_deletion(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    vm.created = True
    vm.operation_name = "failed-operation"
    vm.insert_completed = True
    vm.operation = AsyncMock(side_effect=service.DeploymentError("operation failed"))
    vm.client = AsyncMock()
    vm.client.get.return_value = httpx.Response(404)
    await vm.cleanup()
    vm.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_gcp_transport_error_is_safe_and_preserves_uncertain_ownership(gcp_env):
    vm = gcp.GCPDeployment(str(uuid4()), uuid4(), "standard")
    vm.created = True
    vm.client = AsyncMock()
    vm.client.request.side_effect = httpx.ConnectError(
        "private-token in remote diagnostic"
    )
    with pytest.raises(service.DeploymentError) as exc:
        await vm.request("POST", "instances", {})
    assert "private-token" not in str(exc.value)
    assert vm.created
