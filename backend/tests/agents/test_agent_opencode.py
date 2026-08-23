"""Tests for OpenCode agent implementation."""

import os
from unittest.mock import patch, AsyncMock

import pytest

from preloop.agents.opencode import OpenCodeAgent


class TestOpenCodeAgentInit:
    """Test OpenCodeAgent initialization."""

    def test_default_image(self):
        """Default image is docker/sandbox-templates:opencode."""
        agent = OpenCodeAgent({})
        assert agent.agent_type == "opencode"
        assert agent.image == "docker/sandbox-templates:opencode"

    def test_custom_image_from_env(self):
        """OPENCODE_IMAGE env var overrides default image."""
        with patch.dict(os.environ, {"OPENCODE_IMAGE": "custom/opencode:latest"}):
            agent = OpenCodeAgent({})
            assert agent.image == "custom/opencode:latest"

    def test_config_stored(self):
        """Configuration is stored on the agent."""
        config = {"model": "claude-sonnet-4-20250514", "custom": "value"}
        agent = OpenCodeAgent(config)
        assert agent.config == config

    def test_agent_type(self):
        """Agent type is 'opencode'."""
        agent = OpenCodeAgent({})
        assert agent.agent_type == "opencode"


class TestOpenCodeKubernetesDetection:
    """Test Kubernetes environment detection."""

    def test_explicit_true(self):
        """USE_KUBERNETES=true forces Kubernetes mode."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "true"}):
            agent = OpenCodeAgent({})
            assert agent._detect_kubernetes_environment() is True

    def test_explicit_false(self):
        """USE_KUBERNETES=false forces Docker mode."""
        with patch.dict(os.environ, {"USE_KUBERNETES": "false"}):
            agent = OpenCodeAgent({})
            assert agent._detect_kubernetes_environment() is False

    def test_service_host_detection(self):
        """KUBERNETES_SERVICE_HOST triggers k8s detection."""
        with patch.dict(
            os.environ,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "USE_KUBERNETES": ""},
        ):
            agent = OpenCodeAgent({})
            assert agent._detect_kubernetes_environment() is True

    def test_no_k8s_indicators(self):
        """Defaults to Docker when no k8s indicators found."""
        env_clean = {
            "USE_KUBERNETES": "",
            "KUBERNETES_SERVICE_HOST": "",
        }
        with patch.dict(os.environ, env_clean, clear=False):
            with patch("os.path.exists", return_value=False):
                agent = OpenCodeAgent({})
                assert agent._detect_kubernetes_environment() is False


class TestOpenCodeModelResolution:
    """Test model resolution logic in start()."""

    @pytest.mark.asyncio
    async def test_model_identifier_takes_priority(self):
        """model_identifier from AIModel takes priority."""
        agent = OpenCodeAgent({})
        context = {
            "model_identifier": "claude-sonnet-4-20250514",
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
                assert call_ctx["opencode_model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_agent_config_model_fallback(self):
        """Falls back to agent_config.model when model_identifier is absent."""
        agent = OpenCodeAgent({})
        context = {
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
                assert call_ctx["opencode_model"] == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_no_model_raises_error(self):
        """Raises ValueError when no model is specified."""
        agent = OpenCodeAgent({})
        context = {
            "agent_config": {},
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with pytest.raises(ValueError, match="No model specified"):
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)

    @pytest.mark.asyncio
    async def test_no_model_empty_context_raises_error(self):
        """Raises ValueError when execution context has no model at all."""
        agent = OpenCodeAgent({})
        context = {
            "execution_id": "test-123",
            "flow_id": "flow-1",
        }
        with pytest.raises(ValueError, match="No model specified"):
            with patch.object(agent, "use_kubernetes", False):
                await agent.start(context)

    @pytest.mark.asyncio
    async def test_gateway_model_alias_takes_priority(self):
        """Gateway model alias takes priority when gateway transport is enabled."""
        agent = OpenCodeAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_model_alias": "anthropic/claude-sonnet-4-5",
            "model_identifier": "claude-sonnet-4-20250514",
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
                assert call_ctx["opencode_model"] == "anthropic/claude-sonnet-4-5"


class TestOpenCodeBuildScript:
    """Test _build_opencode_script method."""

    def test_script_contains_prompt(self):
        """Generated script contains the base64-encoded prompt."""
        import base64

        agent = OpenCodeAgent({})
        prompt = "Add unit tests for the API"
        context = {
            "prompt": prompt,
            "opencode_model": "claude-sonnet-4-20250514",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        # Prompt is base64-encoded for shell safety
        expected_b64 = base64.b64encode(prompt.encode()).decode()
        assert expected_b64 in script

    def test_script_contains_model(self):
        """Script logs the configured model."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "gpt-5.4",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        assert "gpt-5.4" in script

    def test_script_has_post_exec_sleep_trap(self):
        """Script includes the post-exec debug sleep trap."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        assert "_post_exec_sleep()" in script
        assert "trap _post_exec_sleep EXIT" in script

    def test_script_runs_opencode(self):
        """Script runs opencode run with current non-interactive flags."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        assert "opencode run" in script
        assert "--model anthropic/model-1" in script
        assert "--format json" in script
        assert "--dangerously-skip-permissions" in script
        assert "opencode-json-log-filter.js" in script
        assert "node /tmp/opencode-json-log-filter.js" in script
        assert "python -u /tmp/opencode-json-log-filter.py" not in script
        assert "function eventName(event) {" in script
        assert "readline.createInterface({ input: process.stdin })" in script
        assert r"text.split(/\r?\n/)" in script
        assert "text.split(/\r?\n/)" not in script
        assert "OPENCODE_EXIT_CODE=${PIPE_CODES[0]}" in script
        assert "FLOW_EXECUTION_SUCCESS" in script
        assert "OpenCode command failed; see CLI output above." in script
        assert 'echo "FLOW_EXECUTION_SUCCESS"' not in script
        assert "-y" not in script
        # Should NOT use non-existent --non-interactive flag
        assert "--non-interactive" not in script

    def test_script_runs_gateway_model_with_provider_qualified_model(self):
        """Gateway models should be invoked with the OpenCode provider prefix."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "deepseek/deepseek-v4-pro",
            "model_provider": "deepseek",
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_url": "http://release-preloop-api:80/openai/v1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }

        script = agent._build_opencode_script(context)

        assert "--model preloop/deepseek/deepseek-v4-pro" in script
        assert "--dangerously-skip-permissions" in script

    def test_script_installs_opencode(self):
        """Script installs opencode-ai via npm."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        assert "npm install -g opencode-ai" in script

    def test_script_writes_config(self):
        """Script writes opencode.json config file."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_opencode_script(context)
        assert "opencode.json" in script
        assert "OPENCODE_CONFIG_EOF" in script
        assert '"share": "disabled"' in script
        assert '"enabled_providers": [' in script
        assert '"small_model": "anthropic/model-1"' in script

    def test_no_model_raises_error(self):
        """Raises ValueError when no model is in context."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        with pytest.raises(ValueError, match="No model specified"):
            agent._build_opencode_script(context)


