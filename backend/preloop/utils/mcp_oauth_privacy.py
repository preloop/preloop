"""Keep upstream OAuth codes and flow webhook secrets out of access logs."""

import logging
import re

from preloop.utils.sentry_filters import (
    redact_flow_webhook_secret,
    register_private_callback_prefix,
)

_QUERY = re.compile(
    r"(/api/v1/mcp-servers/(?:[^/?\s]+/oauth/authorize|oauth/callback))\?[^\s\"']*"
)


def _scrub(value: str) -> str:
    """Redact OAuth query strings and flow webhook secrets in one string."""
    return redact_flow_webhook_secret(_QUERY.sub(r"\1?[redacted]", value))


class MCPOAuthLogFilter(logging.Filter):
    """Scrub OAuth query strings and webhook secrets, keeping Uvicorn fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _scrub(value) if isinstance(value, str) else value
                for value in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        return True


def install_mcp_oauth_privacy() -> None:
    """Installed when routes load, after the ASGI server configures logging."""
    for name in ("uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, MCPOAuthLogFilter) for item in logger.filters):
            logger.addFilter(MCPOAuthLogFilter())
    register_private_callback_prefix("/api/v1/mcp-servers/oauth/")
