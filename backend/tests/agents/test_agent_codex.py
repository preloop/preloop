"""Tests for Codex agent implementation."""

import os
from unittest.mock import patch, AsyncMock

import pytest

from preloop.agents.codex import CodexAgent


class TestCodexAgentInit:
    """Test CodexAgent initialization."""

    def test_default_image(self):
        """Default image comes from CODEX_IMAGE env var or fallback."""
        agent = CodexAgent({})
        assert agent.agent_type == "codex"
        # The image should be set (either from env or default)
        assert agent.image is not None

    def test_custom_image_from_env(self):
        """CODEX_IMAGE env var overrides default image."""
        with patch.dict(os.environ, {"CODEX_IMAGE": "custom/codex:latest"}):
            agent = CodexAgent({})
            assert agent.image == "custom/codex:latest"

    def test_config_stored(self):
        """Configuration is stored on the agent."""
        config = {"model": "gpt-5.4", "custom_key": "value"}
        agent = CodexAgent(config)
        assert agent.config == config

    def test_agent_type(self):
        """Agent type is 'codex'."""
        agent = CodexAgent({})
        assert agent.agent_type == "codex"


class TestCodexKubernetesDetection:
    """Test Kubernetes environment detection."""

    def test_explicit_true(self):
        """USE_KUBERNETES=true forces Kubernetes mode."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "true"}):
            agent = CodexAgent({})
            assert agent._detect_kubernetes_environment() is True

    def test_explicit_false(self):
        """USE_KUBERNETES=false forces Docker mode."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "false"}):
            agent = CodexAgent({})
            assert agent._detect_kubernetes_environment() is False

    def test_service_host_detection(self):
        """KUBERNETES_SERVICE_HOST triggers k8s detection."""
        with patch.dict(
            os.environ,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "USE_KUBERNETES": ""},
        ):
            agent = CodexAgent({})
            assert agent._detect_kubernetes_environment() is True

    def test_no_k8s_indicators(self):
        """Defaults to Docker when no k8s indicators found."""
        env_clean = {
            "USE_KUBERNETES": "",
            "KUBERNETES_SERVICE_HOST": "",
        }
        with patch.dict(os.environ, env_clean, clear=False):
            with patch("os.path.exists", return_value=False):
                agent = CodexAgent({})
                assert agent._detect_kubernetes_environment() is False


class TestCodexModelResolution:
    """Test model resolution logic in start()."""

    @pytest.mark.asyncio
    async def test_model_identifier_takes_priority(self):
        """model_identifier from AIModel takes priority over agent_config."""
        agent = CodexAgent({})
        context = {
            "model_identifier": "gpt-5.4",
            "agent_config": {"model": "gpt-5.4"},
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with patch.object(
            agent, "_start_docker_container", new_callable=AsyncMock, return_value="cid"
        ) as mock_start:
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)
                call_ctx = mock_start.call_args[0][0]
                assert call_ctx["codex_model"] == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_agent_config_model_fallback(self):
        """Falls back to agent_config.model when model_identifier is absent."""
        agent = CodexAgent({})
        context = {
            "agent_config": {"model": "gpt-3.5-turbo"},
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with patch.object(
            agent, "_start_docker_container", new_callable=AsyncMock, return_value="cid"
        ) as mock_start:
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)
                call_ctx = mock_start.call_args[0][0]
                assert call_ctx["codex_model"] == "gpt-3.5-turbo"

    @pytest.mark.asyncio
    async def test_default_model(self):
        """Falls back to 'gpt-5.4' when nothing is specified."""
        agent = CodexAgent({})
        context = {
            "agent_config": {},
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with patch.object(
            agent, "_start_docker_container", new_callable=AsyncMock, return_value="cid"
        ) as mock_start:
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)
                call_ctx = mock_start.call_args[0][0]
                assert call_ctx["codex_model"] == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_gateway_model_alias_takes_priority(self):
        """Gateway model alias takes priority when gateway transport is enabled."""
        agent = CodexAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_model_alias": "openai/gpt-5.4",
            "model_identifier": "gpt-5.4",
            "agent_config": {"model": "gpt-5.4"},
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with patch.object(
            agent, "_start_docker_container", new_callable=AsyncMock, return_value="cid"
        ) as mock_start:
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)
                call_ctx = mock_start.call_args[0][0]
                assert call_ctx["codex_model"] == "openai/gpt-5.4"


