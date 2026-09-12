"""Capacity wrapper and effective runtime evidence checks without Docker."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.capacity import collect


def test_up_builds_shared_application_image_once(tmp_path: Path) -> None:
    """Compose must not race several services exporting the same image tag."""
    docker = tmp_path / "docker"
    log = tmp_path / "docker-calls.jsonl"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPACITY_TEST_CALLS'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['context', 'inspect']:\n"
        "    print(json.dumps([{'Endpoints': {'docker': {'Host': 'unix:///test'}}}]))\n"
    )
    docker.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        CAPACITY_TEST_CALLS=str(log),
        CAPACITY_REVISION="synthetic",
    )
    env.pop("DOCKER_HOST", None)
    env.pop("CAPACITY_IMAGE", None)
    subprocess.run(["bash", "scripts/capacity/lab.sh", "up"], env=env, check=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    builds = [call for call in calls if "build" in call]
    assert len(builds) == 1
    assert builds[0][-2:] == ["build", "api"]
    assert calls[-1][-5:] == ["-d", "--wait", "api", "gateway", "fake"]


def test_sample_records_effective_overrides_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record actual Docker limits even when requested Compose config differs."""
    inspected = [
        {
            "Id": "synthetic-id",
            "Name": "/gateway",
            "Image": "sha256:synthetic",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "preloop-capacity",
                    "com.docker.compose.service": "gateway",
                },
                "Env": [
                    "DATABASE_POOL_SIZE=12",
                    "DATABASE_MAX_OVERFLOW=4",
                    "PRELOOP_SERVICE_ROLE=gateway",
                    "SECRET_KEY=must-not-record",
                    "DATABASE_URL=private-database",
                ],
            },
            "HostConfig": {
                "NanoCpus": 2000000000,
                "Memory": 1073741824,
                "CpuQuota": 0,
                "CpuPeriod": 0,
                "MemorySwap": 2147483648,
            },
            "NetworkSettings": {"Ports": {}},
        }
    ]

    def command(arguments: list[str], timeout: float = 10) -> str:
        if arguments[-2:] == ["ps", "-aq"]:
            return "synthetic-id"
        if arguments[:2] == ["docker", "inspect"]:
            if "--format" in arguments:
                return '{"Running":true}'
            return json.dumps(inspected)
        if arguments[:2] == ["docker", "stats"]:
            return '{"CPUPerc":"50.0%"}'
        return "idle|Client|1"

    monkeypatch.setattr(collect, "command", command)
    result = collect.sample()
    actual = result["runtime_settings"][0]
    assert actual["environment"]["DATABASE_POOL_SIZE"] == "12"
    assert actual["nano_cpus"] == 2000000000
    assert actual["image_id"] == "sha256:synthetic"
    assert "must-not-record" not in json.dumps(result)
    assert "private-database" not in json.dumps(result)
    inspected[0]["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
    with pytest.raises(ValueError, match="Unexpected Compose project"):
        collect.sample()
