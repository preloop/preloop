"""Shared default Docker images for hosted executors and private runners."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Index digest published by Publish Universal Image from main 99557e60f
# (run 41302063952). The attestation subject is this same digest.
_HOSTED_CODEX_IMAGE = (
    "ghcr.io/preloop/preloop-codex-universal@sha256:"
    "191dc7e2ef84b7a05c2b3d6403df59c9b2e51e890b0853eae1935ccc4417dd78"
)
_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}\Z")
_codex_tag_warning_emitted = False

# Fallback images used when the matching *_IMAGE env var is unset. Keep
# these in one place so RemoteRunnerExecutor injects the same image the
# hosted executor would have started. Codex is pinned by digest.
DEFAULT_AGENT_IMAGES: dict[str, str] = {
    "pi": "node:22-bookworm",
    "deepseek": "node:22-bookworm",
    "opencode": "docker/sandbox-templates:opencode",
    "codex": _HOSTED_CODEX_IMAGE,
    "aider": "dustinwashington/aider-ce:v0.88.6",
    "gemini": "docker/sandbox-templates:gemini",
    "openhands": "spacebridge/openhands:latest-tmux",
}

_IMAGE_ENV_VARS: dict[str, str] = {
    "pi": "PI_IMAGE",
    "deepseek": "DEEPSEEK_IMAGE",
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
        Image reference, honoring a non-empty matching ``*_IMAGE``
        environment variable, or ``None`` when the agent type has no
        default. A blank override is treated as unset.
    """
    key = (agent_type or "").strip().lower()
    fallback = DEFAULT_AGENT_IMAGES.get(key)
    if fallback is None:
        return None
    override = os.environ.get(_IMAGE_ENV_VARS[key])
    if override is None or not override.strip():
        return fallback
    return override


def warn_codex_image_tag_once() -> None:
    """Log one warning when ``CODEX_IMAGE`` is a tag rather than a digest.

    An unset or blank variable uses the digest in ``DEFAULT_AGENT_IMAGES``
    and does not warn. Repeated calls in one process log at most once.
    """
    global _codex_tag_warning_emitted
    if _codex_tag_warning_emitted:
        return
    raw = os.environ.get("CODEX_IMAGE")
    if raw is None or not raw.strip():
        return
    reference = raw.strip()
    if _DIGEST_PIN.search(reference) is not None:
        return
    _codex_tag_warning_emitted = True
    logger.warning(
        "CODEX_IMAGE is a tag (%s), not a digest. Pin it as "
        "name@sha256:<64 hex chars> so the image can be cached.",
        reference,
    )


def agent_config_has_image(agent_config: dict[str, object]) -> bool:
    """Return True when agent_config already names an image.

    Args:
        agent_config: Flow agent settings.

    Returns:
        True if ``image`` or ``docker_image`` is a non-empty string.
    """
    return effective_agent_image(agent_config) is not None


def effective_agent_image(agent_config: dict[str, object]) -> Optional[str]:
    """Resolve the private runner's canonical/legacy image override."""
    for key in ("image", "docker_image"):
        value = agent_config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