class TestCodexBuildScript:
    """Test _build_codex_script method."""

    def test_script_contains_prompt(self):
        """Generated script contains the escaped prompt."""
        agent = CodexAgent({})
        context = {
            "prompt": "Fix the bug in main.py",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert "Fix the bug in main.py" in script

    def test_script_escapes_special_chars(self):
        """Prompt with special shell characters is properly escaped."""
        agent = CodexAgent({})
        context = {
            "prompt": 'Run `echo "hello $USER"` please',
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        # Backticks, dollar signs, and double quotes should be escaped
        assert "\\`" in script
        assert "\\$" in script

    def test_script_contains_model(self):
        """Generated script uses the configured model."""
        agent = CodexAgent({})
        context = {
            "prompt": "test",
            "codex_model": "gpt-5.4",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert '--model "gpt-5.4"' in script

    def test_script_has_post_exec_sleep_trap(self):
        """Script includes the post-exec debug sleep trap."""
        agent = CodexAgent({})
        context = {
            "prompt": "test",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert "_post_exec_sleep()" in script
        assert "trap _post_exec_sleep EXIT" in script

    def test_script_always_attaches_preloop_mcp(self):
        """Empty allowlist still attaches Preloop MCP. Overhead is tools."""
        agent = CodexAgent({})
        bare = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
            }
        )
        assert "[mcp_servers.preloop]" in bare
        assert "preloop.security.mcp_server" not in bare
        assert "repo-audit" not in bare

    def test_script_contains_codex_exec_command(self):
        """Script runs codex exec with correct flags."""
        agent = CodexAgent({})
        context = {
            "prompt": "test prompt",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert "codex exec" in script
        assert "--skip-git-repo-check" in script
        assert "--yolo" in script

    def test_script_includes_init_commands(self):
        """Script includes git clone init commands when configured."""
        agent = CodexAgent({})
        context = {
            "prompt": "test",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
            "git_clone_config": {
                "repositories": [
                    {
                        "url": "https://github.com/test/repo.git",
                        "clone_path": "/workspace/repo",
                    }
                ]
            },
        }
        script = agent._build_codex_script(context)
        assert "git" in script.lower()


class TestCodexAuthConfig:
    """Test _build_codex_auth_config method."""

    def test_openai_config(self):
        """Standard OpenAI config generates correct auth.json and config.toml."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config("gpt-5.4", "openai", "")
        assert "OPENAI_API_KEY" in auth_block
        assert 'model = "gpt-5.4"' in auth_block
        assert "[mcp_servers.preloop]" in auth_block
        assert "repo-audit" not in auth_block

    def test_custom_provider_config(self):
        """Custom provider generates provider-specific config.toml section."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "claude-sonnet-4-20250514",
            "anthropic",
            "https://api.anthropic.com/v1",
        )
        assert "ANTHROPIC_API_KEY" in auth_block
        assert "anthropic" in auth_block
        assert 'base_url = "https://api.anthropic.com/v1"' in auth_block
        assert auth_block.index("rmcp_client = true") < auth_block.index(
            "[model_providers.anthropic]"
        )
        assert auth_block.index("rmcp_client = true") < auth_block.index(
            "[mcp_servers.preloop]"
        )
        assert 'wire_api = "chat"' in auth_block

    def test_preloop_gateway_provider_uses_responses_wire_api(self):
        """The Preloop gateway receives Codex Responses API requests."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "deepseek/deepseek-v4-pro", "preloop", "http://preloop-api:8000/openai/v1"
        )
        assert "PRELOOP_API_KEY" in auth_block
        assert 'base_url = "http://preloop-api:8000/openai/v1"' in auth_block
        assert 'wire_api = "responses"' in auth_block

    def test_direct_deepseek_provider_uses_chat_wire_api(self):
        """DeepSeek direct mode should not ask Codex to call /responses."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "deepseek-v4-pro", "deepseek", "https://api.deepseek.com/v1"
        )
        assert "DEEPSEEK_API_KEY" in auth_block
        assert 'base_url = "https://api.deepseek.com/v1"' in auth_block
        assert 'wire_api = "chat"' in auth_block

    def test_custom_provider_no_endpoint(self):
        """Custom provider without endpoint omits base_url."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "claude-sonnet-4-20250514", "anthropic", ""
        )
        assert "base_url" not in auth_block

    def test_gateway_script_uses_openai_compatible_gateway_url(self):
        """Codex should use the OpenAI-compatible gateway endpoint for any model."""
        agent = CodexAgent({})
        script = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "model_gateway_enabled": True,
                "model_gateway_provider": "preloop",
                "model_gateway_model_alias": "google/gemini-2.5-pro",
                "model_gateway_url": "https://review.preloop.ai/gemini/v1beta",
            }
        )
        assert 'base_url = "https://review.preloop.ai/openai/v1"' in script
        assert 'model = "google/gemini-2.5-pro"' in script


class TestCodexPrepareEnvironment:
    """Test _prepare_environment method."""

    @pytest.mark.asyncio
    async def test_openai_api_key(self):
        """OpenAI provider sets OPENAI_API_KEY."""
        agent = CodexAgent({})
        context = {
            "model_api_key": "sk-test-key",
            "model_provider": "openai",
        }
        env = await agent._prepare_environment(context)
        assert env["OPENAI_API_KEY"] == "sk-test-key"

    @pytest.mark.asyncio
    async def test_custom_provider_api_key(self):
        """Custom provider sets both custom and OPENAI_API_KEY."""
        agent = CodexAgent({})
        context = {
            "model_api_key": "ant-test-key",
            "model_provider": "anthropic",
        }
        env = await agent._prepare_environment(context)
        assert env["ANTHROPIC_API_KEY"] == "ant-test-key"
        assert env["OPENAI_API_KEY"] == "ant-test-key"

    @pytest.mark.asyncio
    async def test_gateway_token_sets_gateway_env(self):
        """Gateway-enabled execution uses the short-lived gateway token."""
        agent = CodexAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_token": "gw-token-123",
        }
        env = await agent._prepare_environment(context)
        assert env["PRELOOP_API_KEY"] == "gw-token-123"
        assert env["OPENAI_API_KEY"] == "gw-token-123"
        assert env["PRELOOP_MODEL_GATEWAY_TOKEN"] == "gw-token-123"

    @pytest.mark.asyncio
    async def test_gateway_provider_env_name_is_shell_safe(self):
        """Gateway provider adapter names may contain hyphens."""
        agent = CodexAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_provider": "openai-codex",
            "model_gateway_token": "gw-token-123",
        }
        env = await agent._prepare_environment(context)
        assert env["OPENAI_CODEX_API_KEY"] == "gw-token-123"
        assert "OPENAI-CODEX_API_KEY" not in env

    @pytest.mark.asyncio
    async def test_language_runtime_env_vars(self):
        """Codex sets language runtime version env vars."""
        agent = CodexAgent({})
        context = {"model_provider": "openai"}
        env = await agent._prepare_environment(context)
        assert "CODEX_ENV_PYTHON_VERSION" in env
        assert "CODEX_ENV_NODE_VERSION" in env

    @pytest.mark.asyncio
    async def test_default_mcp_timeout(self):
        """Default MCP timeout is 600 seconds."""
        agent = CodexAgent({})
        context = {"model_provider": "openai"}
        env = await agent._prepare_environment(context)
        assert env["MCP_TOOL_TIMEOUT"] == "600"
        assert context["_mcp_tool_timeout"] == 600


class TestCodexDynamicModelListing:
    """Codex CLI populates its model picker from GET /models.

    The Preloop gateway already includes a top-level ``models`` array in
    the GET /models response (see openai_gateway.py list_models) which
    Codex CLI's model-manager deserializes.  This means Codex is already
    dynamic: enabling a new model on the account makes it appear in
    ``GET /models`` immediately without config regeneration.

    These tests assert the contract so regressions are caught.
    """

    def test_codex_config_has_no_static_model_list(self):
        """Codex auth config does not embed a models list.

        Unlike OpenCode (which writes a ``models`` map in opencode.json),
        Codex's config.toml and auth.json contain only the primary model
        and provider setup.  The model picker is populated at runtime via
        ``GET /models``.
        """
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config("gpt-5", "openai", "")
        # config.toml has model = "..." but no [models] section
        assert 'model = "gpt-5"' in auth_block
        assert "[models]" not in auth_block

    def test_codex_custom_provider_has_no_static_model_list(self):
        """Custom-provider config.toml also has no embedded model list."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "claude-sonnet-4", "preloop", "https://gw.example/openai/v1"
        )
        assert 'model = "claude-sonnet-4"' in auth_block
        assert "[models]" not in auth_block
