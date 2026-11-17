"""Delivery reports read repositories without generating publication scripts."""

import base64
import gzip
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.container import K8S_INNER_SCRIPT_ENV_PREFIX
from preloop.utils.execve_limits import check_launch_payload, chunked_env


def report_context(repository_count: int = 500) -> dict[str, Any]:
    """Synthetic account with independently named repositories and no live services."""
    tracker_id = str(uuid4())
    context = {
        "account_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "prompt": "Read the repository history and write a delivery report.",
        "agent_config": {"report_kind": "delivery_report", "enable_auto_lint": False},
        "git_clone_config": {
            "enabled": True,
            "publication_mode": "legacy",
            "create_pull_request": False,
            "repositories": [
                {
                    "repository_url": f"https://github.com/example/project-{i}.git",
                    "tracker_id": tracker_id,
                    "branch": "main",
                }
                for i in range(repository_count)
            ],
        },
        "git_credentials_map": {
            tracker_id: {
                "token": "synthetic-installation-token",
                "tracker_type": "github",
                "auth_type": "github_app",
            }
        },
    }
    return context


@pytest.fixture
def large_report_context() -> dict[str, Any]:
    """A report with five hundred connected repositories."""
    return report_context()


@pytest.mark.parametrize("repository_count", [1, 80, 440, 500])
def test_report_launch_has_no_publication_capabilities(repository_count: int) -> None:
    """Existing presets use legacy clone config but must never prepare a push."""
    agent = CodexAgent({})
    context = report_context(repository_count)
    with patch(
        "preloop.api.endpoints.publication_credentials.mint_publication_capability",
        return_value="synthetic-capability",
    ) as mint:
        script = agent._build_codex_script(context)
        setup = json.loads(
            gzip.decompress(base64.b64decode(context["_repository_setup"]))
        )
        assert "git clone" in setup["script"]
        assert "project-0.git" not in script
        assert "synthetic-installation-token" not in script
        assert "git push" not in script
        assert "PRELOOP_PUBLICATION_REFRESH_URL" not in script
        mint.assert_not_called()
    check_launch_payload(
        command=["/bin/bash"],
        args=["-c", "exec bash /tmp/preloop/agent-script.sh"],
        env=chunked_env(K8S_INNER_SCRIPT_ENV_PREFIX, script),
    )


