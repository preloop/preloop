"""Server entry point for Preloop REST API."""

import argparse
import importlib
import os
import sys
from typing import Optional

import uvicorn

import preloop.logging as preloop_logging

# Add project root to sys.path to ensure SpaceModels can be imported
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Resolve stdlib logging without `import logging`, which CodeQL flags as a
# self-import relative to preloop.logging.
logger = importlib.import_module("logging").getLogger(__name__)


def start_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    debug: Optional[bool] = None,
    init_test_data: Optional[bool] = None,
) -> None:
    """Start the Preloop REST API server.

    Args:
        host: Host to bind to, defaults to HOST env var or 0.0.0.0
        port: Port to bind to, defaults to PORT env var or 8000
        debug: Whether to run in debug mode, defaults to DEBUG env var or False
        init_test_data: Whether to initialize test data, defaults to INIT_TEST_DATA env var or False
    """
    # Configure logging first
    preloop_logging.configure_logging()

    # Set server parameters from environment or defaults
    host = host or os.getenv("HOST", "0.0.0.0")
    port = port or int(os.getenv("PORT", "8000"))
    debug = (
        debug if debug is not None else os.getenv("DEBUG", "false").lower() == "true"
    )

    # Set up test data initialization flag
    init_test_data = (
        init_test_data
        if init_test_data is not None
        else os.getenv("INIT_TEST_DATA", "false").lower() == "true"
    )

    # Set environment variable for the app to use
    if init_test_data:
        os.environ["INIT_TEST_DATA"] = "true"
        logger.info("Test data initialization enabled")

    # Start the Uvicorn server with FastAPI
    logger.info(f"Starting Preloop REST API server on {host}:{port} (debug={debug})")

    uvicorn.run(
        "preloop.api.app:create_app",
        host=host,
        port=port,
        reload=debug,
        factory=True,
        # log_level="debug" if debug else "info", # Let configure_logging handle this
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the Preloop REST API server")
    parser.add_argument("--host", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Port to bind to (default: 8000)")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")
    parser.add_argument(
        "--init-test-data", action="store_true", help="Initialize test data"
    )

    args = parser.parse_args()

    start_server(
        host=args.host,
        port=args.port,
        debug=args.debug,
        init_test_data=args.init_test_data,
    )
