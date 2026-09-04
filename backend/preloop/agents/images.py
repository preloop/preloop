"""Shared default Docker images for hosted executors and private runners."""

from __future__ import annotations

import os
from typing import Optional

# Fallback tags used when the matching *_IMAGE env var is unset. Keep these
# in one place so RemoteRunnerExecutor injects the same image the hosted
# executor would have started.
DEFAULT_AGENT_IMAGES: dict[str, str] = {
    "opencode": "docker/sandbox-templates:opencode",
    "codex": "ghcr.io/openai/codex-universal:latest",
    "aider": "dustinwashington/aider-ce:v0.88.6",
    "gemini": "docker/sandbox-templates:gemini",
    "openhands": "spacebridge/openhands:latest-tmux",
}

_IMAGE_ENV_VARS: dict[str, str] = {
    "opencode": "OPENCODE_IMAGE",
    "codex": "CODEX_IMAGE",
    "aider": "AIDER_IMAGE",
    "gemini": "GEMINI_IMAGE",
    "openhands": "OPENHANDS_IMAGE",
}


def default_agent_image(agent_type: str) -> Optional[str]:
    """Return the per-agent-type Docker image hosted executors use.

    Args:
        agent_type: Agent harness name (case-insensitive).

    Returns:
        Image reference, honoring the matching ``*_IMAGE`` environment
        variable, or ``None`` when the agent type has no default.
    """
    key = (agent_type or "").strip().lower()
    fallback = DEFAULT_AGENT_IMAGES.get(key)
    if fallback is None:
        return None
    return os.getenv(_IMAGE_ENV_VARS[key], fallback)


def agent_config_has_image(agent_config: dict[str, object]) -> bool:
    """Return True when agent_config already names an image.

    Args:
        agent_config: Flow agent settings.

    Returns:
        True if ``image`` or ``docker_image`` is a non-empty string.
    """
    for key in ("image", "docker_image"):
        value = agent_config.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False