@pytest.mark.asyncio
async def test_private_report_launch_fits_runner_limits(
    large_report_context: dict[str, Any],
) -> None:
    """The runner accepts the complete launch, including the bootstrap env."""
    from preloop.agents.runner_launch import build_runner_launch

    launch = await build_runner_launch({**large_report_context, "agent_type": "codex"})
    check_launch_payload(
        command=["/bin/bash", "-c", "true"],
        env={**launch["env"], "PRELOOP_RUNNER_SCRIPT": launch["script"]},
    )
    from preloop.utils.execve_limits import total_launch_bytes, largest_launch_string

    print(
        json.dumps(
            {
                "repositories": len(
                    large_report_context["git_clone_config"]["repositories"]
                ),
                "private_script_bytes": len(launch["script"].encode()),
                "private_argument_environment_bytes": total_launch_bytes(
                    env={**launch["env"], "PRELOOP_RUNNER_SCRIPT": launch["script"]}
                ),
                "private_largest_string_bytes": largest_launch_string(
                    env={**launch["env"], "PRELOOP_RUNNER_SCRIPT": launch["script"]}
                ).size,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_count", [1, 440, 500])
async def test_hosted_report_uses_mounted_setup_within_all_limits(
    repository_count: int,
) -> None:
    """Measure the actual Job and Secret, including all arguments and env."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from kubernetes_asyncio.client import ApiClient
    from preloop.utils.execve_limits import total_launch_bytes, largest_launch_string
    from preloop.utils.repository_setup import SETUP_DIRECTORY, SETUP_FILENAME

    context = report_context(repository_count)
    context["flow_id"] = str(uuid4())
    agent = CodexAgent({})
    agent.use_kubernetes = True
    agent._init_kubernetes_clients = AsyncMock()
    agent._prepare_environment = AsyncMock(return_value={})
    agent._k8s_batch_api = AsyncMock()
    agent._k8s_batch_api.read_namespaced_job.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="synthetic-job-uid")
    )
    agent._k8s_core_api = AsyncMock()
    await agent._start_kubernetes_pod(context)
    job = agent._k8s_batch_api.create_namespaced_job.call_args.kwargs["body"]
    secret = agent._k8s_core_api.create_namespaced_secret.call_args.kwargs["body"]
    container = job.spec.template.spec.containers[0]
    env = {entry.name: entry.value for entry in container.env}
    check_launch_payload(command=container.command, args=container.args, env=env)
    # A fixed script and file reference leave the same launch size at any count.
    assert total_launch_bytes(container.command, container.args, env) < 160_000
    assert (
        largest_launch_string(container.command, container.args, env).size < 128 * 1024
    )
    assert "PRELOOP_GIT_CREDENTIALS" not in env
    assert any(
        m.mount_path == SETUP_DIRECTORY and m.read_only for m in container.volume_mounts
    )
    assert secret.metadata.owner_references[0].uid == "synthetic-job-uid"
    compressed = base64.b64decode(secret.data[SETUP_FILENAME])
    assert len(compressed) < 1024 * 1024
    setup = json.loads(gzip.decompress(compressed))
    assert f"project-{repository_count - 1}.git" in setup["script"]
    async with ApiClient() as serializer:
        job_bytes = len(json.dumps(serializer.sanitize_for_serialization(job)).encode())
        secret_bytes = len(
            json.dumps(serializer.sanitize_for_serialization(secret)).encode()
        )
        assert job_bytes < 1_500_000
        assert secret_bytes < 1_500_000
    print(
        json.dumps(
            {
                "repositories": repository_count,
                "hosted_argument_environment_bytes": total_launch_bytes(
                    container.command, container.args, env
                ),
                "hosted_largest_string_bytes": largest_launch_string(
                    container.command, container.args, env
                ).size,
                "job_json_bytes": job_bytes,
                "secret_json_bytes": secret_bytes,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_docker_seeds_before_start(large_report_context: dict[str, Any]) -> None:
    """The native Docker path uploads the same file before starting the process."""
    import io
    import tarfile
    from unittest.mock import AsyncMock, MagicMock
    from preloop.utils.repository_setup import SETUP_PATH

    agent = CodexAgent({})
    context = {**large_report_context, "flow_id": str(uuid4())}
    docker = MagicMock()
    docker.images.inspect = AsyncMock()
    container = MagicMock(id="synthetic-container")
    container.put_archive = AsyncMock()
    container.start = AsyncMock()
    docker.containers.create = AsyncMock(return_value=container)
    agent._get_docker_client = AsyncMock(return_value=docker)
    agent._prepare_environment = AsyncMock(return_value={})
    await agent._start_docker_container(context)
    assert container.method_calls[0][0] == "put_archive"
    assert container.method_calls[1][0] == "start"
    with tarfile.open(
        fileobj=io.BytesIO(container.put_archive.call_args.args[1])
    ) as archive:
        data = archive.extractfile(SETUP_PATH.lstrip("/")).read()
    setup = json.loads(gzip.decompress(data))
    assert "project-499.git" in setup["script"]


def test_setup_file_executes_without_losing_credentials_or_shell_state(
    tmp_path: Any,
) -> None:
    """A file seed executes intact and leaves the checkout cwd in the caller."""
    import os
    import subprocess
    from preloop.utils.repository_setup import SETUP_PATH, encode_setup, setup_shell

    target = tmp_path / "checkout"
    target.mkdir()
    payload = encode_setup(
        f"cd '{target}' && export SETUP_READY=yes", "synthetic-secret", 500
    )
    data_path = tmp_path / "setup.json.gz"
    data_path.write_bytes(base64.b64decode(payload))
    script = setup_shell().replace(SETUP_PATH, str(data_path))
    script = script.replace("/tmp/preloop", str(tmp_path / "runtime"))
    script = script.replace(
        "/tmp/.preloop-git-credentials", str(tmp_path / "credentials")
    )
    completed = subprocess.run(
        ["bash", "-ec", script + '\n[ "$SETUP_READY" = yes ] && pwd'],
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == str(target)
    assert (tmp_path / "credentials").read_text() == "synthetic-secret"
    assert (tmp_path / "credentials").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damaged", [False, True])
def test_private_setup_seed_round_trip(tmp_path: Any, damaged: bool) -> None:
    """The existing runner protocol reconstructs a complete file or fails closed."""
    import os
    import subprocess
    from preloop.utils.repository_setup import (
        SETUP_DIRECTORY,
        SETUP_PATH,
        encode_setup,
        setup_seed_env,
        setup_seed_shell,
    )

    encoded = encode_setup("echo ready\n" * 500, "synthetic-credential" * 500, 500)
    env = setup_seed_env(encoded)
    if damaged:
        env.pop("PRELOOP_REPOSITORY_SETUP_0")
    directory = tmp_path / "seed"
    script = setup_seed_shell().replace(SETUP_DIRECTORY, str(directory))
    completed = subprocess.run(
        ["bash", "-ec", script],
        capture_output=True,
        text=True,
        env={**os.environ, **env, "PRELOOP_DISABLE_TELEMETRY": "true"},
    )
    if damaged:
        assert completed.returncode != 0
    else:
        assert completed.returncode == 0, completed.stderr
        data = (directory / SETUP_PATH.rsplit("/", 1)[1]).read_bytes()
        setup = json.loads(gzip.decompress(data))
        assert setup["script"] == "echo ready\n" * 500
        assert setup["credentials"] == "synthetic-credential" * 500


@pytest.mark.asyncio
async def test_private_report_with_distinct_repository_credentials(
    large_report_context: dict[str, Any],
) -> None:
    """Many tracker credentials cannot become one oversized environment string."""
    import hashlib
    from preloop.agents.runner_launch import build_runner_launch, check_runner_launch

    context = {**large_report_context, "agent_type": "codex", "git_credentials_map": {}}
    for index, repo in enumerate(context["git_clone_config"]["repositories"]):
        tracker = str(uuid4())
        repo["tracker_id"] = tracker
        context["git_credentials_map"][tracker] = {
            "token": hashlib.sha512(f"synthetic-{index}".encode()).hexdigest() * 2,
            "tracker_type": "github",
        }
    launch = await build_runner_launch(context)
    assert launch["version"] == 1
    assert "PRELOOP_GIT_CREDENTIALS" not in launch["env"]
    check_runner_launch(launch["script"], launch["env"])
    check_launch_payload(
        env={**launch["env"], "PRELOOP_RUNNER_SCRIPT": launch["script"]}
    )
    assert len(json.dumps(launch).encode()) < 1024 * 1024


@pytest.mark.asyncio
async def test_setup_delivery_failure_removes_pending_job() -> None:
    """A refused Secret must not strand a Job with an unmountable volume."""
    from unittest.mock import AsyncMock
    from kubernetes_asyncio.client.exceptions import ApiException
    from preloop.agents.errors import AgentStartError

    agent = CodexAgent({})
    agent.use_kubernetes = True
    agent._init_kubernetes_clients = AsyncMock()
    agent._prepare_environment = AsyncMock(return_value={})
    agent._k8s_batch_api = AsyncMock()
    agent._deliver_repository_setup = AsyncMock(side_effect=ApiException(status=403))
    context = {**report_context(500), "flow_id": str(uuid4())}
    with pytest.raises(AgentStartError, match="500 repositories"):
        await agent._start_kubernetes_pod(context)
    agent._k8s_batch_api.delete_namespaced_job.assert_awaited_once()


def test_native_host_report_uses_data_plan_for_500_repositories(
    large_report_context: dict[str, Any],
) -> None:
    """Native host profiles already read checkout data and must admit reports."""
    from preloop.services.host_exec_delivery import build_host_exec_checkout

    plan = build_host_exec_checkout(large_report_context)
    assert len(plan["repositories"]) == 500
    assert plan["report_kind"] == "delivery_report"
    assert len(json.dumps(plan).encode()) < 1024 * 1024


@pytest.mark.asyncio
async def test_hosted_setup_secret_is_owner_only_and_unique_per_launch() -> None:
    """The credential-bearing Secret is owner-read only and never read back."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from preloop.utils.repository_setup import SETUP_FILE_MODE

    names = []
    for _ in range(2):
        context = {**report_context(3), "flow_id": str(uuid4())}
        context["execution_id"] = "synthetic-execution"
        agent = CodexAgent({})
        agent.use_kubernetes = True
        agent._init_kubernetes_clients = AsyncMock()
        agent._prepare_environment = AsyncMock(return_value={})
        agent._k8s_batch_api = AsyncMock()
        agent._k8s_batch_api.read_namespaced_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(uid="synthetic-job-uid")
        )
        agent._k8s_core_api = AsyncMock()
        await agent._start_kubernetes_pod(context)
        job = agent._k8s_batch_api.create_namespaced_job.call_args.kwargs["body"]
        secret = agent._k8s_core_api.create_namespaced_secret.call_args.kwargs["body"]
        volume = next(
            v for v in job.spec.template.spec.volumes if v.name == "repository-setup"
        )
        assert SETUP_FILE_MODE == 0o400
        assert volume.secret.default_mode == 0o400
        assert volume.secret.secret_name == secret.metadata.name
        assert len(secret.metadata.name) <= 63
        agent._k8s_core_api.read_namespaced_secret.assert_not_called()
        agent._k8s_core_api.replace_namespaced_secret.assert_not_called()
        names.append(secret.metadata.name)
    assert names[0] != names[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("container_user", "owner"), [(None, (0, 0)), ("1000:1000", (1000, 1000))]
)
async def test_docker_setup_file_is_readable_only_by_init_user(
    container_user: str | None, owner: tuple[int, int]
) -> None:
    """The harness user cannot read the uploaded credential store."""
    import io
    import tarfile
    from unittest.mock import AsyncMock
    from preloop.utils.repository_setup import SETUP_PATH, encode_setup

    agent = CodexAgent({})
    container = AsyncMock()
    context: dict[str, Any] = {"_repository_setup": encode_setup("true", "x", 1)}
    if container_user:
        context["_container_user"] = container_user
    await agent._seed_docker_repository_setup(container, context)
    with tarfile.open(
        fileobj=io.BytesIO(container.put_archive.call_args.args[1])
    ) as archive:
        entry = archive.getmember(SETUP_PATH.lstrip("/"))
    assert entry.mode == 0o400
    assert (entry.uid, entry.gid) == owner


def test_setup_shell_removes_transport_file_and_shares_credential_path(
    tmp_path: Any,
) -> None:
    """After extraction only the 0600 credential store remains."""
    import os
    import subprocess
    from preloop.utils.git_credentials import GIT_CREDENTIALS_FILE
    from preloop.utils.repository_setup import SETUP_PATH, encode_setup, setup_shell

    assert GIT_CREDENTIALS_FILE in setup_shell()
    data_path = tmp_path / "setup.json.gz"
    data_path.write_bytes(base64.b64decode(encode_setup("true", "secret", 1)))
    script = setup_shell().replace(SETUP_PATH, str(data_path))
    script = script.replace("/tmp/preloop", str(tmp_path / "runtime"))
    script = script.replace(GIT_CREDENTIALS_FILE, str(tmp_path / "credentials"))
    subprocess.run(
        ["bash", "-ec", script],
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        check=True,
    )
    assert not data_path.exists()
    assert (tmp_path / "credentials").read_text() == "secret"


def test_openhands_report_uses_mounted_setup() -> None:
    """The OpenHands harness gets the same constant-size report bootstrap."""
    from preloop.agents.openhands import OpenHandsAgent
    from preloop.utils.repository_setup import SETUP_CONTEXT_KEY, setup_shell

    context = report_context(500)
    script = OpenHandsAgent({})._prepare_git_clone_command(context)
    assert script == setup_shell()
    setup = json.loads(gzip.decompress(base64.b64decode(context[SETUP_CONTEXT_KEY])))
    assert "project-499.git" in setup["script"]


def test_generic_private_runner_refusal_does_not_mention_reports() -> None:
    """Ordinary flows keep the generic wording; reports name their scope."""
    from preloop.agents.runner_launch import (
        MAX_RUNNER_LAUNCH_STRING_BYTES,
        RunnerLaunchRejectedError,
        check_runner_launch,
    )

    script = "x" * (MAX_RUNNER_LAUNCH_STRING_BYTES + 1)
    with pytest.raises(RunnerLaunchRejectedError) as generic:
        check_runner_launch(script, {})
    assert "repositories)" not in str(generic.value)
    assert "report" not in str(generic.value).lower()
    with pytest.raises(RunnerLaunchRejectedError) as report:
        check_runner_launch(script, {"PRELOOP_REPOSITORY_COUNT": "440"})
    assert "(440 repositories)" in str(report.value)
    assert "The report could not start." in str(report.value)