class TestOpenCodeBuildConfig:
    """Test _build_opencode_config method."""

    def test_basic_config_structure(self):
        """Config has required schema, autoupdate, and mcp keys."""
        agent = OpenCodeAgent({})
        config = agent._build_opencode_config(
            "claude-sonnet-4-20250514", "anthropic", {}, 600000
        )
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert config["autoupdate"] is False
        assert config["share"] == "disabled"
        assert config["small_model"] == "anthropic/claude-sonnet-4-20250514"
        assert config["enabled_providers"] == ["anthropic"]
        assert "mcp" in config

    def test_mcp_server_config(self):
        """MCP config has preloop server with correct structure."""
        agent = OpenCodeAgent({})
        config = agent._build_opencode_config(
            "model-1",
            "anthropic",
            {"allowed_mcp_servers": ["preloop-mcp"]},
            600000,
        )
        preloop = config["mcp"]["preloop"]
        assert preloop["type"] == "remote"
        assert preloop["url"] == "$PRELOOP_MCP_URL"
        assert preloop["headers"]["Authorization"] == "Bearer $PRELOOP_API_TOKEN"
        assert preloop["timeout"] == 600000
        assert preloop["enabled"] is True

    def test_custom_endpoint(self):
        """Custom endpoint adds provider config with npm, options.baseURL, models."""
        agent = OpenCodeAgent({})
        context = {"model_endpoint": "https://custom.api.com/v1"}
        config = agent._build_opencode_config("model-1", "customllm", context, 600000)
        assert "provider" in config
        provider = config["provider"]["customllm"]
        assert provider["npm"] == "@ai-sdk/openai-compatible"
        assert provider["options"]["baseURL"] == "https://custom.api.com/v1"
        assert provider["options"]["apiKey"] == "$OPENAI_API_KEY"
        assert "model-1" in provider["models"]

    def test_llm_request_timeout_aligned_with_gateway_stack(self):
        """The provider LLM timeout must sit at 600s, not OpenCode-fatal 120s.

        OpenCode aborts any LLM request after ``provider.options.timeout`` ms
        ("The operation timed out."). 120s was below real upstream latency
        (slow-but-successful gateway calls finished after the CLI had already
        exited), so the timeout must align with the MCP tool timeout (600s)
        and stay under the gateway proxy readTimeout (900s).
        """
        agent = OpenCodeAgent({})
        context = {"model_endpoint": "https://custom.api.com/v1"}
        config = agent._build_opencode_config("model-1", "customllm", context, 600000)
        options = config["provider"]["customllm"]["options"]
        assert options["timeout"] == 600000
        assert options["chunkTimeout"] == 600000

    def test_llm_request_timeout_env_override(self):
        """OPENCODE_LLM_TIMEOUT_SEC overrides the 600s default."""
        agent = OpenCodeAgent({})
        context = {"model_endpoint": "https://custom.api.com/v1"}
        with patch.dict(os.environ, {"OPENCODE_LLM_TIMEOUT_SEC": "750"}):
            config = agent._build_opencode_config(
                "model-1", "customllm", context, 600000
            )
        options = config["provider"]["customllm"]["options"]
        assert options["timeout"] == 750000
        assert options["chunkTimeout"] == 750000

    def test_llm_request_timeout_invalid_override_falls_back(self):
        """A malformed OPENCODE_LLM_TIMEOUT_SEC must not fail the run."""
        agent = OpenCodeAgent({})
        context = {"model_endpoint": "https://custom.api.com/v1"}
        for bad in ("ninety", "", "-5", "0"):
            with patch.dict(os.environ, {"OPENCODE_LLM_TIMEOUT_SEC": bad}):
                config = agent._build_opencode_config(
                    "model-1", "customllm", context, 600000
                )
            options = config["provider"]["customllm"]["options"]
            assert options["timeout"] == 600000, bad
            assert options["chunkTimeout"] == 600000, bad

    def test_gateway_endpoint_uses_preloop_provider(self):
        """Gateway-enabled config uses gateway provider and URL."""
        agent = OpenCodeAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_url": "http://gateway.internal/gemini/v1beta",
            "model_gateway_provider": "preloop",
        }
        config = agent._build_opencode_config(
            "openai/gpt-5", "anthropic", context, 600000
        )
        provider = config["provider"]["preloop"]
        assert provider["options"]["baseURL"] == "http://gateway.internal/openai/v1"
        assert "openai/gpt-5" in provider["models"]
        assert config["model"] == "preloop/openai/gpt-5"
        assert config["small_model"] == "preloop/openai/gpt-5"
        assert config["enabled_providers"] == ["preloop"]

    def test_gateway_strips_duplicate_provider_prefix_in_models_map(self):
        """If alias already includes ``preloop/``, models keys stay provider-local."""
        agent = OpenCodeAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_url": "http://gateway.internal/openai/v1",
            "model_gateway_provider": "preloop",
        }
        config = agent._build_opencode_config(
            "preloop/openai/gpt-5", "anthropic", context, 600000
        )
        assert config["model"] == "preloop/openai/gpt-5"
        assert config["provider"]["preloop"]["models"] == {
            "openai/gpt-5": {"name": "preloop/openai/gpt-5"}
        }

    def test_non_gateway_provider_fallback_uses_effective_provider(self):
        """Direct-provider config derives provider consistently for env fallback."""
        agent = OpenCodeAgent({})
        with patch.dict(os.environ, {"GOOGLE_API_BASE": "https://google.example/v1"}):
            config = agent._build_opencode_config(
                "gemini-2.5-pro", "google", {}, 600000
            )

        assert config["model"] == "google/gemini-2.5-pro"
        assert config["provider"]["google"]["options"]["baseURL"] == (
            "https://google.example/v1"
        )

    def test_no_endpoint_no_provider_config(self):
        """No provider config when no custom endpoint."""
        agent = OpenCodeAgent({})
        config = agent._build_opencode_config("model-1", "openai", {}, 600000)
        assert "provider" not in config

    def test_timeout_in_milliseconds(self):
        """MCP timeout is in milliseconds."""
        agent = OpenCodeAgent({})
        config = agent._build_opencode_config(
            "model-1",
            "anthropic",
            {"allowed_mcp_servers": ["preloop-mcp"]},
            900000,
        )
        assert config["mcp"]["preloop"]["timeout"] == 900000

    def test_empty_allowlist_starts_no_mcp(self):
        """Ordinary agents do not load Preloop or repo-audit MCP."""
        agent = OpenCodeAgent({})
        config = agent._build_opencode_config("model-1", "anthropic", {}, 600000)
        assert config["mcp"] == {}


