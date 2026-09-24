"""Tests for Codex agent implementation."""

import logging
import os
from unittest.mock import patch, AsyncMock

import pytest

from preloop.agents.codex import CodexAgent
from preloop.services.model_context_limits import limits_for_execution
from preloop.utils.execve_limits import PROMPT_FILE_PATH


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

    def test_script_reads_the_prompt_from_a_file_not_from_its_own_text(self):
        """The prompt is delivered out of band, never inlined in the script.

        Inlining it made the script grow with the trigger payload, and one
        execve string cannot exceed MAX_ARG_STRLEN.
        """
        agent = CodexAgent({})
        context = {
            "prompt": "Fix the bug in main.py",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert "Fix the bug in main.py" not in script
        assert f'cat "{PROMPT_FILE_PATH}" | codex exec' in script

    def test_shell_metacharacters_never_reach_the_script(self):
        """A prompt full of shell syntax leaves no trace in the script.

        There is nothing left to escape: the prompt travels as base64 in the
        environment, so backticks and dollar signs cannot be misquoted into
        command substitution.
        """
        agent = CodexAgent({})
        prompt = 'Run `echo "hello $USER"` please'
        context = {
            "prompt": prompt,
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        script = agent._build_codex_script(context)
        assert 'echo "hello $USER"' not in script
        assert (
            "`"
            not in script.split("PRELOOP_AGENT_EXEC_START")[0].split("codex --version")[
                -1
            ]
        )

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

    def test_script_omits_preloop_mcp_when_allowlists_are_empty(self):
        """Empty allowlists must not open an MCP session."""
        agent = CodexAgent({})
        bare = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
            }
        )
        assert "[mcp_servers.preloop]" not in bare
        assert "rmcp_client = true" not in bare
        assert 'echo "MCP Server: not attached"' in bare
        assert "preloop.security.mcp_server" not in bare
        assert "repo-audit" not in bare

    def test_script_attaches_preloop_mcp_when_a_tool_is_allowed(self):
        """A non-empty allowlist still configures the Preloop MCP server."""
        agent = CodexAgent({})
        script = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "allowed_mcp_tools": [{"name": "get_pull_request"}],
            }
        )
        assert "rmcp_client = true" in script
        assert "[mcp_servers.preloop]" in script
        assert 'echo "MCP Server: $PRELOOP_MCP_URL"' in script

    def test_read_only_sandbox_does_not_pass_yolo(self):
        """sandbox_type read-only is the platform shell lock."""
        agent = CodexAgent({})
        assert agent.live_nudge_command is None
        script = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "agent_config": {"sandbox_type": "read-only"},
                "completion_nudge_enabled": True,
            }
        )
        assert script.count("--sandbox read-only") >= 3
        assert script.count("--disable shell_tool") >= 3
        assert 'approval_policy = "never"' in script
        assert 'sandbox_mode = "read-only"' in script
        assert "--yolo" not in script
        assert "--sandbox read-only" in agent.live_nudge_command
        assert "--disable shell_tool" in agent.live_nudge_command
        assert "--yolo" not in agent.live_nudge_command

    def test_exec_sandbox_keeps_yolo(self):
        """The preset default sandbox_type exec still bypasses the sandbox."""
        agent = CodexAgent({})
        script = agent._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "agent_config": {"sandbox_type": "exec"},
            }
        )
        assert "--yolo" in script
        assert "--sandbox read-only" not in script
        assert "approval_policy" not in script

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

    def test_browser_mcp_fragment_is_appended_after_config_write(self):
        """Codex replaces config.toml; the browser fragment is attached after."""
        agent = CodexAgent({})
        script = agent._build_codex_script(
            {"prompt": "test", "execution_id": "exec-1", "flow_name": "test-flow"}
        )
        assert script.index("cat > ~/.codex/config.toml") < script.index(
            "preloop-browser-mcp.toml"
        )


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
        assert "request_max_retries = 4" in auth_block
        assert "stream_max_retries = 5" in auth_block

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
        assert "request_max_retries = 4" in auth_block
        assert "stream_max_retries = 5" in auth_block

    def test_openai_keeps_builtin_retry_defaults(self):
        """Native OpenAI must not emit a partial [model_providers.openai]."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config("gpt-5.4", "openai", "")
        assert "[model_providers.openai]" not in auth_block
        assert "request_max_retries" not in auth_block
        assert "stream_max_retries" not in auth_block
        assert "stream_idle_timeout_ms" not in auth_block

    def test_attach_mcp_false_omits_the_server(self):
        """An empty allowlist must not write an MCP client into config.toml."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "deepseek-v4-flash",
            "openrouter",
            "https://openrouter.ai/api/v1",
            attach_mcp=False,
        )
        assert "rmcp_client" not in auth_block
        assert "[mcp_servers.preloop]" not in auth_block
        assert 'model = "deepseek-v4-flash"' in auth_block
        assert "request_max_retries = 4" in auth_block

    def test_shell_lock_on_custom_provider_precedes_the_provider_block(self):
        """Read-only config pins approval and sandbox before the provider."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "deepseek-v4-flash",
            "openrouter",
            "https://openrouter.ai/api/v1",
            shell_locked=True,
        )
        assert auth_block.index('approval_policy = "never"') < auth_block.index(
            "[model_providers.openrouter]"
        )
        assert auth_block.index('sandbox_mode = "read-only"') < auth_block.index(
            "[model_providers.openrouter]"
        )

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


class TestCodexCliSession:
    """Native CLI session capture/restore blocks in the generated script."""

    def _context(self, **extra):
        context = {
            "prompt": "test",
            "codex_model": "gpt-5.4",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
        context.update(extra)
        return context

    def test_cold_start_has_no_resume_args(self):
        """A non-resume run sets empty resume args and still captures/packs."""
        script = CodexAgent({})._build_codex_script(self._context())
        assert "PRELOOP_CLI_SESSION_ID=''" in script
        assert 'CODEX_RESUME_ARGS=""' in script
        assert 'echo "PRELOOP_AGENT_SESSION codex $_pl_codex_sid"' in script
        assert "/workspace/.preloop-agent-session/codex" in script
        assert "--exclude=auth.json" in script

    def test_run_command_expands_resume_args(self):
        script = CodexAgent({})._build_codex_script(self._context())
        assert "codex exec $CODEX_RESUME_ARGS --skip-git-repo-check" in script

    def test_resume_metadata_uses_resume_subcommand(self):
        script = CodexAgent({})._build_codex_script(
            self._context(
                trigger_event_data={
                    "_resume": {
                        "cli_session": {
                            "agent_type": "codex",
                            "session_id": "0f0e1d2c-3b4a-4568-8778-aabbccddeeff",
                        }
                    }
                }
            )
        )
        assert "PRELOOP_CLI_SESSION_ID='0f0e1d2c-3b4a-4568-8778-aabbccddeeff'" in script
        assert 'CODEX_RESUME_ARGS="resume $PRELOOP_CLI_SESSION_ID"' in script

    def test_capture_block_extracts_rollout_uuid(self):
        script = CodexAgent({})._build_codex_script(self._context())
        assert "capture_codex_session_id" in script
        assert 'payload.get("forked_from_id")' in script
        assert "expected exactly one Codex root session" in script
        assert "rollout-*.jsonl" in script
        assert "/tmp/preloop-cli-session-id" not in script
        assert 'find "$CODEX_HOME/sessions"' not in script
        assert "sort | tail -n 1" not in script
        assert "except Exception:" in script
        assert "<<'PRELOOP_CODEX_CAPTURE_PY' || true" in script

    def test_stream_recovery_resumes_captured_parent_before_publication(self):
        script = CodexAgent({})._build_codex_script(self._context())
        exec_at = script.index('echo "PRELOOP_AGENT_EXEC_START"')
        tail = script[exec_at:]
        assert tail.index("capture_codex_session_id") < tail.index(
            "# Recover only this captured conversation"
        )
        assert tail.index("# Recover only this captured conversation") < tail.index(
            "_pl_pack_cli_session"
        )
        assert 'codex exec resume "$_pl_recovery_sid"' in script
        assert "codex exec resume --last" not in script
        assert "timeout -k 5 600" in script
        assert "130|137|143" in script

    def test_checkpoint_thread_keeps_fail_closed_parent_capture(self):
        script = CodexAgent({})._build_codex_script(
            self._context(
                trigger_event_data={
                    "_session_thread_id": "thread-1",
                    "_resume": {
                        "cli_session": {
                            "agent_type": "codex",
                            "session_id": "0f0e1d2c-3b4a-4568-8778-aabbccddeeff",
                        }
                    },
                }
            )
        )
        assert "capture_codex_session_id" in script
        assert "<<'PRELOOP_CODEX_CAPTURE_PY' || true" in script
        assert "python3 /tmp/preloop-native-session.py capture" not in script

    def test_confirmation_nudge_never_restores(self):
        script = CodexAgent({})._build_codex_script(
            self._context(
                confirmation_nudge=True,
                trigger_event_data={
                    "_resume": {
                        "cli_session": {
                            "agent_type": "codex",
                            "session_id": "0f0e1d2c-3b4a-4568-8778-aabbccddeeff",
                        }
                    }
                },
            )
        )
        assert "PRELOOP_CLI_SESSION_RESTORED" not in script
        assert "PRELOOP_CLI_SESSION_ID=" not in script
        assert 'echo "PRELOOP_AGENT_SESSION codex $_pl_codex_sid"' in script

    def test_mismatched_agent_type_starts_cold(self):
        script = CodexAgent({})._build_codex_script(
            self._context(
                trigger_event_data={
                    "_resume": {
                        "cli_session": {
                            "agent_type": "opencode",
                            "session_id": "ses_ab12cd34",
                        }
                    }
                }
            )
        )
        assert "PRELOOP_CLI_SESSION_ID=''" in script
        # No recorded id is ever embedded for a mismatched agent type.
        assert "PRELOOP_CLI_SESSION_ID='ses_" not in script

    def test_embedded_archive_is_decoded_into_the_workspace(self):
        import base64

        script = CodexAgent({})._build_codex_script(
            self._context(cli_session_restore_archive=b"tar-gz-bytes")
        )
        assert "base64 -d | tar xzf -" in script
        encoded = script.split("echo '")[1].split("'")[0]
        assert base64.b64decode(encoded) == b"tar-gz-bytes"


class TestCodexContextLimits:
    """config.toml tells Codex the window it is actually working in (#851).

    Without these keys Codex assumes a conservative window, compacts long
    before it has to, and then re-reads what it just dropped. Preloop knows
    the real numbers from the model row or the vendored price catalog.
    """

    def test_config_carries_both_limits_from_the_catalog(self):
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "gpt-5.4",
            "openai",
            "",
            limits_for_execution({"model_identifier": "gpt-5.4"}),
        )
        assert "model_context_window = 1050000" in auth_block
        assert "model_max_output_tokens = 128000" in auth_block

    def test_the_model_row_overrides_the_catalog(self):
        """An operator's provisioned deployment can be smaller than the
        public model."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "gpt-5.4",
            "preloop",
            "https://gw.example.com/openai/v1",
            limits_for_execution(
                {
                    "model_identifier": "gpt-5.4",
                    "model_parameters": {"context_window": 262144},
                }
            ),
        )
        assert "model_context_window = 262144" in auth_block
        # The ceiling the row said nothing about still comes from the catalog.
        assert "model_max_output_tokens = 128000" in auth_block

    def test_an_unknown_model_gets_no_limits_and_one_info_line(self, caplog):
        """Nothing is guessed: Codex keeps its own defaults."""
        agent = CodexAgent({})
        with caplog.at_level(logging.INFO, logger="preloop.agents.codex"):
            auth_block = agent._build_codex_auth_config(
                "nobody-has-heard-of-this",
                "example",
                "https://api.example.com/v1",
                limits_for_execution({"model_identifier": "nobody-has-heard-of-this"}),
            )
        assert "model_context_window" not in auth_block
        assert "model_max_output_tokens" not in auth_block
        unknown = [
            record
            for record in caplog.records
            if "No context window or output ceiling known" in record.getMessage()
        ]
        assert len(unknown) == 1

    def test_a_caller_that_passes_no_limits_changes_nothing(self):
        """Existing callers keep the config they had."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config("gpt-5.4", "openai", "")
        assert "model_context_window" not in auth_block
        assert 'model = "gpt-5.4"' in auth_block

    def test_the_generated_script_carries_the_limits(self):
        """End to end: the window reaches the config.toml heredoc."""
        script = CodexAgent({})._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "model_identifier": "gpt-5.4",
                "model_provider": "openai",
            }
        )
        assert "model_context_window = 1050000" in script
        assert "model_max_output_tokens = 128000" in script

    def test_limits_sit_under_the_model_line_and_above_rmcp_client(self):
        """A key in the wrong table is a key Codex reads as someone else's."""
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "gpt-5.4",
            "preloop",
            "https://gw.example.com/openai/v1",
            limits_for_execution({"model_identifier": "gpt-5.4"}),
        )
        assert auth_block.index('model = "gpt-5.4"') < auth_block.index(
            "model_context_window"
        )
        assert auth_block.index("model_context_window") < auth_block.index(
            "rmcp_client = true"
        )
        assert auth_block.index("model_max_output_tokens") < auth_block.index(
            "[model_providers.preloop]"
        )


