"""FastAPI application for Preloop.

This FastAPI application provides HTTP endpoints for authentication and management
of issue tracking systems.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any
from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from pyinstrument import Profiler
from pyinstrument.renderers import SpeedscopeRenderer
from starlette.middleware.base import BaseHTTPMiddleware

from preloop import __version__
from fastapi.encoders import jsonable_encoder
from preloop.api.auth import auth_router, get_current_active_user
from preloop.api.endpoints import (
    account,
    agent_control,
    agent_permission,
    anthropic_gateway,
    audio,
    approval_bypass,
    approval_requests,
    comments,
    cost,
    features,
    gemini_gateway,
    health,
    issues,
    mcp_servers,
    notification_preferences,
    organizations,
    policies,
    projects,
    public_approval,
    roles,
    search as search_router,
    security_screen,
    session_optimization,
    tools,
    trackers,
    usage_import,
    version,
    embedding as embedding_router,
    webhooks,
    flows,
    runners,
    ai_models,
    openai_gateway,
    websockets,
)

# Enterprise endpoints (impersonation, issue_compliance, issue_duplicates, issue_dependencies)
# are now loaded exclusively via the plugin system - see plugins/admin and plugins/analytics

from preloop.services.mcp_http import setup_mcp_routes
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.models.sentry import init_sentry
from preloop.models.db.session import get_db_session, get_session_factory
from preloop.models.db.setup import setup_database
from preloop.models.models.api_usage import ApiUsage
from preloop.sync.services.event_bus import connect_nats, close_nats  # NATS integration


logger = logging.getLogger(__name__)

_api_usage_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="api_usage_",
)


class PyinstrumentMiddleware(BaseHTTPMiddleware):
    """Middleware to profile requests using pyinstrument."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """Process a request and profile it.
        Args:
            request: The request to process.
            call_next: The next middleware to call.
        Returns:
            The response from the next middleware.
        """
        profiling_enabled = os.getenv("PROFILING_ENABLED", "false").lower() == "true"
        if not profiling_enabled or not request.url.path.startswith("/api/v1"):
            return await call_next(request)

        profiler = Profiler()
        start_time = time.time()

        profiler.start()
        response = await call_next(request)
        profiler.stop()

        duration = time.time() - start_time

        # Ensure the profiling directory exists
        output_dir = Path("/tmp/profiling")
        output_dir.mkdir(exist_ok=True)

        # Generate a unique filename
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path_slug = request.url.path.replace("/", "_").strip("_")
        filename_base = f"{timestamp}_{request.method}_{path_slug}"

        # Save HTML report
        html_path = output_dir / f"{filename_base}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(profiler.output_html())

        # Save speedscope report
        speedscope_path = output_dir / f"{filename_base}.speedscope.json"
        renderer = SpeedscopeRenderer()
        with open(speedscope_path, "w", encoding="utf-8") as f:
            f.write(renderer.render(profiler.last_session))

        logger.info(
            f"Profiled request {request.method} {request.url.path} in {duration:.4f}s. "
            f"Reports saved to {html_path} and {speedscope_path}"
        )

        return response


class ApiUsageMiddleware(BaseHTTPMiddleware):
    """Middleware to track API usage."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """Process a request and track API usage.

        Args:
            request: The request to process.
            call_next: The next middleware to call.

        Returns:
            The response from the next middleware.
        """
        # Skip tracking for non-api routes
        path = request.url.path

        if (
            not path.startswith("/api/v1")
            or path.startswith("/api/v1/health")
            or path.startswith("/api/v1/billing/plans")
            or path.startswith("/api/v1/billing/create-checkout-session")
            or path.startswith("/api/v1/billing/webhooks")
            or path.startswith("/api/v1/ai-models/providers/")
        ):
            return await call_next(request)

        start_time = datetime.now(timezone.utc)
        response = await call_next(request)
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        # Log slow requests (> 500ms) for performance monitoring
        if duration > 0.5:
            logger.warning(
                f"[SlowRequest] {request.method} {path} took {duration:.2f}s "
                f"(status: {response.status_code})"
            )

        # Extract tracking information
        method = request.method
        status_code = response.status_code
        action_type = None

        # Determine the action type based on the path and method
        if "/issues" in path:
            if method == "POST":
                action_type = "create_issue"
            elif method == "PUT" or method == "PATCH":
                action_type = "update_issue"
            elif method == "DELETE":
                action_type = "delete_issue"

        # Get user_id from auth token if available
        user_id = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            from preloop.api.auth.jwt import decode_token
            from uuid import UUID

            try:
                token = auth_header.replace("Bearer ", "")
                token_data = decode_token(token)
                # user_id is stored in the "sub" field of the token
                user_id_str = getattr(token_data, "sub", None)
                if user_id_str:
                    user_id = UUID(user_id_str)
            except Exception:
                # Ignore errors in token decoding
                pass

        # Log usage in database on a shared executor to avoid pool starvation
        if user_id and status_code < 500:  # Only log successful API calls

            def log_usage_sync() -> None:
                session_factory = get_session_factory()
                session = session_factory()
                try:
                    usage_entry = ApiUsage(
                        user_id=user_id,
                        endpoint=path,
                        method=method,
                        status_code=status_code,
                        duration=duration,
                        action_type=action_type,
                        timestamp=start_time,
                    )
                    session.add(usage_entry)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    logger.error(f"Error logging API usage: {str(e)}")
                finally:
                    session.close()

            _api_usage_executor.submit(log_usage_sync)

        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup logic
    logger.info("Starting up application and database...")
    service_role = os.getenv("PRELOOP_SERVICE_ROLE", "all").lower()
    is_testing = os.getenv("TESTING") == "true"
    is_api_role = service_role in {"all", "api"}
    is_gateway_role = service_role in {"all", "gateway"}

    # Initialize Sentry if DSN is configured
    init_sentry()

    # Stamp LiteLLM env defaults before any httpx client is cached so
    # provider dashboards do not attribute traffic to LiteLLM.
    from preloop.services.litellm_routing import ensure_preloop_client_identity

    ensure_preloop_client_identity()

    # Register the vendored model-price catalog so gateway cost estimates are
    # deterministic per release (independent of the installed litellm version).
    # load_catalog() already handles missing/corrupt files; this catch is only
    # for an unavailable module so unexpected errors still fail startup.
    try:
        from preloop.services.model_price_catalog import load_catalog

        load_catalog()
    except ImportError:
        logger.exception("Model price catalog load failed; using litellm defaults")

    # Initialize database connection and optionally create tables.
    logger.info("Setting up database connection...")
    try:
        # Check if running in test mode or if INIT_DB is set
        init_db = os.getenv("INIT_DB", "false").lower() == "true"
        if init_db and is_api_role:
            logger.info("Initializing database schema...")
            database_url = os.getenv(
                "DATABASE_URL",
                "postgresql+psycopg://user:password@db:5432/preloop",
            )
            setup_database(database_url)
            logger.info("Database schema initialized.")
        elif init_db:
            logger.info(
                "Skipping database schema initialization for %s role.",
                service_role,
            )
        else:
            logger.info("Skipping database schema initialization (INIT_DB not true).")

        # Check if test data initialization is enabled
        if os.getenv("INIT_TEST_DATA", "false").lower() == "true" and is_api_role:
            logger.info("Initializing test data...")
            # Await the coroutine directly: the CLI wrapper (main) calls
            # asyncio.run(), which raises inside this already-running lifespan
            # loop. Schema setup is the INIT_DB branch's job (or the caller's,
            # e.g. CI's init_db.py); seeding legitimately assumes it happened.
            from scripts.init_test_data import create_test_data  # type: ignore

            await create_test_data()
            logger.info("Test data initialization complete.")
        elif os.getenv("INIT_TEST_DATA", "false").lower() == "true":
            logger.info("Skipping test data initialization for %s role.", service_role)

    except Exception as e:
        logger.error(f"Database setup failed: {e}", exc_info=True)
        raise RuntimeError("Database setup failed") from e

    # Connect to NATS (skip in testing mode)
    if not is_testing and (is_api_role or is_gateway_role):
        logger.info("Connecting to NATS...")
        try:
            await connect_nats()
            app.state.nats_connected = True
            logger.info("NATS connection established.")
        except Exception as e:
            logger.error(f"NATS connection failed: {e}", exc_info=True)
            raise RuntimeError("NATS connection failed") from e

        # Start the NATS consumer for WebSocket broadcasting only on the core API.
    if not is_testing and is_api_role:
        from preloop.services.websocket_manager import manager, nats_consumer
        import asyncio

        # Start the NATS consumer as a background task
        loop = asyncio.get_event_loop()
        app.state.nats_consumer_task = loop.create_task(nats_consumer(manager))
        logger.info("NATS consumer for WebSockets started.")
    else:
        logger.info("Skipping NATS WebSocket consumer for %s role.", service_role)

    # Start the execution monitor for cleaning up stale executions (skip in testing mode)
    execution_monitor = None
    if not is_testing and is_api_role:
        from preloop.services.execution_monitor import get_execution_monitor

        execution_monitor = get_execution_monitor()
        await execution_monitor.start()
        logger.info("Execution monitor started.")
    else:
        logger.info("Skipping execution monitor for %s role.", service_role)

    # Start the optimization-job recovery sweeper (skip in testing mode). It
    # runs one sweep immediately, recovering jobs abandoned by the previous
    # process, then every couple of minutes.
    optimization_job_sweeper = None
    if not is_testing and is_api_role:
        from preloop.services.session_optimization_jobs import (
            get_optimization_job_sweeper,
        )

        optimization_job_sweeper = get_optimization_job_sweeper()
        await optimization_job_sweeper.start()
        logger.info("Optimization job sweeper started.")
    else:
        logger.info("Skipping optimization job sweeper for %s role.", service_role)

    # Recover orphaned flow executions (skip in testing mode)
    recovery_service = None
    if not is_testing and is_api_role:
        from preloop.services.flow_execution_dispatcher import (
            flow_execution_worker_enabled,
        )
        from preloop.services.execution_recovery import get_recovery_service

        # When flow orchestration runs on sync workers, API must not start
        # in-process orchestrators (unsafe with multiple API replicas).
        # Workers re-dispatch stale claims on boot instead.
        skip_recovery = (
            os.getenv("SKIP_EXECUTION_RECOVERY", "false").lower() == "true"
            or flow_execution_worker_enabled()
        )
        if flow_execution_worker_enabled():
            logger.info(
                "Skipping API execution recovery "
                "(FLOW_EXECUTION_WORKER_ENABLED=true; workers own orchestration)"
            )
        elif skip_recovery:
            logger.warning("Skipping execution recovery (SKIP_EXECUTION_RECOVERY=true)")
        else:
            recovery_service = get_recovery_service()
            logger.info("Checking for orphaned flow executions to recover...")
            try:
                # Get a database session for recovery
                db = next(get_db_session())
                try:
                    recovered_count = (
                        await recovery_service.recover_orphaned_executions(db)
                    )
                    if recovered_count > 0:
                        logger.info(
                            f"Recovered {recovered_count} orphaned execution(s)"
                        )
                    else:
                        logger.info("No orphaned executions found")
                finally:
                    db.close()
            except Exception as e:
                logger.error(
                    f"Error recovering orphaned executions: {e}", exc_info=True
                )
                # Don't fail startup - continue anyway
    else:
        logger.info("Skipping execution recovery for %s role.", service_role)

    # Start MCP server lifespan (skip in testing mode)
    mcp_lifespan = None
    if not is_testing and is_api_role:
        from preloop.services.mcp_http import get_mcp_lifespan_manager

        mcp_lifespan = get_mcp_lifespan_manager()
        if mcp_lifespan:
            await mcp_lifespan.__aenter__()
            logger.info("MCP server lifespan started")
        else:
            logger.warning("No MCP lifespan manager available")
    else:
        logger.info("Skipping MCP server for %s role.", service_role)

    # Initialize plugin system (skip in testing mode)
    plugin_manager = None
    if not is_testing and is_api_role:
        from preloop.plugins import get_plugin_manager

        logger.info("Initializing plugin system...")
        plugin_manager = get_plugin_manager()
        await plugin_manager.startup_all()
        logger.info(
            f"Plugin system initialized. "
            f"Registered {len(plugin_manager.list_condition_evaluators())} condition evaluators."
        )
    else:
        logger.info("Skipping plugin system for %s role.", service_role)

    # Register instance and send version check (skip in testing mode)
    if not is_testing and is_api_role:
        from preloop.services.instance_service import register_instance

        try:
            await register_instance()
        except Exception as e:
            logger.warning(f"Instance registration failed: {e}")
            # Don't fail startup - continue anyway
    else:
        logger.info("Skipping instance registration for %s role.", service_role)

    # Repair legacy default approval workflows (skip in testing mode).
    # Older builds created the per-account default workflow with
    # ``approval_type="manual"`` and could leave it without any approver.
    # Both make the workflow unusable: the dialog renders the type as blank,
    # and approval requests routed through it have no one able to act on
    # them. The repair pass is idempotent and safe to run on every boot.
    if not is_testing and is_api_role:
        try:
            from preloop.services.approval_workflow_service import (
                run_default_approval_workflow_startup_repair,
            )

            stats = run_default_approval_workflow_startup_repair()
            if stats["broken"] or stats["missing"]:
                logger.info(
                    "Default approval workflow repair pass complete: "
                    "scanned=%(broken)s, repaired=%(repaired)s, "
                    "missing=%(missing)s, seeded=%(seeded)s" % stats
                )
        except Exception as e:
            logger.warning(
                f"Default approval workflow repair pass failed: {e}",
                exc_info=True,
            )
    else:
        logger.info("Skipping approval workflow repair pass for %s role.", service_role)

    # Encrypt any legacy plaintext tracker credentials at rest (idempotent).
    # Per-row failures are handled inside the backfill; catch only import/DB
    # setup failures here so unexpected bugs still fail startup loudly.
    if not is_testing and is_api_role:
        try:
            from sqlalchemy.exc import SQLAlchemyError

            from preloop.services.tracker_credential_backfill import (
                run_tracker_credential_encryption_backfill,
            )

            tracker_stats = run_tracker_credential_encryption_backfill()
            if (
                tracker_stats["migrated_api_keys"]
                or tracker_stats["migrated_webhook_secrets"]
            ):
                logger.info(
                    "Tracker credential backfill complete: "
                    "scanned=%(scanned)s, api_keys=%(migrated_api_keys)s, "
                    "webhook_secrets=%(migrated_webhook_secrets)s" % tracker_stats
                )
        except (ImportError, SQLAlchemyError) as e:
            logger.warning(
                f"Tracker credential backfill failed: {e}",
                exc_info=True,
            )

    yield

    # Shutdown logic

    # Wait for in-flight flow executions to complete (skip in testing mode)
    if not is_testing and recovery_service:
        logger.info("Waiting for in-flight flow executions to complete...")
        try:
            # Wait up to 5 minutes for recovery tasks to complete
            await recovery_service.wait_for_completion(timeout=300)
            logger.info("All in-flight executions completed or timed out.")
        except Exception as e:
            logger.error(
                f"Error waiting for executions to complete: {e}", exc_info=True
            )
    else:
        logger.info("Skipping execution wait for %s role.", service_role)

    # Stop version checker (skip in testing mode)
    if not is_testing and is_api_role:
        from preloop.services.instance_service import stop_version_checker

        stop_version_checker()

    # Shutdown plugin system (skip in testing mode)
    if not is_testing and plugin_manager:
        logger.info("Shutting down plugin system...")
        try:
            await plugin_manager.shutdown_all()
            logger.info("Plugin system shut down successfully.")
        except Exception as e:
            logger.error(f"Error shutting down plugins: {e}", exc_info=True)
    else:
        logger.info("Skipping plugin shutdown for %s role.", service_role)

    # Stop MCP server lifespan (skip in testing mode)
    if not is_testing and mcp_lifespan:
        try:
            await mcp_lifespan.__aexit__(None, None, None)
            logger.info("MCP server lifespan stopped")
        except Exception as e:
            logger.error(f"Error stopping MCP lifespan: {e}", exc_info=True)
    else:
        logger.info("Skipping MCP shutdown for %s role.", service_role)

    # Stop the optimization-job sweeper and abandon in-flight optimization
    # jobs (skip in testing mode). shutdown(wait=False) on purpose: a model
    # pass can take minutes; abandoned rows are failed by the sweeper after
    # restart with the retriable user-facing copy.
    if not is_testing and optimization_job_sweeper:
        from preloop.services.session_optimization_jobs import shutdown_job_executor

        try:
            await optimization_job_sweeper.stop()
            logger.info("Optimization job sweeper stopped.")
        except Exception as e:
            logger.error(f"Error stopping optimization job sweeper: {e}", exc_info=True)
        shutdown_job_executor()
        logger.info("Optimization job executor shut down.")
    else:
        logger.info("Skipping optimization job shutdown for %s role.", service_role)

    # Stop the execution monitor (skip in testing mode)
    if not is_testing and execution_monitor:
        try:
            await execution_monitor.stop()
            logger.info("Execution monitor stopped.")
        except Exception as e:
            logger.error(f"Error stopping execution monitor: {e}", exc_info=True)
    else:
        logger.info("Skipping execution monitor shutdown for %s role.", service_role)

    # Cancel the NATS consumer task (skip in testing mode)
    if not is_testing and getattr(app.state, "nats_connected", False):
        if hasattr(app.state, "nats_consumer_task"):
            app.state.nats_consumer_task.cancel()
            logger.info("NATS consumer for WebSockets stopped.")

        logger.info("Shutting down NATS connection...")
        try:
            await close_nats()
            logger.info("NATS connection closed.")
        except Exception as e:
            logger.error(f"Error closing NATS connection: {e}", exc_info=True)
    else:
        logger.info("Skipping NATS shutdown for %s role.", service_role)

    logger.info("Shutting down application...")
    logger.info("Shutting down API usage executor...")
    try:
        from preloop.services.otel_export import shutdown_otel

        shutdown_otel()
    except Exception:
        logger.debug("OTLP shutdown failed", exc_info=True)

    _api_usage_executor.shutdown(wait=False, cancel_futures=True)
    # Restore the original jsonable_encoder
    import fastapi.encoders

    if hasattr(app.state, "original_jsonable_encoder"):
        fastapi.encoders.jsonable_encoder = app.state.original_jsonable_encoder
        logger.info("Restored original jsonable_encoder.")
    logger.info("Application shutdown complete.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        FastAPI: The configured FastAPI application.
    """
    # Load environment variables from .env file
    # load_dotenv()

    # Define base directory relative to this file's location
    base_dir = Path(__file__).resolve().parent.parent.parent

    # Initialize FastAPI app
    app = FastAPI(
        title="Preloop API",
        description="REST API for Preloop issue tracking management",
        version=__version__,
        openapi_url="/api/v1/openapi.json",  # Keep OpenAPI schema URL
        docs_url=None,  # Disable the automatic docs at /docs
        redoc_url=None,  # Disable the automatic redoc at /redoc
        lifespan=lifespan,
    )

    # Add global exception handler to ensure all errors are logged
    @app.exception_handler(ModelGatewayAPIError)
    async def model_gateway_exception_handler(
        request: Request, exc: ModelGatewayAPIError
    ) -> JSONResponse:
        """Render provider-native gateway error bodies."""
        logger.warning(
            "Model gateway error in %s %s: %s",
            request.method,
            request.url.path,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(),
            headers=exc.response_headers(),
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Log all exceptions with full traceback."""
        logger.error(
            f"Unhandled exception in {request.method} {request.url.path}: {exc}",
            exc_info=True,
        )
        # Re-raise HTTPException as-is
        if isinstance(exc, HTTPException):
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail}
            )
        # Return 500 for all other exceptions
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )

    # Replace the default jsonable_encoder function with our custom one
    def custom_jsonable_encoder(obj: Any, *args: Any, **kwargs: Any) -> Any:
        # First let FastAPI's encoder prepare the object
        encoded = jsonable_encoder(obj, *args, **kwargs)
        # Then manually process any datetime objects that might have been missed
        if isinstance(encoded, dict):
            for key, value in encoded.items():
                if isinstance(value, datetime):
                    encoded[key] = value.isoformat()
        elif isinstance(encoded, list):
            for i, item in enumerate(encoded):
                if isinstance(item, datetime):
                    encoded[i] = item.isoformat()
                elif isinstance(item, dict):
                    for key, value in item.items():
                        if isinstance(value, datetime):
                            item[key] = value.isoformat()
        return encoded

    # Patch FastAPI's jsonable_encoder
    import fastapi.encoders

    app.state.original_jsonable_encoder = fastapi.encoders.jsonable_encoder
    fastapi.encoders.jsonable_encoder = custom_jsonable_encoder

    # Configure CORS
    # In development/local mode, allow all origins for MCP and agent containers
    # In production, this should be restricted to specific domains
    dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"
    cors_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Allow all origins in development mode for MCP clients (including containers)
    if dev_mode or os.getenv("ALLOW_ALL_ORIGINS", "false").lower() == "true":
        cors_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    service_role = os.getenv("PRELOOP_SERVICE_ROLE", "all").lower()
    is_api_role = service_role in {"all", "api"}
    is_gateway_role = service_role in {"all", "gateway"}

    # Add profiling middleware only for core API
    if is_api_role:
        app.add_middleware(PyinstrumentMiddleware)

    # Add API usage tracking
    # Can be disabled with DISABLE_API_USAGE_TRACKING=true for debugging
    if (
        is_api_role
        and os.getenv("TESTING") != "true"
        and os.getenv("DISABLE_API_USAGE_TRACKING", "false").lower() != "true"
    ):
        app.add_middleware(ApiUsageMiddleware)

    # Add WebSocket authentication middleware
    # Validates Bearer token during HTTP upgrade before WebSocket handshake
    if is_api_role:
        from preloop.api.middleware import WebSocketAuthMiddleware

        app.add_middleware(WebSocketAuthMiddleware)

    # Rewrite /mcp → /mcp/v1 before Starlette's Mount can redirect.
    # Must be registered here (not in setup_mcp_routes) to guarantee it's
    # in the middleware stack before the first request.
    if is_api_role:
        from preloop.services.mcp_http import MCPPathRewriteMiddleware

        app.add_middleware(MCPPathRewriteMiddleware)

    # --- Custom API Docs Routes (Moved to /docs/api and /docs/redoc) ---
    @app.get("/docs/api", include_in_schema=False)  # Changed path
    async def custom_swagger_ui_html() -> Any:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui.css",
        )

    @app.get("/api/v1/openapi.yaml", include_in_schema=False)
    @app.get("/api/v1/spec", include_in_schema=False)
    async def get_openapi_yaml() -> Any:
        import yaml  # type: ignore
        from fastapi.responses import PlainTextResponse

        schema = app.openapi()
        yaml_str = yaml.dump(schema, sort_keys=False)
        return PlainTextResponse(yaml_str, media_type="application/x-yaml")

    @app.get("/docs/redoc", include_in_schema=False)  # Changed path
    async def custom_redoc_html() -> Any:
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.0.0/bundles/redoc.standalone.js",
        )

    # Add custom OpenAPI schema
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema  # type: ignore

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # Add security schemes and requirements
        openapi_schema["components"]["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT token for authentication",
            }
        }

        # Apply security to all endpoints except auth endpoints, landing page, health checks, and docs
        excluded_prefixes = [
            "/api/v1/auth",
            "/approval",  # Public approval endpoints (token-based, no login required)
            "/api/v1/billing/plans",
            "/api/v1/billing/create-checkout-session",
            "/api/v1/webhooks/flows",
            "/",
            "/static",
            "/register",
            "/logout",
            "/api/v1/health",
            "/api/v1/features",
            "/approval",
        ]
        for path in openapi_schema["paths"]:
            # Check if path starts with any excluded prefix
            is_excluded = False
            for prefix in excluded_prefixes:
                if path == prefix or (prefix != "/" and path.startswith(prefix)):
                    is_excluded = True
                    break
            if not is_excluded:
                # Check if path is exactly /api/v1/openapi.json
                if path == app.openapi_url:
                    continue  # Don't require auth for the schema itself

                for method in openapi_schema["paths"][path]:
                    if method.lower() != "options":  # Skip OPTIONS method
                        openapi_schema["paths"][path][method]["security"] = [
                            {"bearerAuth": []}
                        ]

        app.openapi_schema = openapi_schema  # type: ignore
        return app.openapi_schema  # type: ignore

    app.openapi = custom_openapi  # type: ignore

    # Health/version remain available on every role for container probes.
    app.include_router(
        health.router, prefix="/api/v1", tags=["Health"], include_in_schema=False
    )
    app.include_router(
        version.router, prefix="/api/v1", tags=["Version"], include_in_schema=False
    )

    if is_api_role:
        # OAuth consent page (login form for CLI and MCP OAuth flows)
        from preloop.api.endpoints.oauth_consent import router as oauth_consent_router

        app.include_router(oauth_consent_router)
        logger.info("OAuth consent routes registered")

        # OAuth server endpoints (authorize, token, register, well-known metadata)
        from preloop.api.endpoints.oauth_server import router as oauth_server_router

        app.include_router(oauth_server_router)
        logger.info("OAuth server routes registered")

        # Setup MCP routes with DynamicMCPServer (MUST be before SPA mount)
        setup_mcp_routes(app)
        logger.info("MCP routes configured with DynamicMCPServer")

        # Register plugin routes
        # This allows plugins (both builtin and proprietary) to add their own endpoints
        # We do this before adding standard routers to ensure plugins can override if needed
        # or just be registered alongside
        from preloop.plugins import get_plugin_manager

        plugin_manager = get_plugin_manager()
        plugin_manager.register_routes(app)
        for plugin in plugin_manager._plugins.values():
            app.dependency_overrides.update(plugin.get_dependencies())

        # Core API routers
        app.include_router(auth_router, prefix="/api/v1/auth", tags=["Auth"])
        # WebAuthn passkey ceremonies. Mounted WITHOUT an auth dependency:
        # the authentication ceremony must run before the user has a token.
        # Endpoints that need a signed-in user declare it themselves.
        from preloop.api.auth.webauthn_router import router as webauthn_router

        app.include_router(
            webauthn_router,
            prefix="/api/v1/auth/webauthn",
            tags=["Auth", "Passkeys"],
        )
        app.include_router(
            account.router,
            prefix="/api/v1",
            tags=["Account"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            account.public_router,
            prefix="/api/v1",
            tags=["Account"],
        )  # Public account endpoints (no auth required)
        app.include_router(
            public_approval.router, tags=["Public Approval"], include_in_schema=False
        )  # No auth required, mounted at /approval (not /api/v1/approval)
        app.include_router(
            features.router,
            prefix="/api/v1",
            tags=["Features"],
            include_in_schema=False,
        )  # No auth required
        app.include_router(
            trackers.router,
            prefix="/api/v1",
            tags=["Trackers"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            mcp_servers.router,
            prefix="/api/v1",
            tags=["MCP Servers"],
            dependencies=[Depends(get_current_active_user)],
        )
        # Public MCP OAuth callback (no auth — user is redirected by external server)
        app.include_router(mcp_servers.oauth_callback_router)
        app.include_router(
            tools.router,
            prefix="/api/v1",
            tags=["Tools"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            approval_requests.router,
            prefix="/api/v1",
            tags=["Approval Requests"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            approval_bypass.router,
            prefix="/api/v1",
            tags=["Approval Bypasses"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            notification_preferences.router,
            prefix="/api/v1/notification-preferences",
            tags=["Notification Preferences"],
            # No router-level auth - individual endpoints handle their own auth
            # /register-device and /register-via-token are public (token-based)
        )
        # Note: Issue dependencies endpoint is now loaded via plugins/analytics
        app.include_router(
            organizations.router,
            prefix="/api/v1",
            tags=["Organizations"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            projects.router,
            prefix="/api/v1",
            tags=["Projects"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            issues.router,
            prefix="/api/v1",
            tags=["Issues"],
            dependencies=[Depends(get_current_active_user)],
        )
        # Note: Issue compliance endpoint is now loaded via plugins/analytics
        app.include_router(
            comments.router,
            prefix="/api/v1",
            tags=["Comments"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            search_router.router,
            prefix="/api/v1",
            tags=["Search"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            embedding_router.router,
            prefix="/api/v1",
            tags=["Embeddings"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            ai_models.router,
            prefix="/api/v1",
            tags=["AI Models"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            audio.router,
            prefix="/api/v1",
            tags=["Audio"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            cost.router,
            prefix="/api/v1",
            tags=["Cost Analytics"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            usage_import.router,
            prefix="/api/v1",
            tags=["Usage Import"],
            dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            session_optimization.router,
            prefix="/api/v1",
            tags=["Session Optimization"],
            dependencies=[Depends(get_current_active_user)],
        )
    # AI Model Gateway routers
    if is_gateway_role:
        app.include_router(
            openai_gateway.router,
            prefix="/openai/v1",
            tags=["OpenAI Gateway"],
            include_in_schema=False,
        )
        app.include_router(
            anthropic_gateway.router,
            prefix="/anthropic/v1",
            tags=["Anthropic Gateway"],
            include_in_schema=False,
        )
        app.include_router(
            gemini_gateway.router,
            prefix="/gemini/v1beta",
            tags=["Gemini Gateway"],
            include_in_schema=False,
        )

    if is_api_role:
        # Note: Issue duplicates endpoint is now loaded via plugins/analytics
        app.include_router(webhooks.router, prefix="/api/v1", tags=["Webhooks"])
        app.include_router(
            flows.router,
            prefix="/api/v1",
            tags=["Flows"],
            # dependencies=[Depends(get_current_active_user)],
        )
        app.include_router(
            runners.router,
            prefix="/api/v1",
            tags=["Runners"],
        )

        # Policies router for policy-as-code YAML import/export
        app.include_router(
            policies.router,
            prefix="/api/v1",
            tags=["Policies"],
            dependencies=[Depends(get_current_active_user)],
        )

        # Security-screen scoring endpoint (QM external proxy contract).
        # Auth is handled in-endpoint: callers send an API key in x-api-key.
        app.include_router(
            security_screen.router,
            prefix="/api/v1",
            tags=["Security Screen"],
        )

        # WebSocket router
        app.include_router(websockets.router, prefix="/api/v1", tags=["WebSockets"])
        app.include_router(
            agent_control.router,
            prefix="/api/v1",
            tags=["Agent Control"],
        )
        app.include_router(
            agent_permission.router,
            prefix="/api/v1",
            tags=["Agent Permissions"],
        )

        # Impersonation router - Enterprise feature (loaded via admin plugin)
        # No longer loaded from core - handled by plugins/admin

        app.include_router(
            roles.router,
            prefix="/api/v1",
            tags=["Roles"],
            dependencies=[Depends(get_current_active_user)],
        )

        # --- Public Approval Page ---
        @app.get("/approval/{request_id}", include_in_schema=False)
        async def serve_approval_page(request_id: str) -> FileResponse:
            """Serve the public approval page."""
            approval_html_path = base_dir / "preloop" / "templates" / "approval.html"
            return FileResponse(str(approval_html_path), media_type="text/html")

        # --- Public Invitation Accept Page ---
        @app.get("/invitations/accept", include_in_schema=False)
        async def serve_invitation_accept_page() -> FileResponse:
            """Serve the public invitation accept page."""
            invitation_html_path = (
                base_dir / "preloop" / "templates" / "invitation-accept.html"
            )
            return FileResponse(str(invitation_html_path), media_type="text/html")

    return app