class TestOpenCodePrepareEnvironment:
    """Test _prepare_environment method."""

    @pytest.mark.asyncio
    async def test_anthropic_api_key(self):
        """Anthropic provider sets ANTHROPIC_API_KEY and OPENAI_API_KEY."""
        agent = OpenCodeAgent({})
        context = {
            "model_api_key": "ant-key-123",
            "model_provider": "anthropic",
        }
        env = await agent._prepare_environment(context)
        assert env["ANTHROPIC_API_KEY"] == "ant-key-123"
        assert env["OPENAI_API_KEY"] == "ant-key-123"  # fallback

    @pytest.mark.asyncio
    async def test_openai_api_key(self):
        """OpenAI provider sets only OPENAI_API_KEY (no fallback duplication)."""
        agent = OpenCodeAgent({})
        context = {
            "model_api_key": "sk-key-123",
            "model_provider": "openai",
        }
        env = await agent._prepare_environment(context)
        assert env["OPENAI_API_KEY"] == "sk-key-123"

    @pytest.mark.asyncio
    async def test_no_hardcoded_home(self):
        """HOME should NOT be hardcoded."""
        agent = OpenCodeAgent({})
        context = {"model_provider": "openai"}
        env = await agent._prepare_environment(context)
        assert "HOME" not in env

    @pytest.mark.asyncio
    async def test_default_mcp_timeout(self):
        """Default MCP timeout is 600 seconds."""
        agent = OpenCodeAgent({})
        context = {"model_provider": "openai"}
        env = await agent._prepare_environment(context)
        assert env["MCP_TOOL_TIMEOUT"] == "600"
        assert context["_mcp_tool_timeout"] == 600

    @pytest.mark.asyncio
    async def test_no_api_key_when_missing(self):
        """No API key env vars when model_api_key is absent."""
        agent = OpenCodeAgent({})
        context = {"model_provider": "anthropic"}
        env = await agent._prepare_environment(context)
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env

    @pytest.mark.asyncio
    async def test_google_provider_key(self):
        """Google provider sets GOOGLE_API_KEY."""
        agent = OpenCodeAgent({})
        context = {
            "model_api_key": "google-key-123",
            "model_provider": "google",
        }
        env = await agent._prepare_environment(context)
        assert env["GOOGLE_API_KEY"] == "google-key-123"
        assert env["OPENAI_API_KEY"] == "google-key-123"

    @pytest.mark.asyncio
    async def test_gateway_token_sets_gateway_env(self):
        """Gateway-enabled execution uses the short-lived gateway token."""
        agent = OpenCodeAgent({})
        context = {
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_token": "gw-token-123",
        }
        env = await agent._prepare_environment(context)
        assert env["PRELOOP_API_KEY"] == "gw-token-123"
        assert env["OPENAI_API_KEY"] == "gw-token-123"
        assert env["PRELOOP_MODEL_GATEWAY_TOKEN"] == "gw-token-123"