class TestCodexReasoningEffort:
    """A routed effort reaches Codex through config.toml (#851).

    Codex takes its effort from its config file, not from the request, so a
    flow-level "think harder on this label" has to be written here.
    """

    def test_a_routed_effort_is_written(self):
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "gpt-5.4", "openai", "", None, "high"
        )
        assert 'model_reasoning_effort = "high"' in auth_block

    def test_no_routed_effort_leaves_the_model_default(self):
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config("gpt-5.4", "openai", "")
        assert "model_reasoning_effort" not in auth_block

    def test_an_effort_codex_does_not_accept_is_dropped(self, caplog):
        """A config file Codex refuses to parse would fail the whole run."""
        agent = CodexAgent({})
        with caplog.at_level(logging.INFO, logger="preloop.agents.codex"):
            auth_block = agent._build_codex_auth_config(
                "gpt-5.4", "openai", "", None, "maximum"
            )
        assert "model_reasoning_effort" not in auth_block
        assert any(
            "Ignoring reasoning effort" in record.getMessage()
            for record in caplog.records
        )

    def test_the_effort_travels_on_the_model_parameters(self):
        """End to end: what the orchestrator wrote reaches the config."""
        script = CodexAgent({})._build_codex_script(
            {
                "prompt": "test",
                "execution_id": "exec-1",
                "flow_name": "test-flow",
                "model_identifier": "gpt-5.4",
                "model_provider": "openai",
                "model_parameters": {"reasoning_effort": "high"},
            }
        )
        assert 'model_reasoning_effort = "high"' in script

    def test_the_effort_sits_beside_the_context_limits(self):
        agent = CodexAgent({})
        auth_block = agent._build_codex_auth_config(
            "gpt-5.4",
            "preloop",
            "https://gw.example.com/openai/v1",
            limits_for_execution({"model_identifier": "gpt-5.4"}),
            "medium",
        )
        assert auth_block.index("model_context_window") < auth_block.index(
            "model_reasoning_effort"
        )
        assert auth_block.index("model_reasoning_effort") < auth_block.index(
            "rmcp_client = true"
        )
