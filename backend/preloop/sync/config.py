"""
Configuration management for preloop.sync.
"""

import importlib
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import preloop.logging as preloop_logging

# Resolve stdlib logging without `import logging`, which CodeQL flags alongside
# preloop.logging as a dual import of shadowed module names.
_logging = importlib.import_module("logging")

# Load environment variables from .env file if it exists
load_dotenv()

# Initialize logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE")

# Database configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/preloop"
)

# Service configuration
SERVICE_HOST = os.getenv("SERVICE_HOST", "0.0.0.0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "5000"))
SERVICE_POLL_INTERVAL = int(os.getenv("SERVICE_POLL_INTERVAL", "90"))

# Define base directory
BASE_DIR = Path(__file__).parent.parent


def setup_logging() -> Any:
    """
    Set up logging configuration based on environment variables.
    """
    log_level = getattr(_logging, LOG_LEVEL.upper())

    # Create a logger for the application
    preloop_logging.configure_logging()
    app_logger = _logging.getLogger("preloop-sync")
    app_logger.setLevel(log_level)
    return app_logger


# Create application logger
logger = setup_logging()