class TestOpenCodeKubernetesStartup:
    """Test _start_kubernetes_pod sets correct command and args."""

    @pytest.mark.asyncio
    async def test_k8s_sets_container_command_and_args(self):
        """K8s path sets _container_command=['/bin/bash'] and _container_args=['-c', script]."""
        agent = OpenCodeAgent({})
        context = {
            "prompt": "test prompt",
            "opencode_model": "claude-sonnet-4-20250514",
            "model_identifier": "claude-sonnet-4-20250514",
            "model_provider": "anthropic",
            "model_api_key": "key-123",
            "execution_id": "exec-k8s-1",
            "flow_id": "flow-1",
            "flow_name": "test-flow",
        }
        # Mock the parent _start_kubernetes_pod to capture the context
        with patch(
            "preloop.agents.container.ContainerAgentExecutor._start_kubernetes_pod",
            new_callable=AsyncMock,
            return_value="job-name",
        ) as mock_parent:
            await agent._start_kubernetes_pod(context)
            call_ctx = mock_parent.call_args[0][0]
            assert call_ctx["_container_command"] == ["/bin/bash"]
            assert call_ctx["_container_args"][0] == "-c"
            assert len(call_ctx["_container_args"]) == 2


class TestOpenCodeStderrCapture:
    """OpenCode failures must be diagnosable from the execution log.

    Staging PR-reviewer runs died with "OpenCode CLI exited with code: 1" and
    nothing else: opencode's internal logger writes to log files (not stderr)
    unless --print-logs is passed, and stderr was not routed through the JSON
    log filter, so fatal errors never reached the captured log stream
    (issue #212).
    """

    def _context(self):
        return {
            "prompt": "test",
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }

    def test_script_enables_opencode_logging(self):
        """opencode run must print its internal logs to stderr."""
        agent = OpenCodeAgent({})
        script = agent._build_opencode_script(self._context())
        assert "--print-logs" in script
        assert "--log-level WARN" in script

    def test_script_routes_stderr_through_log_filter(self):
        """stderr must be merged into the pipe feeding the JSON log filter.

        The filter passes non-JSON lines through verbatim, so plain-text
        stderr log lines reach the captured log stream in order.
        """
        agent = OpenCodeAgent({})
        script = agent._build_opencode_script(self._context())
        assert "2>&1 | node /tmp/opencode-json-log-filter.js" in script
