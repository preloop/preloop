"""Health check endpoints."""

import logging
from datetime import UTC, datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session as get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/ping")
def ping() -> Dict[str, str]:
    """Simple liveness check - no database, no external dependencies.

    Use this for Kubernetes liveness probes to avoid killing pods
    due to temporary database issues.

    Returns:
        Simple pong response
    """
    return {"status": "pong", "timestamp": datetime.now(UTC).isoformat()}


@router.get("/health")
def health_check(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Health check endpoint with database and MCP server status.

    Returns:
        Dictionary with health status including:
        - status: Overall health status (healthy/unhealthy)
        - database: Database connection status
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

    # Verify database connection
    try:
        db.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception as e:
        logger.error("Database health check failed", exc_info=True)
        health_status["database"] = f"error: {type(e).__name__}"
        health_status["status"] = "unhealthy"

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
