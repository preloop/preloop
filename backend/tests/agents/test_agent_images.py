"""Tests for the shared default agent image helper."""

import os
from unittest.mock import patch

from preloop.agents.aider import AiderAgent
from preloop.agents.codex import CodexAgent
from preloop.agents.gemini import GeminiAgent
from preloop.agents.images import DEFAULT_AGENT_IMAGES, default_agent_image
from preloop.agents.opencode import OpenCodeAgent


def test_default_agent_image_matches_hosted_executors() -> None:
    assert default_agent_image("opencode") == OpenCodeAgent({}).image
    assert default_agent_image("codex") == CodexAgent({}).image
    assert default_agent_image("aider") == AiderAgent({}).image
    assert default_agent_image("gemini") == GeminiAgent({}).image
    assert default_agent_image("OPENCODE") == DEFAULT_AGENT_IMAGES["opencode"]
    assert default_agent_image("unknown") is None


def test_default_agent_image_honors_env_overrides() -> None:
    with patch.dict(
        os.environ,
        {
            "OPENCODE_IMAGE": "custom/opencode:dev",
            "CODEX_IMAGE": "custom/codex:dev",
            "AIDER_IMAGE": "custom/aider:dev",
            "GEMINI_IMAGE": "custom/gemini:dev",
        },
    ):
        assert default_agent_image("opencode") == "custom/opencode:dev"
        assert default_agent_image("codex") == "custom/codex:dev"
        assert default_agent_image("aider") == "custom/aider:dev"
        assert default_agent_image("gemini") == "custom/gemini:dev"
        assert OpenCodeAgent({}).image == "custom/opencode:dev"
        assert CodexAgent({}).image == "custom/codex:dev"
