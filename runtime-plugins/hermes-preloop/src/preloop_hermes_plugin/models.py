"""Gateway model-list refresh for the Hermes plugin.

Derives the gateway ``GET /models`` URL from the Agent Control WS URL,
fetches the current model list, and makes it available for programmatic
use.

NOT WIRED YET.  Nothing in ``preloop_hermes_plugin`` imports these
helpers: Hermes's plugin API (``register_hook("pre_tool_call", ...)``)
exposes no runtime model-list mutation, so there is no hook to call them
from.  The module is kept as the ready-made fetch side of that future
hook, and its behavior is pinned by ``tests/test_models.py``.  Wire
``refresh_models`` into the hook once Hermes supports catalog updates.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib import parse, request

logger = logging.getLogger(__name__)

# Network timeout for the GET /models call (seconds).
FETCH_TIMEOUT_SECONDS = 15.0


def gateway_models_url(control_ws_url: str) -> str:
    """Derive the gateway ``GET /models`` URL from the control WS URL.

    Example::

        wss://app.preloop.ai/api/v1/agents/control/ws
         -> https://app.preloop.ai/openai/v1/models
    """
    parsed = parse.urlparse(control_ws_url)
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    return f"{scheme}://{parsed.netloc}/openai/v1/models"


def fetch_gateway_models(
    control_ws_url: str,
    bearer_token: str,
) -> list[dict[str, Any]]:
    """Fetch the current model list from the Preloop gateway.

    Returns the list of model objects from the ``data`` (or ``models``)
    field.  Raises on network or HTTP errors.
    """
    url = gateway_models_url(control_ws_url)
    req = request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
    )
    with request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        body = json.loads(resp.read())
    if isinstance(body, dict):
        return body.get("data") or body.get("models") or []
    return []


def refresh_models(
    control_ws_url: str,
    bearer_token: str,
) -> list[dict[str, Any]]:
    """Best-effort model refresh.  Returns the model list or ``[]``.

    Hermes's plugin hook API does not support runtime model-list
    mutation (the ``register_hook`` surface is limited to
    ``pre_tool_call``).  This helper fetches and returns the list so
    callers with additional capabilities can act on it.
    """
    try:
        models = fetch_gateway_models(control_ws_url, bearer_token)
        logger.info("Preloop model refresh: fetched %d model(s)", len(models))
        return models
    except Exception as exc:
        logger.warning("Preloop model refresh failed: %s", exc)
        return []
