"""Tests for OpenHandsAgent implementation."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from preloop.agents.openhands import OpenHandsAgent
from preloop.utils.execve_limits import (
    MAX_LAUNCH_STRING_BYTES,
    MAX_LEGACY_PROMPT_BYTES,
    PROMPT_ENV_PREFIX,
    PROMPT_FILE_PATH,
    largest_launch_string,
    prompt_transport_env,
)


@pytest.fixture
def openhands_config():
    """Sample OpenHands agent configuration."""
    return {
        "agent_type": "CodeActAgent",
        "max_iterations": 15,
        "custom_setting": "value",
    }


@pytest.fixture
def mock_docker():
    """Mock aiodocker Docker client."""
    with patch("preloop.agents.container.aiodocker.Docker") as mock:
        docker_instance = AsyncMock()
        mock.return_value = docker_instance
        docker_instance.containers.create = AsyncMock()
        yield docker_instance


class TestOpenHandsAgent:
    """Test OpenHandsAgent class."""

    def test_init_default_image(self, openhands_config):
        """Test OpenHandsAgent initialization with default image."""
        agent = OpenHandsAgent(openhands_config)

        assert agent.agent_type == "openhands"
        assert agent.config == openhands_config
        assert agent.image == "spacebridge/openhands:latest-tmux"
        assert agent.use_kubernetes is False

    def test_init_custom_image(self, openhands_config):
        """Test OpenHandsAgent initialization with custom image."""
        with patch.dict(os.environ, {"OPENHANDS_IMAGE": "custom-image:v1.0"}):
            agent = OpenHandsAgent(openhands_config)
            assert agent.image == "custom-image:v1.0"

    def test_init_kubernetes_enabled(self, openhands_config):
        """Test OpenHandsAgent with Kubernetes enabled."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "true"}):
            agent = OpenHandsAgent(openhands_config)
            assert agent.use_kubernetes is True

    def test_init_kubernetes_disabled(self, openhands_config):
        """Test OpenHandsAgent with Kubernetes explicitly disabled."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "false"}):
            agent = OpenHandsAgent(openhands_config)
            assert agent.use_kubernetes is False

    @pytest.mark.asyncio
    async def test_start_with_agent_config(self, openhands_config, mock_docker):
        """Test starting OpenHands with agent configuration."""
        mock_container = AsyncMock()
        mock_container.id = "openhands-container-123"
        mock_docker.containers.create.return_value = mock_container

        agent = OpenHandsAgent(openhands_config)

        execution_context = {
            "flow_id": "flow-456",
            "execution_id": "exec-789",
            "prompt": "Fix the authentication bug",
            "agent_config": {
                "agent_type": "PlannerAgent",
                "max_iterations": 20,
            },
            "model_identifier": "gpt-5.4",
            "model_api_key": "test-key",
        }

        session_ref = await agent.start(execution_context)

        assert session_ref == "openhands-container-123"
        mock_container.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_default_agent_type(self, mock_docker):
        """Test starting OpenHands with default CodeActAgent type."""
        mock_container = AsyncMock()
        mock_container.id = "openhands-container-456"
        mock_docker.containers.create.return_value = mock_container

        agent = OpenHandsAgent({})

        execution_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Test prompt",
            "agent_config": {},  # No agent_type specified
        }

        await agent.start(execution_context)

        # Verify that CodeActAgent is used as default
        # This is verified through the environment variables set
        mock_docker.containers.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_default_max_iterations(self, mock_docker):
        """Test starting OpenHands with default max_iterations."""
        mock_container = AsyncMock()
        mock_container.id = "openhands-container-789"
        mock_docker.containers.create.return_value = mock_container

        agent = OpenHandsAgent({})

        execution_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Test prompt",
            "agent_config": {},  # No max_iterations specified
        }

        await agent.start(execution_context)

        # Default should be 10
        mock_docker.containers.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_environment_openhands_specific(self, openhands_config):
        """Test that OpenHands-specific environment variables are set."""
        agent = OpenHandsAgent(openhands_config)

        execution_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Implement feature X",
            "agent_config": {
                "agent_type": "CodeActAgent",
                "max_iterations": 25,
            },
            "model_identifier": "gpt-5.4-turbo",
            "model_api_key": "sk-test-key",
            "model_provider": "openai",
            "model_parameters": {
                "temperature": 0.7,
                "max_tokens": 4000,
            },
            "openhands_agent_type": "CodeActAgent",
            "max_iterations": 25,
        }

        env = await agent._prepare_environment(execution_context)

        # Check OpenHands-specific variables
        assert env["AGENT_TYPE"] == "CodeActAgent"
        assert env["MAX_ITERATIONS"] == "25"
        assert env["PROMPT"] == "Implement feature X"

        # Check AI model variables
        assert env["LLM_MODEL"] == "gpt-5.4-turbo"
        assert env["LLM_API_KEY"] == "sk-test-key"
        assert env["LLM_PROVIDER"] == "openai"
        assert env["LLM_TEMPERATURE"] == "0.7"
        assert env["LLM_MAX_TOKENS"] == "4000"

    @pytest.mark.asyncio
    async def test_prepare_environment_minimal(self, openhands_config):
        """Test environment preparation with minimal context."""
        agent = OpenHandsAgent(openhands_config)

        execution_context = {
            "prompt": "Simple task",
            "openhands_agent_type": "CodeActAgent",
            "max_iterations": 10,
        }

        env = await agent._prepare_environment(execution_context)

        assert env["AGENT_TYPE"] == "CodeActAgent"
        assert env["MAX_ITERATIONS"] == "10"
        assert env["PROMPT"] == "Simple task"

        # Optional fields should not be present
        assert "LLM_MODEL" not in env
        assert "LLM_API_KEY" not in env

    @pytest.mark.asyncio
    async def test_prepare_environment_gateway_uses_openai_compatible_endpoint(
        self, openhands_config
    ):
        """Gateway mode should configure OpenHands through the OpenAI API shape."""
        agent = OpenHandsAgent(openhands_config)

        env = await agent._prepare_environment(
            {
                "prompt": "Use the gateway",
                "openhands_agent_type": "CodeActAgent",
                "max_iterations": 10,
                "model_gateway_enabled": True,
                "model_gateway_model_alias": "google/gemini-2.5-pro",
                "model_gateway_token": "gw-token-123",
                "model_gateway_url": "https://review.preloop.ai/gemini/v1beta",
            }
        )

        assert env["LLM_MODEL"] == "openai/google/gemini-2.5-pro"
        assert env["LLM_PROVIDER"] == "openai"
        assert env["LLM_API_KEY"] == "gw-token-123"
        assert env["LLM_BASE_URL"] == "https://review.preloop.ai/openai/v1"
        assert env["OPENAI_API_BASE"] == "https://review.preloop.ai/openai/v1"

    @pytest.mark.asyncio
    async def test_start_enhances_context(self, openhands_config, mock_docker):
        """Test that start method enhances execution context."""
        mock_container = AsyncMock()
        mock_container.id = "container-xyz"
        mock_docker.containers.create.return_value = mock_container

        agent = OpenHandsAgent(openhands_config)

        original_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Test",
            "agent_config": {
                "agent_type": "PlannerAgent",
                "max_iterations": 30,
            },
        }

        await agent.start(original_context)

        # Verify that the container was created
        # The enhanced context should include openhands_agent_type and max_iterations
        mock_docker.containers.create.assert_called_once()
        mock_container.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_openhands_agent_types(self, mock_docker):
        """Test that different OpenHands agent types are supported."""
        mock_container = AsyncMock()
        mock_container.id = "container-123"
        mock_docker.containers.create.return_value = mock_container

        agent_types = ["CodeActAgent", "PlannerAgent", "MonologueAgent"]

        for agent_type in agent_types:
            agent = OpenHandsAgent({"agent_type": agent_type})

            execution_context = {
                "flow_id": "flow-123",
                "execution_id": "exec-456",
                "prompt": f"Test {agent_type}",
                "agent_config": {"agent_type": agent_type},
            }

            await agent.start(execution_context)

            # Verify container was created
            assert mock_docker.containers.create.called

    @pytest.mark.asyncio
    async def test_model_parameters_handling(self, openhands_config):
        """Test that model parameters are properly handled."""
        agent = OpenHandsAgent(openhands_config)

        execution_context = {
            "prompt": "Test task",
            "model_identifier": "gpt-5.4",
            "model_parameters": {
                "temperature": 0.9,
                "max_tokens": 2000,
                "top_p": 0.95,  # This should be ignored as it's not in the list
            },
            "openhands_agent_type": "CodeActAgent",
            "max_iterations": 10,
        }

        env = await agent._prepare_environment(execution_context)

        assert env["LLM_TEMPERATURE"] == "0.9"
        assert env["LLM_MAX_TOKENS"] == "2000"
        # top_p is not in the supported parameters list
        assert "LLM_TOP_P" not in env

    @pytest.mark.asyncio
    async def test_model_parameters_missing(self, openhands_config):
        """Test behavior when model_parameters is missing."""
        agent = OpenHandsAgent(openhands_config)

        execution_context = {
            "prompt": "Test task",
            "model_identifier": "gpt-5.4",
            # No model_parameters
            "openhands_agent_type": "CodeActAgent",
            "max_iterations": 10,
        }

        env = await agent._prepare_environment(execution_context)

        # Should not fail, just skip the parameters
        assert "LLM_TEMPERATURE" not in env
        assert "LLM_MAX_TOKENS" not in env


class TestOpenHandsGitCloneCredentials:
    """OpenHands overrides _prepare_git_clone_command, so it needs its own
    coverage for the credential leak in issue #173.
    """

    PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def _context(self, repo_url="https://github.com/acme/private.git"):
        return {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": repo_url,
                        "clone_path": "/workspace",
                        "tracker_id": "tracker-1",
                    }
                ],
            },
            "git_credentials_map": {
                "tracker-1": {"token": self.PAT, "tracker_type": "github"}
            },
        }

    def test_clone_command_contains_no_token(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        command = agent._prepare_git_clone_command(self._context())
        assert self.PAT not in command
        assert "@github.com" not in command

    def test_clone_url_is_credential_free(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        command = agent._prepare_git_clone_command(self._context())
        assert "git clone https://github.com/acme/private.git /workspace" in command

    def test_credential_helper_is_installed_before_cloning(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        command = agent._prepare_git_clone_command(self._context())
        assert command.index("credential.helper") < command.index("git clone")

    def test_token_travels_in_the_environment(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        context = self._context()
        agent._prepare_git_clone_command(context)
        env = agent._git_credential_env(context)
        assert self.PAT in env["PRELOOP_GIT_CREDENTIALS"]

    def test_url_token_is_stripped(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        command = agent._prepare_git_clone_command(
            self._context(f"https://{self.PAT}@github.com/acme/private.git")
        )
        assert self.PAT not in command

    def test_repo_url_and_branch_are_shell_quoted(self, openhands_config):
        """Both are attacker-influenced through webhook payloads."""
        agent = OpenHandsAgent(openhands_config)
        context = self._context("https://github.com/acme/repo.git; echo pwned")
        context["git_clone_config"]["repositories"][0]["branch"] = "main; echo pwned"
        command = agent._prepare_git_clone_command(context)
        assert "-b 'main; echo pwned'" in command
        assert "'https://github.com/acme/repo.git; echo pwned'" in command


class TestOpenHandsPromptTransport:
    """OpenHands is the default agent_type and must use chunked prompt transport."""

    def _context(self, prompt: str, **extra):
        ctx = {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "prompt": prompt,
            "openhands_agent_type": "CodeActAgent",
            "max_iterations": 10,
            "agent_config": {},
        }
        ctx.update(extra)
        return ctx

    def test_script_reads_prompt_from_the_materialized_file_not_inline(self):
        agent = OpenHandsAgent({})
        prompt = 'Fix the "auth" bug; do not touch `main`'
        script = agent._build_openhands_script(self._context(prompt))
        assert prompt not in script
        assert f'-t "$(cat {PROMPT_FILE_PATH})"' in script
        assert f'-t "{prompt}"' not in script
        assert "PRELOOP_AGENT_PROMPT_" in script

    def test_script_stays_under_the_execve_budget_for_a_huge_prompt(self):
        agent = OpenHandsAgent({})
        prompt = "PR body:\n" + ("x" * (200 * 1024))
        script = agent._build_openhands_script(self._context(prompt))
        assert prompt not in script
        assert len(script.encode()) < MAX_LAUNCH_STRING_BYTES
        env = prompt_transport_env(prompt)
        biggest = largest_launch_string(command=["bash"], args=["-c", script], env=env)
        assert biggest.size < MAX_LAUNCH_STRING_BYTES, biggest

    @pytest.mark.asyncio
    async def test_small_prompt_still_sets_legacy_prompt_env(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        env = await agent._prepare_environment(self._context("Implement feature X"))
        assert env["PROMPT"] == "Implement feature X"
        assert env["AGENT_PROMPT"] == "Implement feature X"
        assert env["AGENT_PROMPT_FILE"] == PROMPT_FILE_PATH
        assert f"{PROMPT_ENV_PREFIX}0" in env

    @pytest.mark.asyncio
    async def test_large_prompt_omits_unbounded_prompt_env(self, openhands_config):
        agent = OpenHandsAgent(openhands_config)
        prompt = "y" * (MAX_LEGACY_PROMPT_BYTES + 1)
        env = await agent._prepare_environment(self._context(prompt))
        assert "PROMPT" not in env
        assert "AGENT_PROMPT" not in env
        assert env["AGENT_PROMPT_FILE"] == PROMPT_FILE_PATH
        assert f"{PROMPT_ENV_PREFIX}0" in env
        assert f"{PROMPT_ENV_PREFIX}CHUNKS" in env

    @pytest.mark.asyncio
    async def test_docker_cmd_does_not_inline_quoted_prompt(
        self, openhands_config, mock_docker
    ):
        mock_container = AsyncMock()
        mock_container.id = "openhands-quoted-1"
        mock_docker.containers.create.return_value = mock_container

        agent = OpenHandsAgent(openhands_config)
        prompt = 'Fix the "auth" bug'
        await agent.start(self._context(prompt))

        config = mock_docker.containers.create.call_args.kwargs["config"]
        script = config["Cmd"][-1]
        assert prompt not in script
        assert f'-t "$(cat {PROMPT_FILE_PATH})"' in script
        env = {}
        for entry in config["Env"]:
            name, _, value = str(entry).partition("=")
            env[name] = value
        assert env["PROMPT"] == prompt
        assert env["AGENT_PROMPT_FILE"] == PROMPT_FILE_PATH
        assert f"{PROMPT_ENV_PREFIX}0" in env

    @pytest.mark.asyncio
    async def test_k8s_pins_script_and_chunked_prompt_env(self):
        """Hosted Kubernetes must hand the base the script and chunked env."""
        agent = OpenHandsAgent({})
        ctx = self._context('Fix the "auth" bug')
        script = agent._build_openhands_script(ctx)
        with patch(
            "preloop.agents.container.ContainerAgentExecutor._start_kubernetes_pod",
            new_callable=AsyncMock,
            return_value="job-name",
        ) as mock_parent:
            await agent._start_kubernetes_pod(ctx)
            call_ctx = mock_parent.call_args[0][0]
            assert call_ctx["_container_command"] == ["bash"]
            assert call_ctx["_container_args"] == ["-c", script]
            env = call_ctx["_agent_env"]
            assert env["AGENT_PROMPT_FILE"] == PROMPT_FILE_PATH
            assert f"{PROMPT_ENV_PREFIX}0" in env
            assert env["AGENT_TYPE"] == "CodeActAgent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_config,expected",
    [
        # The flow's per-run turn limit caps OpenHands' own loop.
        ({"limits": {"max_turns": 40}}, 40),
        # An explicit OpenHands setting still wins over the run limit.
        ({"max_iterations": 15, "limits": {"max_turns": 40}}, 15),
        # Neither set: the long-standing default.
        ({}, 10),
    ],
)
async def test_start_takes_max_iterations_from_run_limits(agent_config, expected):
    """agent_config.limits.max_turns becomes OpenHands' -i when unset."""
    from unittest.mock import patch

    from preloop.agents.openhands import ContainerAgentExecutor

    agent = OpenHandsAgent({})
    with patch.object(
        ContainerAgentExecutor, "start", new=AsyncMock(return_value="ref")
    ) as parent_start:
        await agent.start(
            {
                "flow_id": "flow-1",
                "execution_id": "exec-1",
                "prompt": "Triage new issues.",
                "agent_config": agent_config,
            }
        )

    context = parent_start.call_args.args[0]
    assert context["max_iterations"] == expected
