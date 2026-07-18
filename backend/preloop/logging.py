"""Logging configuration for Preloop."""

import importlib
import os

# Import stdlib logging.config via importlib so this module (also named
# ``logging``) does not look like a self-import to static analysis.
_logging_config = importlib.import_module("logging.config")

# Get log level from environment
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ROOT_LOG_LEVEL = os.getenv("ROOT_LOG_LEVEL", "WARNING").upper()


def configure_logging() -> None:
    """Configure logging for the application."""
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            },
            "json": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": LOG_LEVEL,
            },
        },
        "loggers": {
            "preloop": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False,
            },
            "preloop-sync": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False,
            },
            "sqlalchemy": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "fakeredis": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "httpx": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "httpcore": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "apscheduler": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "docket": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "docket.worker": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "watchfiles": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
        },
        "root": {"handlers": ["console"], "level": ROOT_LOG_LEVEL},
    }

    # Try to use JSON formatter if available
    try:
        import pythonjsonlogger.jsonlogger  # noqa

        # Use JSON formatter if the module is available
        handlers = logging_config["handlers"]
        handlers["console"]["formatter"] = "json"
    except ImportError:
        # JSON formatter not available, use default
        pass

    # Configure logging
    _logging_config.dictConfig(logging_config)
