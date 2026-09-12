#!/usr/bin/env python3
"""Record Docker resource and PostgreSQL wait evidence from the local fixture."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
COMPOSE = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yaml"))]


def command(arguments: list[str], timeout: float = 10) -> str:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def sample() -> dict[str, Any]:
    """Only inspect containers belonging to the dedicated Compose project."""
    ids = command([*COMPOSE, "ps", "-aq"]).split()
    if not ids:
        return {"error": "no_capacity_containers"}
    runtime_settings = []
    for row in json.loads(command(["docker", "inspect", *ids])):
        if (
            row.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
            != "preloop-capacity"
        ):
            raise ValueError("Unexpected Compose project")
        config = row.get("Config", {})
        host = row.get("HostConfig", {})
        # Whitelist performance knobs; never archive arbitrary container env.
        allowed = {
            "DATABASE_POOL_SIZE",
            "DATABASE_MAX_OVERFLOW",
            "DATABASE_POOL_TIMEOUT",
            "PRELOOP_SERVICE_ROLE",
            "FAKE_MODEL_DELAY_MS",
            "FAKE_TOKEN_DELAY_MS",
            "FAKE_OUTPUT_TOKENS",
            "FAKE_ERROR_EVERY",
            "PRELOOP_DISABLE_TELEMETRY",
        }
        environment = {}
        for entry in config.get("Env", []):
            key, _, value = entry.partition("=")
            if key in allowed:
                environment[key] = value
        runtime_settings.append(
            {
                "container_id": row.get("Id"),
                "service": config.get("Labels", {}).get("com.docker.compose.service"),
                "image_id": row.get("Image"),
                "nano_cpus": host.get("NanoCpus"),
                "cpu_quota": host.get("CpuQuota"),
                "cpu_period": host.get("CpuPeriod"),
                "memory_bytes": host.get("Memory"),
                "memory_swap_bytes": host.get("MemorySwap"),
                "ports": row.get("NetworkSettings", {}).get("Ports"),
                "environment": environment,
            }
        )
    stats = [
        json.loads(line)
        for line in command(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids]
        ).splitlines()
    ]
    states = [
        json.loads(line)
        for line in command(
            ["docker", "inspect", "--format", "{{json .State}}", *ids]
        ).splitlines()
    ]
    pg = command(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "capacity",
            "-d",
            "capacity",
            "-At",
            "-c",
            "SELECT COALESCE(state, 'background'), COALESCE(wait_event_type, 'none'), count(*) FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1, 2;",
        ]
    )
    return {
        "containers": stats,
        "states": states,
        "postgres_activity": pg.splitlines(),
        "runtime_settings": runtime_settings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 0 < args.seconds <= 86400 or not 1 <= args.interval <= 60:
        parser.error("Use positive bounded collection duration and interval")
    context = json.loads(command(["docker", "context", "inspect"]))[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("unix://") or os.getenv(
        "DOCKER_HOST"
    ):
        parser.error("Collector requires a local Unix-socket Docker context")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with args.output.open("x") as output:
        output.write(
            json.dumps(
                {
                    "type": "host",
                    "platform": platform.platform(),
                    "logical_cpus": os.cpu_count(),
                    "revision": command(["git", "rev-parse", "HEAD"]).strip(),
                }
            )
            + "\n"
        )
        while time.monotonic() - started < args.seconds:
            before = time.monotonic()
            try:
                row = sample()
            except Exception as exc:
                row = {"error": type(exc).__name__}
            output.write(json.dumps({"timestamp": time.time(), **row}) + "\n")
            output.flush()
            time.sleep(max(0, args.interval - (time.monotonic() - before)))


if __name__ == "__main__":
    main()
