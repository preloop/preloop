"""Health check endpoints."""

import logging
from datetime import UTC, datetime
from typing import Any, Dict

from fastapi import APIRouter
from sqlalchemy import text

from preloop.models.db.session import get_health_engine

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/ping")
async def ping() -> Dict[str, str]:
    """Simple liveness check - no database, no external dependencies.

    Declared ``async`` on purpose: a plain ``def`` endpoint is executed in
    Starlette's bounded anyio worker threadpool (40 threads by default). During
    a database stall, sync endpoints block those threads waiting on pool
    checkout, so even this DB-free probe would queue behind them and the
    liveness probe would time out and get the pod SIGKILLed. Running on the
    event loop keeps liveness answerable no matter how saturated the DB is.

    Returns:
        Simple pong response
    """
    return {"status": "pong", "timestamp": datetime.now(UTC).isoformat()}


def _pool_health() -> Dict[str, Any]:
    """Summarize connection pool usage for the readiness payload.

    Reports per-engine checkout counts and a saturation flag so an operator
    (or an alert on the readiness body) can tell "the pool is full" apart from
    "the database is down", which looked identical from the outside during the
    2026-09-03 incident.

    Returns:
        A dict with a ``saturated`` flag and one entry per initialized engine.
        Never raises: health reporting must not be able to fail health.
    """
    try:
        from preloop.services.db_pool_monitor import collect_pool_stats

        engines = collect_pool_stats()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Pool health snapshot failed", exc_info=True)
        return {"status": f"error: {type(exc).__name__}"}

    saturated = any(
        snapshot["ceiling"] > 0 and snapshot["checked_out"] >= snapshot["ceiling"]
        for snapshot in engines
    )
    return {
        "saturated": saturated,
        "engines": [
            {
                "engine": snapshot["engine"],
                "checked_out": snapshot["checked_out"],
                "ceiling": snapshot["ceiling"],
                "overflow_in_use": snapshot["overflow_in_use"],
            }
            for snapshot in engines
        ],
    }


def _usage_queue_health() -> Dict[str, Any]:
    """Summarize the API usage writer queue for the readiness payload.

    Returns:
        Queue depth and counters, or an error marker. Never raises.
    """
    try:
        from preloop.services.api_usage_recorder import get_api_usage_recorder

        return get_api_usage_recorder().stats()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Usage queue snapshot failed", exc_info=True)
        return {"status": f"error: {type(exc).__name__}"}


@router.get("/health")
def health_check() -> Dict[str, Any]:
    """Health check endpoint with database and MCP server status.

    Uses a dedicated single-connection engine rather than the shared request
    pool: readiness should reflect "can I reach Postgres", not "is the request
    pool momentarily full". Consuming a pooled connection here made the probe
    fail during saturation, which amplified load spikes into pod restarts.

    Returns:
        Dictionary with health status including:
        - status: Overall health status (healthy/unhealthy)
        - database: Database connection status
        - db_pool: Per-engine connection pool usage and saturation
        - api_usage_queue: Usage-logging queue depth and drop counters
        - mcp_server: MCP server availability
        - upstream_connections: Number of active upstream MCP connections
        - timestamp: Current timestamp
    """
    health_status: Dict[str, Any] = {
        "status": "healthy",
        "timestamp": datetime.now(UTC).isoformat(),
        "database": "unknown",
        "mcp_server": "unknown",
        "upstream_connections": 0,
    }

    # Verify database connection via the dedicated health engine
    try:
        with get_health_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception as e:
        logger.error("Database health check failed", exc_info=True)
        health_status["database"] = f"error: {type(e).__name__}"
        health_status["status"] = "unhealthy"

    # Report pool saturation without failing readiness on it. During the
    # 2026-09-03 incident the pool was full while Postgres was idle; taking
    # pods out of the load balancer for that would have concentrated the same
    # traffic on fewer pods. So this is visible, not fatal.
    health_status["db_pool"] = _pool_health()
    health_status["api_usage_queue"] = _usage_queue_health()

    # Check MCP server availability
    try:
        from preloop.services.mcp_http import get_mcp_lifespan_manager

        mcp_lifespan = get_mcp_lifespan_manager()
        if mcp_lifespan is not None:
            health_status["mcp_server"] = "available"
        else:
            health_status["mcp_server"] = "not_initialized"
    except Exception as e:
        logger.error("MCP server health check failed", exc_info=True)
        health_status["mcp_server"] = f"error: {type(e).__name__}"

    # Check upstream MCP connections
    try:
        from preloop.services.mcp_client_pool import get_mcp_client_pool

        client_pool = get_mcp_client_pool()
        active_servers = client_pool.get_active_servers()
        health_status["upstream_connections"] = len(active_servers)
        if active_servers:
            health_status["upstream_servers"] = active_servers
    except Exception as e:
        logger.error("Upstream MCP health check failed", exc_info=True)
        health_status["upstream_connections"] = f"error: {type(e).__name__}"

    return health_status
