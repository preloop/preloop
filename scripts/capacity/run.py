#!/usr/bin/env python3
"""Bounded authenticated capacity workloads for the disposable Compose stack."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import secrets
import time
from collections import Counter
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


class WorkloadError(Exception):
    """A stable, payload-free workload failure category."""


def error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return str(exc) if isinstance(exc, WorkloadError) else type(exc).__name__


def mcp_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """Keep MCP traffic independent of ambient proxies and remote redirects."""
    return httpx.AsyncClient(
        **{**kwargs, "trust_env": False, "follow_redirects": False}
    )


def local_url(value: str) -> str:
    """Keep the driver restricted to this stack or local smoke servers."""
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname
        not in {"api", "gateway", "fake", "nats", "127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Capacity endpoints must be local HTTP stack services")
    return value.rstrip("/")


def percentile(values: list[float], quantile: float) -> float | None:
    """Nearest-rank percentile; return no value for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 3)


def summarize(samples: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    """Summarize attempts, including failed-request latency separately."""
    operations = {}
    for operation in sorted({sample["operation"] for sample in samples}):
        selected = [sample for sample in samples if sample["operation"] == operation]
        success = [
            sample["ms"]
            for sample in selected
            if sample["ok"] and not sample.get("cancelled")
        ]
        operations[operation] = {
            "attempts": len(selected),
            "successes": len(success),
            "errors": sum(not sample["ok"] for sample in selected),
            "errors_by_kind": dict(
                Counter(sample["error"] for sample in selected if not sample["ok"])
            ),
            "cancelled_streams": sum(
                sample.get("cancelled", False) for sample in selected
            ),
            "attempts_per_second": round(len(selected) / elapsed, 3),
            "successes_per_second": round(len(success) / elapsed, 3),
            "error_rate": sum(not sample["ok"] for sample in selected) / len(selected),
            "p50_ms": percentile(success, 0.50),
            "p95_ms": percentile(success, 0.95),
            "p99_ms": percentile(success, 0.99),
            "all_attempts_p99_ms": percentile([s["ms"] for s in selected], 0.99),
            "ttft_p95_ms": percentile(
                [s["ttft_ms"] for s in selected if s.get("ttft_ms") is not None], 0.95
            ),
        }
    return {"elapsed_seconds": round(elapsed, 3), "operations": operations}


def breaches(
    summary: dict[str, Any], max_error_rate: float, max_p95_ms: float
) -> list[str]:
    failures = []
    for name, stats in summary["operations"].items():
        if stats["error_rate"] > max_error_rate:
            failures.append(f"{name}:error_rate")
        if stats["p95_ms"] is not None and stats["p95_ms"] > max_p95_ms:
            failures.append(f"{name}:p95")
    return failures


class Recorder:
    """Bound in-memory samples per stage while preserving every attempt on disk."""

    def __init__(
        self, output: TextIO, stage: int, maximum: int, phase: str = "load"
    ) -> None:
        self.output = output
        self.stage = stage
        self.phase = phase
        self.maximum = maximum
        self.started = 0
        self.samples: list[dict[str, Any]] = []

    def reserve(self) -> bool:
        if self.started >= self.maximum:
            return False
        self.started += 1
        return True

    def record(
        self, operation: str, started: float, error: str | None = None, **extra: Any
    ) -> None:
        sample = {
            "stage": self.stage,
            "phase": self.phase,
            "timestamp": time.time(),
            "operation": operation,
            "ms": round((time.perf_counter() - started) * 1000, 3),
            "ok": error is None,
            "error": error,
            **extra,
        }
        self.samples.append(sample)
        self.output.write(json.dumps(sample) + "\n")


async def checked(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> Any:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


async def bootstrap(
    client: httpx.AsyncClient, api: str, fake: str
) -> tuple[str, dict[str, str], str]:
    """Create a new isolated account using the same API as an actual client."""
    marker = await checked(client, "GET", fake + "/capacity/identity")
    if marker.get("fixture") != "preloop-capacity-v1":
        raise ValueError("Deterministic fixture identity check failed")
    username = "capacity" + secrets.token_hex(6)
    login = {"username": username, "password": secrets.token_urlsafe(24)}
    user = await checked(
        client,
        "POST",
        api + "/api/v1/auth/register",
        json={
            **login,
            "email": username + "@example.com",
            "full_name": "Capacity Fixture",
        },
    )
    auth = await checked(client, "POST", api + "/api/v1/auth/token/json", json=login)
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    key = await checked(
        client,
        "POST",
        api + "/api/v1/auth/api-keys",
        headers=headers,
        json={"name": "capacity-driver"},
    )
    await checked(
        client,
        "POST",
        api + "/api/v1/ai-models",
        headers=headers,
        json={
            "name": "Capacity fixture",
            "provider_name": "openai",
            "model_identifier": "capacity-model",
            "api_endpoint": fake + "/v1",
            "api_key": "example-key-capacity-fixture",
            "is_default": True,
            "meta_data": {
                "gateway": {"enabled": True, "model_alias": "openai/capacity-model"}
            },
        },
    )
    server = await checked(
        client,
        "POST",
        api + "/api/v1/mcp-servers",
        headers=headers,
        json={
            "name": "capacity-fixture",
            "url": fake + "/tools/mcp",
            "transport": "http-streaming",
            "auth_type": "none",
        },
    )
    if server.get("status") != "active":
        raise RuntimeError("Fixture MCP discovery failed; inspect local API logs")
    return key["key"], login, user["account_id"]


async def model_call(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], cancel: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    first = None
    done = False
    content = False
    async with client.stream(
        "POST",
        url + "/openai/v1/chat/completions",
        headers=headers,
        json={
            "model": "openai/capacity-model",
            "messages": [
                {"role": "user", "content": "Return the deterministic fixture reply."}
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 64,
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if "error" in chunk:
                raise WorkloadError("upstream_stream_error")
            if any(
                choice.get("delta", {}).get("content")
                for choice in chunk.get("choices", [])
            ):
                content = True
                if first is None:
                    first = round((time.perf_counter() - started) * 1000, 3)
                if cancel:
                    return {"ttft_ms": first, "cancelled": True}
    if not done or not content:
        raise WorkloadError("incomplete_stream")
    return {"ttft_ms": first, "cancelled": False}


async def actor(
    index: int, args: argparse.Namespace, token: str, recorder: Recorder, stop: float
) -> None:
    headers = {
        "Authorization": "Bearer " + token,
        "X-Preloop-Session-Id": f"capacity-{recorder.stage}-{index}",
    }
    client = Client(
        StreamableHttpTransport(
            args.api + "/mcp/v1", headers=headers, httpx_client_factory=mcp_http_client
        ),
        timeout=args.timeout,
    )
    setup = time.perf_counter()
    connected = False
    if not recorder.reserve():
        return
    try:
        async with (
            client,
            httpx.AsyncClient(timeout=args.timeout, trust_env=False) as http,
        ):
            listed = await client.list_tools()
            tool = next(
                (item.name for item in listed if item.name.endswith("capacity_echo")),
                None,
            )
            if tool is None:
                raise WorkloadError("fixture_tool_not_discovered")
            recorder.record("mcp_connect", setup)
            connected = True
            sequence = 0
            while time.monotonic() < stop:
                for operation in ("mcp_tool", "gateway_stream"):
                    if time.monotonic() >= stop or not recorder.reserve():
                        return
                    started = time.perf_counter()
                    extra: dict[str, Any] = {}
                    error = None
                    try:
                        async with asyncio.timeout(args.timeout):
                            if operation == "mcp_tool":
                                result = await client.call_tool(
                                    tool,
                                    {
                                        "sequence": sequence,
                                        "delay_ms": args.tool_delay_ms,
                                    },
                                )
                                if result.is_error:
                                    raise WorkloadError("mcp_tool_error")
                            else:
                                extra = await model_call(
                                    http,
                                    args.gateway,
                                    headers,
                                    bool(
                                        args.cancel_every
                                        and sequence % args.cancel_every == 0
                                    ),
                                )
                    except Exception as exc:
                        error = error_kind(exc)
                    recorder.record(operation, started, error, **extra)
                sequence += 1
                if args.think_ms:
                    await asyncio.sleep(args.think_ms / 1000)
    except Exception as exc:
        recorder.record(
            "mcp_teardown" if connected else "mcp_connect", setup, error_kind(exc)
        )


async def observe(
    args: argparse.Namespace,
    token: str,
    login: dict[str, str],
    recorder: Recorder,
    stop: asyncio.Event,
    output: TextIO,
) -> None:
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
        tick = 0
        while not stop.is_set() and recorder.started < recorder.maximum:
            timestamp = time.time()
            snapshots: dict[str, Any] = {
                "stage": recorder.stage,
                "phase": recorder.phase,
                "timestamp": timestamp,
            }
            for name, url in (("api", args.api), ("gateway", args.gateway)):
                started = time.perf_counter()
                if not recorder.reserve():
                    break
                error = None
                try:
                    async with asyncio.timeout(args.timeout):
                        await checked(client, "GET", url + "/api/v1/ping")
                except Exception as exc:
                    error = error_kind(exc)
                recorder.record(name + "_ping", started, error)
                try:
                    snapshots[name] = await checked(
                        client, "GET", url + "/api/v1/health"
                    )
                except Exception as exc:
                    snapshots[name] = {"error": type(exc).__name__}
            if recorder.reserve():
                started = time.perf_counter()
                error = None
                try:
                    if tick % 5 == 0:
                        await checked(
                            client,
                            "POST",
                            args.api + "/api/v1/auth/token/json",
                            json=login,
                        )
                    else:
                        await checked(
                            client,
                            "GET",
                            args.api + "/api/v1/auth/users/me",
                            headers=headers,
                        )
                except Exception as exc:
                    error = error_kind(exc)
                recorder.record(
                    "signin" if tick % 5 == 0 else "control", started, error
                )
            try:
                snapshots["nats"] = await checked(client, "GET", args.nats + "/varz")
            except Exception as exc:
                snapshots["nats"] = {"error": type(exc).__name__}
            output.write(json.dumps(snapshots) + "\n")
            output.flush()
            tick += 1
            before = time.monotonic()
            try:
                await asyncio.wait_for(stop.wait(), args.sample_seconds)
            except asyncio.TimeoutError:
                output.write(
                    json.dumps(
                        {
                            "stage": recorder.stage,
                            "phase": recorder.phase,
                            "timestamp": time.time(),
                            "driver_loop_lag_ms": max(
                                0,
                                (time.monotonic() - before - args.sample_seconds)
                                * 1000,
                            ),
                        }
                    )
                    + "\n"
                )


async def run(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {
        **vars(args),
        "output": str(args.output),
        "revision": os.getenv("CAPACITY_REVISION", "unknown"),
        "platform": platform.platform(),
        "telemetry_disabled": True,
        "workload": "closed-loop simulated agents, one proxied tool then one streaming gateway call",
    }
    (args.output / "config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    report: dict[str, Any] = {
        "stages": [],
        "first_failure_concurrency": None,
        "status": "starting",
    }
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            token, login, account_id = await bootstrap(client, args.api, args.fake)
            metadata["fixture"] = await checked(
                client, "GET", args.fake + "/capacity/identity"
            )
            (args.output / "config.json").write_text(
                json.dumps(metadata, indent=2) + "\n"
            )
        fixture = None
        if args.logs_per_second:
            from scripts.capacity.logs import LogFixture

            fixture = await asyncio.to_thread(
                LogFixture, account_id, args.log_executions
            )
        with (
            (args.output / "requests.jsonl").open("w") as requests,
            (args.output / "telemetry.jsonl").open("w") as telemetry,
        ):
            for stage, concurrency in enumerate(args.levels):
                recorder = Recorder(requests, stage, args.max_requests)
                started = time.monotonic()
                stop = asyncio.Event()
                monitor = asyncio.create_task(
                    observe(args, token, login, recorder, stop, telemetry)
                )
                publisher = (
                    asyncio.create_task(
                        fixture.publish(
                            account_id,
                            stage,
                            args.logs_per_second,
                            args.seconds,
                            args.max_logs,
                        )
                    )
                    if fixture
                    else None
                )
                try:
                    await asyncio.gather(
                        *(
                            actor(i, args, token, recorder, started + args.seconds)
                            for i in range(concurrency)
                        )
                    )
                finally:
                    stop.set()
                    await monitor
                log_result = await publisher if publisher else None
                summary = {
                    "concurrency": concurrency,
                    **summarize(recorder.samples, time.monotonic() - started),
                    "request_cap_reached": recorder.started >= args.max_requests,
                }
                # Recovery has its own recorder so cooldown does not dilute load latency.
                recovery = Recorder(requests, stage, args.max_requests, "recovery")
                recovery_started = time.monotonic()
                recovery_stop = asyncio.Event()
                recovery_monitor = asyncio.create_task(
                    observe(args, token, login, recovery, recovery_stop, telemetry)
                )
                persisted = {}
                try:
                    while time.monotonic() - recovery_started < args.cooldown_seconds:
                        if fixture:
                            persisted = await asyncio.to_thread(
                                fixture.reconcile, stage
                            )
                            telemetry.write(
                                json.dumps(
                                    {
                                        "stage": stage,
                                        "timestamp": time.time(),
                                        "recovery_log_counts": persisted,
                                    }
                                )
                                + "\n"
                            )
                        await asyncio.sleep(min(1, args.cooldown_seconds))
                finally:
                    recovery_stop.set()
                    await recovery_monitor
                if fixture:
                    persisted = await asyncio.to_thread(fixture.reconcile, stage)
                    log_result.update(
                        persisted.get(
                            str(stage),
                            {"unique_persisted": 0, "rows": 0, "duplicates": 0},
                        )
                    )
                    log_result["missing_after_recovery"] = (
                        log_result["submitted"] - log_result["unique_persisted"]
                    )
                    summary["logs"] = log_result
                summary["recovery"] = summarize(
                    recovery.samples, max(0.001, time.monotonic() - recovery_started)
                )
                failures = breaches(summary, args.max_error_rate, args.max_p95_ms)
                if args.cooldown_seconds > 0:
                    failures.extend(
                        "recovery:" + breach
                        for breach in breaches(
                            summary["recovery"], args.max_error_rate, args.max_p95_ms
                        )
                    )
                    for required_probe in ("signin", "api_ping", "gateway_ping"):
                        if (
                            summary["recovery"]["operations"]
                            .get(required_probe, {})
                            .get("successes", 0)
                            == 0
                        ):
                            failures.append(
                                "recovery:" + required_probe + ":no_successful_samples"
                            )
                if log_result and log_result["missing_after_recovery"]:
                    failures.append("logs:missing_after_recovery")
                for required in ("mcp_tool", "gateway_stream"):
                    if summary["operations"].get(required, {}).get("successes", 0) == 0:
                        failures.append(required + ":no_successful_samples")
                summary["threshold_breaches"] = failures
                invalid = any(
                    kind
                    in {
                        "http_401",
                        "http_403",
                        "http_404",
                        "http_422",
                        "fixture_tool_not_discovered",
                    }
                    for stats in summary["operations"].values()
                    for kind in stats["errors_by_kind"]
                )
                summary["classification"] = (
                    "setup_or_configuration_failure"
                    if invalid
                    else "fixture_fault_injection"
                    if metadata["fixture"].get("error_every")
                    else "slo_breach"
                    if failures
                    else "within_thresholds"
                )
                report["stages"].append(summary)
                print(json.dumps(summary), flush=True)
                requests.flush()
                if failures and report["first_failure_concurrency"] is None:
                    report["first_failure_concurrency"] = concurrency
                    report["first_failure_kind"] = summary["classification"]
                if failures and not args.continue_after_failure:
                    break

        report["status"] = (
            "threshold_exceeded" if report["first_failure_concurrency"] else "completed"
        )
    except Exception as exc:
        report["status"] = "harness_error"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return 2 if report["first_failure_concurrency"] else 0


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--levels",
        default="1,2,4,8,16,32,64,128",
        help="Concurrency steps; repeat a value for repeated measurements",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=30,
        help="Seconds per step; use one level for a soak",
    )
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--tool-delay-ms", type=int, default=100)
    parser.add_argument("--think-ms", type=int, default=0)
    parser.add_argument("--cancel-every", type=int, default=0)
    parser.add_argument("--sample-seconds", type=float, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument(
        "--logs-per-second",
        type=float,
        default=0,
        help="Optional paced Core NATS log lane",
    )
    parser.add_argument("--log-executions", type=int, default=8)
    parser.add_argument("--max-logs", type=int, default=100_000)
    parser.add_argument("--max-requests", type=int, default=100_000)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=5000)
    parser.add_argument("--continue-after-failure", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/artifacts") / time.strftime("%Y%m%d-%H%M%S"),
    )
    for name, default in (
        ("api", "http://api:8000"),
        ("gateway", "http://gateway:8000"),
        ("fake", "http://fake:9000"),
        ("nats", "http://nats:8222"),
    ):
        parser.add_argument("--" + name, type=local_url, default=default)
    args = parser.parse_args()
    args.levels = [int(level) for level in args.levels.split(",")]
    if (
        not args.levels
        or len(args.levels) > 32
        or not all(1 <= n <= 512 for n in args.levels)
    ):
        parser.error("Use at most 32 concurrency levels, each between 1 and 512")
    if (
        not 0 < args.seconds <= 3600
        or not 0 < args.timeout <= 120
        or not 0 <= args.max_error_rate <= 1
        or not 0 < args.max_p95_ms
    ):
        parser.error("Invalid duration, timeout or threshold")
    if (
        not 1 <= args.max_requests <= 1_000_000
        or not 0 <= args.tool_delay_ms <= 60000
        or not 0 <= args.think_ms <= 60000
        or args.cancel_every < 0
        or args.sample_seconds < 0.1
        or not 0 <= args.cooldown_seconds <= 60
    ):
        parser.error("Invalid workload bound")
    if (
        not 0 <= args.logs_per_second <= 10000
        or not 1 <= args.log_executions <= 64
        or not 1 <= args.max_logs <= 1_000_000
    ):
        parser.error("Invalid log workload bound")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(arguments())))
