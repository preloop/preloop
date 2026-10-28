"""Stream the fixed Overview read set without serial browser round trips."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from urllib.parse import urlencode
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from preloop.api.auth.jwt import get_current_active_user

router = APIRouter()
logger = logging.getLogger(__name__)
MAX_CONCURRENT_READS = 4
MAX_PENDING_READS = 64
READ_DEADLINE_SECONDS = 30.0


def overview_reads(
    start_date: str,
    prior_start: str,
    prior_end: str,
    now: datetime,
    phase: Literal["full", "gateway"] = "full",
) -> list[str]:
    """Build only the reads used by Overview; callers cannot supply paths."""

    def url(path: str, **params: object) -> str:
        return (
            "/api/v1/" + path + ("?" + urlencode(params, doseq=True) if params else "")
        )

    def iso(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    summary = "account/gateway-usage/summary"
    reads = [
        url(summary, start_date=start_date, include_breakdown="false"),
        url(
            summary,
            start_date=prior_start,
            end_date=prior_end,
            include_breakdown="false",
        ),
        url("account/gateway-usage/rate-limits", start_date=start_date),
        url("runtime-sessions", start_date=start_date, status="all", limit=20),
        url("agents", status="all", limit=100),
        url("approval-requests", limit=100, status="pending"),
        url("approval-requests", limit=100),
        url("flows/summary"),
        url("tools/summary"),
        url("ai-models"),
        url("fleet/device-fleet"),
        url("users", skip=0, limit=100),
        url("audit-logs/overview", start_date=start_date, exceptions_limit=12),
        url("flows/executions", limit=100),
        url(
            "account/gateway-usage/search",
            start_date=start_date,
            limit=100,
            include_total="false",
        ),
        url("issues-count"),
        url("trackers"),
        url("auth/api-keys/count"),
        url("mcp-servers"),
        url("ai-models/overview", start_date=start_date),
        url(
            summary,
            start_date=start_date,
            include_breakdown="true",
            breakdown=["models", "flows", "sessions", "tools", "days", "agents"],
        ),
        url("budget/policies"),
        url("account/gateway-usage/search", limit=12, include_total="false"),
        url("flows/executions", limit=25, status="FAILED"),
        url(
            "runtime-sessions",
            start_date=iso(now - timedelta(days=7)),
            status="all",
            limit=100,
        ),
        url("policies/notices/summary", days=7),
        url("attention/extension-items", include_delivery_reports="true"),
        url(
            summary,
            start_date=iso(now - timedelta(days=30)),
            include_breakdown="true",
            breakdown=["models"],
        ),
        url("billing/cost/pricing-overrides", active_only="true"),
        url("attention/spend-outliers"),
        url("attention/dismissals"),
        url("mcp-findings"),
    ]

    if phase == "gateway":
        return reads[:3] + [
            url(
                "account/gateway-usage/search",
                start_date=start_date,
                limit=25,
                include_total="false",
            )
        ]
    return reads


@router.get(
    "/overview/bootstrap",
    dependencies=[Depends(get_current_active_user)],
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def overview_bootstrap(
    request: Request,
    start_date: datetime,
    prior_start: datetime,
    prior_end: datetime,
    now: datetime,
    phase: Literal["full", "gateway"] = "full",
) -> StreamingResponse:
    """Stream independently authorized results, with no cross-request cache.

    Every read traverses its ordinary route and dependency chain, including
    tenant filters, permissions and licensed capabilities. The manifest is
    fixed, read-only and bounded. No URL or method is accepted from a caller.
    """

    def iso(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    paths = overview_reads(
        iso(start_date), iso(prior_start), iso(prior_end), now, phase
    )
    return stream_reads(request, paths)


@router.get(
    "/overview/shell",
    dependencies=[Depends(get_current_active_user)],
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def overview_shell(request: Request) -> StreamingResponse:
    """Read the Overview shell in parallel without cross-user caching."""
    paths = [
        "features",
        "version/status",
        "licence/status",
        "auth/users/me",
        "billing/nudges",
        "approval-bypasses/status",
        "account/kill-switch/status",
        "approval-requests?status=pending",
        "account/runner-compliance/me",
        "me/memberships",
        "flows/executions?limit=10&status=PENDING&status=STARTING&status=INITIALIZING&status=RUNNING",
    ]
    return stream_reads(request, ["/api/v1/" + path for path in paths])


def stream_reads(request: Request, paths: list[str]) -> StreamingResponse:
    """Dispatch a server-owned manifest through ordinary route dependencies."""
    # Forward authentication, never hop-by-hop or caller-controlled routing.
    headers = {
        name: request.headers[name]
        for name in ("authorization", "cookie")
        if name in request.headers
    }

    async def stream() -> AsyncIterator[str]:
        transport = httpx.ASGITransport(
            app=request.app,
            raise_app_exceptions=False,
            client=(request.client.host, request.client.port)
            if request.client
            else ("127.0.0.1", 0),
        )
        semaphore = getattr(request.app.state, "overview_read_slots", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_READS)
            request.app.state.overview_read_slots = semaphore
        deadline = asyncio.get_running_loop().time() + READ_DEADLINE_SECONDS
        async with httpx.AsyncClient(
            transport=transport, base_url=str(request.base_url), headers=headers
        ) as client:

            def unavailable(path: str) -> dict[str, object]:
                return {
                    "url": path,
                    "status": 503,
                    "body": '{"detail":"Overview input unavailable"}',
                    "headers": {"content-type": "application/json"},
                }

            async def read(path: str) -> dict[str, object]:
                pending = getattr(request.app.state, "overview_pending_reads", 0)
                if pending >= MAX_PENDING_READS:
                    return unavailable(path)
                request.app.state.overview_pending_reads = pending + 1
                try:
                    # Queue time counts too: a saturated worker must not keep
                    # browser loads and authentication sessions alive forever.
                    async with asyncio.timeout_at(deadline):
                        async with semaphore:
                            response = await client.get(path)
                    return {
                        "url": path,
                        "status": response.status_code,
                        "body": response.text,
                        "headers": {
                            name: response.headers[name]
                            for name in (
                                "content-type",
                                "retry-after",
                                "www-authenticate",
                            )
                            if name in response.headers
                        },
                    }
                except (httpx.HTTPError, TimeoutError):
                    return unavailable(path)
                except Exception:
                    logger.exception("Overview input failed")
                    return unavailable(path)
                finally:
                    request.app.state.overview_pending_reads -= 1

            remaining = iter(paths)
            completed: asyncio.Queue[dict[str, object]] = asyncio.Queue(
                maxsize=MAX_CONCURRENT_READS
            )

            async def worker() -> None:
                for path in remaining:
                    await completed.put(await read(path))

            # Lazy admission keeps simultaneous page loads from enqueuing
            # their entire manifests ahead of other users.
            tasks = [
                asyncio.create_task(worker())
                for _ in range(min(MAX_CONCURRENT_READS, len(paths)))
            ]
            try:
                yield json.dumps({"paths": paths}) + "\n"
                for _ in paths:
                    yield json.dumps(await completed.get()) + "\n"
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
