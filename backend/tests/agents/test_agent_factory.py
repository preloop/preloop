"""Tests for agent factory."""

import pytest

from preloop.agents.factory import create_agent_executor, SUPPORTED_AGENT_TYPES
from preloop.agents.openhands import OpenHandsAgent
from preloop.agents.aider import AiderAgent
from preloop.agents.codex import CodexAgent
from preloop.agents.gemini import GeminiAgent
from preloop.agents.opencode import OpenCodeAgent


class TestAgentFactory:
    """Test agent factory functionality."""

    def test_create_openhands_agent(self):
        """Test creating an OpenHands agent."""
        config = {
            "agent_type": "CodeActAgent",
            "max_iterations": 10,
        }

        agent = create_agent_executor("openhands", config)

        assert isinstance(agent, OpenHandsAgent)
        assert agent.agent_type == "openhands"
        assert agent.config == config

    def test_create_openhands_agent_case_insensitive(self):
        """Test that agent type is case-insensitive."""
        config = {"agent_type": "CodeActAgent"}

        # Test various cases
        for agent_type in ["openhands", "OpenHands", "OPENHANDS", "OpEnHaNdS"]:
            agent = create_agent_executor(agent_type, config)
            assert isinstance(agent, OpenHandsAgent)

    def test_create_aider_agent(self):
        """Test creating an Aider agent."""
        config = {
            "model": "gpt-5.4",
            "edit_format": "whole",
        }

        agent = create_agent_executor("aider", config)

        assert isinstance(agent, AiderAgent)
        assert agent.agent_type == "aider"
        assert agent.config == config

    def test_create_codex_agent(self):
        """Test creating a Codex agent."""
        config = {
            "model": "gpt-5.4",
        }

        agent = create_agent_executor("codex", config)

        assert isinstance(agent, CodexAgent)
        assert agent.agent_type == "codex"
        assert agent.config == config

    def test_create_gemini_agent(self):
        """Test creating a Gemini agent."""
        config = {
            "model": "gemini-3-pro-preview",
        }

        agent = create_agent_executor("gemini", config)

        assert isinstance(agent, GeminiAgent)
        assert agent.agent_type == "gemini"
        assert agent.config == config

    def test_create_opencode_agent(self):
        """Test creating an OpenCode agent."""
        config = {
            "model": "claude-sonnet-4-20250514",
        }

        agent = create_agent_executor("opencode", config)

        assert isinstance(agent, OpenCodeAgent)
        assert agent.agent_type == "opencode"
        assert agent.config == config

    def test_cursor_is_not_a_hosted_agent_type(self):
        assert "cursor" not in SUPPORTED_AGENT_TYPES
        with pytest.raises(ValueError, match="Unsupported agent type"):
            create_agent_executor("cursor", {})

    def test_unsupported_agent_type(self):
        """Test that unsupported agent type raises ValueError."""
        config = {}

        with pytest.raises(ValueError, match="Unsupported agent type"):
            create_agent_executor("unknown-agent", config)

    def test_unsupported_agent_type_message(self):
        """Test that error message lists supported types."""
        config = {}

        try:
            create_agent_executor("invalid", config)
        except ValueError as e:
            error_msg = str(e)
            assert "invalid" in error_msg
            assert "openhands" in error_msg
            assert "aider" in error_msg
            assert "codex" in error_msg
            assert "gemini" in error_msg
            assert "opencode" in error_msg

    def test_config_passed_to_agent(self):
        """Test that configuration is passed to the agent."""
        config = {
            "agent_type": "PlannerAgent",
            "max_iterations": 20,
            "custom_setting": "test_value",
        }

        agent = create_agent_executor("openhands", config)

        assert agent.config == config
        assert agent.config["agent_type"] == "PlannerAgent"
        assert agent.config["max_iterations"] == 20
        assert agent.config["custom_setting"] == "test_value"

    def test_empty_config(self):
        """Test creating agent with empty configuration."""
        agent = create_agent_executor("openhands", {})

        assert isinstance(agent, OpenHandsAgent)
        assert agent.config == {}

    def test_create_multiple_agents(self):
        """Test creating multiple agent instances."""
        config1 = {"agent_type": "CodeActAgent"}
        config2 = {"agent_type": "PlannerAgent"}

        agent1 = create_agent_executor("openhands", config1)
        agent2 = create_agent_executor("openhands", config2)

        # Should be different instances
        assert agent1 is not agent2
        assert agent1.config != agent2.config

    def test_agent_type_validation(self):
        """Test that agent type validation works correctly."""
        valid_types = {
            "openhands": OpenHandsAgent,
            "OPENHANDS": OpenHandsAgent,
            "OpenHands": OpenHandsAgent,
            "aider": AiderAgent,
            "AIDER": AiderAgent,
            "codex": CodexAgent,
            "CODEX": CodexAgent,
            "gemini": GeminiAgent,
            "GEMINI": GeminiAgent,
            "opencode": OpenCodeAgent,
            "OPENCODE": OpenCodeAgent,
            "OpenCode": OpenCodeAgent,
        }
        invalid_types = ["open hands", "open-hands-v2", "", "  ", "123", "unknown"]

        # Valid types should work
        for agent_type, expected_class in valid_types.items():
            agent = create_agent_executor(agent_type, {})
            assert isinstance(agent, expected_class)

        # Invalid types should raise ValueError
        for agent_type in invalid_types:
            with pytest.raises(ValueError):
                create_agent_executor(agent_type, {})
