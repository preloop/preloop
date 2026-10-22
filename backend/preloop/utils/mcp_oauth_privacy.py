"""Keep upstream OAuth browser codes and state out of application access logs."""

import logging
import re

from preloop.utils.sentry_filters import register_private_callback_prefix

_QUERY = re.compile(
    r"(/api/v1/mcp-servers/(?:[^/?\s]+/oauth/authorize|oauth/callback))\?[^\s\"']*"
)


class MCPOAuthLogFilter(logging.Filter):
    """Scrub callback/authorize query strings while preserving Uvicorn fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _QUERY.sub(r"\1?[redacted]", value) if isinstance(value, str) else value
                for value in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _QUERY.sub(r"\1?[redacted]", record.msg)
        return True


def install_mcp_oauth_privacy() -> None:
    """Installed when routes load, after the ASGI server configures logging."""
    for name in ("uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, MCPOAuthLogFilter) for item in logger.filters):
            logger.addFilter(MCPOAuthLogFilter())
    register_private_callback_prefix("/api/v1/mcp-servers/oauth/")
