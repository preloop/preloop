"""OpenAI Codex CLI agent implementation."""

import json
import logging
import os
from typing import Any, Dict

from aiodocker.exceptions import DockerError

from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api

from .cli_session import (
    AGENT_SESSION_MARKER,
    build_session_archive_decode_shell,
    build_session_pack_shell,
    build_session_restore_shell,
    resume_cli_session,
)
from .completion_nudge import (
    AGENT_OUTPUT_LOG_PATH,
    NUDGE_PROMPT_PATH,
    build_completion_nudge_block,
    completion_nudge_enabled,
    completion_nudge_timeout_seconds,
)
from .container import ContainerAgentExecutor
from .images import default_agent_image

logger = logging.getLogger(__name__)


class CodexAgent(ContainerAgentExecutor):
    """
    OpenAI Codex CLI agent executor.

    Runs OpenAI's Codex CLI tool (https://github.com/openai/codex) in a Docker
    container for autonomous coding tasks.
    """

    # Codex sessions are one-shot containers, so "resume" is a fresh
    # invocation with prior context — validated for the orchestrator's
    # completion-confirmation round (see AgentExecutor for semantics).
    supports_confirmation_nudge = True

    # `codex exec resume --last` re-enters the rollout the container just
    # recorded, so the completion reminder happens in the same container and
    # workspace instead of starting a second session.
    supports_inplace_completion_nudge = True

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Codex agent.

        Args:
            config: Agent configuration including:
                - model: OpenAI model to use (default: gpt-5.4)
                - custom settings for Codex CLI
        """
        image = default_agent_image("codex") or "ghcr.io/openai/codex-universal:latest"

        # Auto-detect Kubernetes environment or use explicit env var
        use_k8s = self._detect_kubernetes_environment()

        super().__init__(
            agent_type="codex",
            config=config,
            image=image,
            use_kubernetes=use_k8s,
        )

    def _detect_kubernetes_environment(self) -> bool:
        """
        Auto-detect if running in Kubernetes environment.

        Checks for:
        1. Explicit USE_KUBERNETES environment variable
        2. Kubernetes service account token (in-cluster detection)
        3. KUBERNETES_SERVICE_HOST environment variable

        Returns:
            True if Kubernetes environment detected, False otherwise
        """
        # Check explicit environment variable first
        env_value = os.getenv("USE_KUBERNETES", "").lower()
        if env_value == "true":
            logger.info("Kubernetes mode enabled via USE_KUBERNETES=true")
            return True
        elif env_value == "false":
            logger.info("Kubernetes mode disabled via USE_KUBERNETES=false")
            return False

        # Auto-detect: Check for Kubernetes service account token (in-cluster)
        k8s_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        if os.path.exists(k8s_token_path):
            logger.info(
                f"Kubernetes environment detected (found service account token at {k8s_token_path})"
            )
            return True

        # Auto-detect: Check for Kubernetes service host
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            logger.info(
                "Kubernetes environment detected (KUBERNETES_SERVICE_HOST present)"
            )
            return True

        # Default to Docker if no Kubernetes indicators found
        logger.info("No Kubernetes environment detected, defaulting to Docker mode")
        return False

    async def start(self, execution_context: Dict[str, Any]) -> str:
        """
        Start Codex agent with specialized configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID or pod name
        """
        # Enhance execution context with Codex-specific settings
        codex_context = execution_context.copy()

        # Extract Codex config
        agent_config = execution_context.get("agent_config", {})

        # Set Codex model - prefer model_identifier from AIModel, fall back to agent_config
        model_identifier = execution_context.get("model_identifier")
        agent_model = agent_config.get("model")

        self.logger.info(
            f"Codex model resolution: model_identifier={model_identifier}, "
            f"agent_config.model={agent_model}"
        )

        model = (
            (
                execution_context.get("model_gateway_model_alias")
                if execution_context.get("model_gateway_enabled")
                else None
            )
            or model_identifier
            or agent_model
            or "gpt-5.4"
        )
        codex_context["codex_model"] = model

        self.logger.info(f"Starting Codex CLI with model={model}")

        # Start the container with enhanced context
        return await super().start(codex_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start Codex CLI in a Docker container.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Log execution context for debugging
        self.logger.info(
            f"_start_docker_container called with codex_model={execution_context.get('codex_model')}, "
            f"model_identifier={execution_context.get('model_identifier')}, "
            f"has_model_api_key={('model_api_key' in execution_context)}"
        )

        # Prepare Codex-specific environment variables
        env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication (always for Codex)
        account_api_token = execution_context.get("account_api_token")
        if account_api_token:
            env["PRELOOP_API_TOKEN"] = account_api_token
        else:
            self.logger.warning("No account API token provided for Preloop MCP access")

        # Set Preloop MCP URL (defaults to host.docker.internal for container access)
        env["PRELOOP_MCP_URL"] = os.getenv(
            "PRELOOP_MCP_URL", "http://host.docker.internal:8000/mcp/v1"
        )

        # Add MCP_TOOL_TIMEOUT_SEC for config.toml substitution
        # This is retrieved from the execution context (set by _prepare_environment)
        mcp_timeout = execution_context.get("_mcp_tool_timeout", 600)
        env["MCP_TOOL_TIMEOUT_SEC"] = str(mcp_timeout)
        self.logger.info(f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for config.toml")

        # Add MCP configuration using MCP config service
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])

        if allowed_mcp_servers or allowed_mcp_tools:
            # Generate MCP environment variables
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            env.update(mcp_env)

            # Generate MCP config file
            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        # Build the Codex script using shared method
        script = self._build_codex_script(execution_context)

        # Determine working directory based on git clone configuration
        working_dir = "/workspace"
        git_clone_config = execution_context.get("git_clone_config")
        if git_clone_config:
            repositories = git_clone_config.get("repositories", [])
            if repositories:
                # Use the first repository's clone path as working directory
                clone_path = repositories[0].get("clone_path", "/workspace")
                if clone_path.startswith("/"):
                    # Absolute path
                    working_dir = clone_path
                else:
                    # Relative path - prepend /workspace/
                    working_dir = f"/workspace/{clone_path}"
                self.logger.info(
                    f"Setting Codex working directory to git repository: {working_dir}"
                )

        # Extract model for logging
        model = (
            execution_context.get("codex_model")
            or execution_context.get("model_identifier")
            or "gpt-5.4"
        )

        self.logger.info(
            f"Container config: model={model}, "
            f"has_api_key={'OPENAI_API_KEY' in env}, "
            f"env_vars={list(env.keys())}"
        )

        # Container configuration
        container_config = {
            "Image": self.image,
            "Env": [
                f"{k}={v}"
                for k, v in self._apply_git_credential_env(
                    env, execution_context
                ).items()
            ],
            # Don't override entrypoint - let codex-universal image configure environment
            # The entrypoint drops into bash, so pass -c and script as arguments to bash
            "Cmd": ["-c", script],
            "WorkingDir": working_dir,  # Set to git repo if configured, otherwise /workspace
            "Labels": {
                "preloop.flow_id": execution_context["flow_id"],
                "preloop.execution_id": execution_id,
                "preloop.agent_type": self.agent_type,
            },
            "HostConfig": {
                "AutoRemove": False,  # Keep container for log retrieval
                "NetworkMode": os.getenv(
                    "AGENT_NETWORK_MODE", "bridge"
                ),  # Use bridge by default
                # Resource limits
                "Memory": int(os.getenv("AGENT_MEMORY_LIMIT", "2g").replace("g", ""))
                * 1024
                * 1024
                * 1024,
                "CpuQuota": int(os.getenv("AGENT_CPU_QUOTA", "100000")),
            },
        }

        try:
            # Pull image if not available
            try:
                await docker.images.inspect(self.image)
            except DockerError:
                self.logger.info(f"Pulling image {self.image}...")
                await docker.images.pull(self.image)

            # Create and start container
            container = await docker.containers.create(config=container_config)
            container_id = container.id

            await container.start()

            self._containers[container_id] = container

            self.logger.info(
                f"Started Codex CLI container {container_id[:12]} for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start Codex CLI container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start Codex CLI container: {e}")

    def _build_cli_session_blocks(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """Shell blocks for native CLI session persistence (Codex flavor).

        See OpenCodeAgent._build_cli_session_blocks for the shared design.
        Codex differences: the session id comes from the newest rollout file
        under ``$CODEX_HOME/sessions`` (its filename embeds the session
        uuid), and the resume flag is the ``codex exec resume`` subcommand.
        """
        blocks: Dict[str, str] = {
            "decode": "",
            "restore": "",
            "args": "",
            "capture": "",
            "pack": "",
        }
        blocks["capture"] = f"""
# Extract this run's session id from the newest rollout file so the
# orchestrator can persist it for a later PR-comment resume.
_pl_codex_sid=""
if [ -d "$CODEX_HOME/sessions" ]; then
    _pl_rollout=$(find "$CODEX_HOME/sessions" -type f -name 'rollout-*.jsonl' 2>/dev/null | sort | tail -n 1)
    if [ -n "$_pl_rollout" ]; then
        _pl_codex_sid=$(printf '%s\\n' "$_pl_rollout" | grep -oE '[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}' | tail -n 1)
    fi
fi
if [ -n "$_pl_codex_sid" ]; then
    echo "{AGENT_SESSION_MARKER} codex $_pl_codex_sid"
fi
"""
        blocks["pack"] = build_session_pack_shell(
            "codex", '"$CODEX_HOME/sessions"', excludes=("auth.json",)
        )
        if execution_context.get("confirmation_nudge"):
            return blocks

        cli_session_archive = execution_context.get("cli_session_restore_archive")
        if isinstance(cli_session_archive, (bytes, bytearray)) and cli_session_archive:
            blocks["decode"] = build_session_archive_decode_shell(
                bytes(cli_session_archive)
            )
        blocks["restore"] = build_session_restore_shell(
            "codex", '"$CODEX_HOME/sessions"'
        )
        session_id = resume_cli_session(execution_context, "codex")
        # Single quotes are safe: the id passed strict validation and cannot
        # contain quote characters.
        session_id_literal = f"'{session_id}'" if session_id else "''"
        blocks["args"] = f"""
PRELOOP_CLI_SESSION_ID={session_id_literal}
CODEX_RESUME_ARGS=""
if [ "$PRELOOP_CLI_SESSION_RESTORED" -eq 1 ] && [ -n "$PRELOOP_CLI_SESSION_ID" ] \\
    && codex exec --help 2>&1 | grep -qw resume; then
    CODEX_RESUME_ARGS="resume $PRELOOP_CLI_SESSION_ID"
elif [ "$PRELOOP_CLI_SESSION_RESTORED" -eq 1 ] \\
    && codex exec --help 2>&1 | grep -qw resume; then
    CODEX_RESUME_ARGS="resume --last"
fi
"""
        return blocks

    def _build_codex_script(self, execution_context: Dict[str, Any]) -> str:
        """
        Build the Codex initialization and execution script.

        This script is used by both Docker and Kubernetes modes.

        Args:
            execution_context: Execution context

        Returns:
            Shell script to execute
        """
        prompt = execution_context["prompt"]
        model = (
            execution_context.get("model_gateway_model_alias")
            if execution_context.get("model_gateway_enabled")
            else None
        ) or (
            execution_context.get("codex_model")
            or execution_context.get("model_identifier")
            or "gpt-5.4"
        )
        model_provider = (
            execution_context.get("model_gateway_provider")
            if execution_context.get("model_gateway_enabled")
            else execution_context.get("model_provider", "openai")
        )
        model_provider = (model_provider or "openai").lower()
        model_endpoint = (
            gateway_url_for_api(execution_context.get("model_gateway_url"), "openai")
            if execution_context.get("model_gateway_enabled")
            else execution_context.get("model_endpoint")
        ) or ""

        # Fallback: resolve endpoint from environment if not set in the AI model.
        # Checks {PROVIDER}_API_BASE (e.g. ZAI_API_BASE) then CUSTOM_API_BASE.
        if not model_endpoint and model_provider and model_provider != "openai":
            env_key = f"{model_provider.upper().replace('-', '_')}_API_BASE"
            model_endpoint = os.getenv(env_key) or os.getenv("CUSTOM_API_BASE", "")

        # Escape prompt for shell - must escape:
        # - Double quotes (for string delimiter)
        # - Single quotes (for shell quoting)
        # - Backticks (prevent command substitution - critical for markdown code blocks)
        # - Dollar signs (prevent variable expansion)
        # - Backslashes (prevent escape sequence interpretation)
        escaped_prompt = (
            prompt.replace("\\", "\\\\")  # Backslashes first
            .replace('"', '\\"')
            .replace("'", "\\'")
            .replace("`", "\\`")  # Backticks for markdown code fences
            .replace("$", "\\$")  # Dollar signs for variables
        )

        # Prepare initialization commands (git clone, custom commands)
        init_commands = self._prepare_init_commands(execution_context)

        # Prepare post-execution commands (push, PR/MR creation)
        post_exec_commands = self._prepare_git_post_execution_commands(
            execution_context
        )

        # Build post-execution block if there are commands
        post_exec_block = ""
        if post_exec_commands:
            post_exec_block = f"""
# Run post-execution commands (push, PR/MR) if codex succeeded
if [ "$CODEX_EXIT_CODE" -eq "0" ]; then
    echo "========================================="
    echo "Running post-execution git operations..."
    echo "========================================="
    {post_exec_commands}
fi
"""

        # In-place completion nudge, emitted BEFORE the post-execution git
        # block so it can never re-run a push. `codex exec resume --last`
        # re-enters the rollout this container just wrote, so the reminder
        # is one short exchange in the same workspace rather than a second
        # session with a second clone.
        completion_nudge_block = ""
        if completion_nudge_enabled(execution_context):
            completion_nudge_block = build_completion_nudge_block(
                agent_label="codex",
                exit_code_var="CODEX_EXIT_CODE",
                resume_probe="codex exec --help 2>&1 | grep -qw resume",
                resume_command=(
                    "$PRELOOP_NUDGE_TIMEOUT codex exec resume --last "
                    "--skip-git-repo-check "
                    f'--model "{model}" --sandbox workspace-write --yolo '
                    f'"$(cat {NUDGE_PROMPT_PATH})" 2>&1 '
                    f'| tee -a "{AGENT_OUTPUT_LOG_PATH}"'
                ),
                timeout_seconds=completion_nudge_timeout_seconds(),
            )

        # Get execution details for logging
        execution_id = execution_context.get("execution_id", "unknown")
        flow_name = execution_context.get("flow_name", "unknown")

        auth_block = self._build_codex_auth_config(
            model, model_provider, model_endpoint
        )

        # Native CLI session persistence blocks (mostly empty on cold start).
        session_blocks = self._build_cli_session_blocks(execution_context)

        # Create the full script
        script = f"""
set -e

# Keep the container alive after execution for debugging.
# Controlled by AGENT_POST_EXEC_SLEEP (seconds, default 0 = disabled).
# Set to e.g. 600 to keep containers alive for 10 minutes.
_post_exec_sleep() {{
    _sleep=${{AGENT_POST_EXEC_SLEEP:-0}}
    if [ "$_sleep" -gt 0 ] 2>/dev/null; then
        echo ""
        echo "========================================="
        echo "Post-execution debug sleep: ${{_sleep}}s"
        echo "Container stays alive for inspection."
        echo "========================================="
        sleep "$_sleep"
    fi
}}
trap _post_exec_sleep EXIT

# ============================================================
# Flow Execution Information
# ============================================================
echo "=================================================="
echo "Flow Execution Started"
echo "=================================================="
echo "Execution ID: {execution_id}"
echo "Flow Name: {flow_name}"
echo "Agent Type: Codex"
echo "Model: {model}"
echo "Start Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "=================================================="
echo ""

# Run initialization commands (git clone, custom commands) if any
{init_commands}

# Restore a prior CLI session (correlated PR-comment resume), if the
# workspace snapshot carried one.
{session_blocks["decode"]}
CODEX_HOME="${{CODEX_HOME:-$HOME/.codex}}"
{session_blocks["restore"]}

# Configure git to trust all directories (needed for cloned repos)
git config --global --add safe.directory '*'

# Configure Codex CLI in the universal image
npm install -g @openai/codex

# Verify API key is set
if [ -z "$OPENAI_API_KEY" ]; then
    echo "ERROR: OPENAI_API_KEY is not set"
    exit 1
fi

# Configure Codex CLI authentication
mkdir -p ~/.codex

"""
        script = (
            script
            + auth_block
            + f"""
# Debug: Show config files (with API key masked)
echo "=== Codex Configuration ==="
echo "Model: {model}"
echo "Provider: {model_provider}"
echo "MCP Server: $PRELOOP_MCP_URL"
echo "=========================="

# Resume the prior CLI session when a correlated restart restored one;
# expands to nothing on a cold start.
{session_blocks["args"]}

# Signal to the orchestrator that the agent is about to start.
# Sentinel detection is suppressed until this marker is seen in logs.
echo "PRELOOP_AGENT_EXEC_START"

# Run codex in non-interactive mode with the prompt.
# The output is tee'd so the in-place completion nudge can check for the
# success sentinel on a line of its own, exactly as the orchestrator does.
# PIPESTATUS[1] is codex's own exit code (echo | codex | tee).
set +e
: > "{AGENT_OUTPUT_LOG_PATH}"
echo "{escaped_prompt}" | codex exec $CODEX_RESUME_ARGS --skip-git-repo-check --model "{model}" --sandbox workspace-write --yolo 2>&1 | tee -a "{AGENT_OUTPUT_LOG_PATH}"
CODEX_PIPE_CODES=("${{PIPESTATUS[@]}}")
CODEX_EXIT_CODE=${{CODEX_PIPE_CODES[1]:-0}}
set -e
{session_blocks["capture"]}
{completion_nudge_block}{session_blocks["pack"]}{post_exec_block}
# Exit with codex's exit code
exit $CODEX_EXIT_CODE
"""
        )
        return script

    async def _start_kubernetes_pod(self, execution_context: Dict[str, Any]) -> str:
        """
        Override to add Codex-specific command to Kubernetes pod.

        The base class creates the pod but doesn't set command/args, which causes
        codex-universal to drop into a bash shell. We need to override this to
        provide the script as command arguments.

        IMPORTANT: We only set args, NOT command. Setting command would override
        the image's ENTRYPOINT, which sets up PATH and other environment variables.
        By only setting args, the entrypoint runs first (sets up environment), then
        passes our args to bash for execution.
        """
        # Get the script to execute
        script = self._build_codex_script(execution_context)

        # Store script in execution context so base class can access it if needed
        execution_context["_codex_script"] = script

        # Set args for Kubernetes - these will be passed to the image's entrypoint
        # The entrypoint sets up the environment and then executes: bash "$@"
        # So our args become: bash -c "script"
        execution_context["_container_args"] = ["-c", script]
        # Don't set _container_command - let the image's entrypoint run

        # Prepare Codex-specific environment variables and store in context
        # The base class will merge these with its default env vars
        codex_env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication (always for Codex)
        account_api_token = execution_context.get("account_api_token")
        if account_api_token:
            codex_env["PRELOOP_API_TOKEN"] = account_api_token
        else:
            self.logger.warning("No account API token provided for Preloop MCP access")

        # Set Preloop MCP URL (for Kubernetes, use the service DNS name or external URL)
        codex_env["PRELOOP_MCP_URL"] = os.getenv(
            "PRELOOP_MCP_URL_K8S",
            os.getenv("PRELOOP_MCP_URL", "http://preloop-api:8000/mcp/v1"),
        )

        # Add MCP_TOOL_TIMEOUT_SEC for config.toml substitution
        # This is retrieved from the execution context (set by _prepare_environment)
        mcp_timeout = execution_context.get("_mcp_tool_timeout", 600)
        codex_env["MCP_TOOL_TIMEOUT_SEC"] = str(mcp_timeout)
        self.logger.info(
            f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for config.toml (Kubernetes)"
        )

        # Add MCP configuration using MCP config service
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])

        if allowed_mcp_servers or allowed_mcp_tools:
            # Generate MCP environment variables
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            codex_env.update(mcp_env)

            # Generate MCP config file
            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            codex_env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        execution_context["_codex_env"] = codex_env

        # Call parent implementation which will use the args and env
        return await super()._start_kubernetes_pod(execution_context)

    def _build_codex_auth_config(
        self, model: str, model_provider: str, model_endpoint: str
    ) -> str:
        """
        Build the auth.json and config.toml shell script block for Codex CLI.

        For OpenAI models, generates a standard config.
        For custom models, generates a custom_provider section with base_url,
        env_key, and wire_api so Codex knows how to reach the provider.
        Preloop MCP is always attached; tool enablement is the allowlist.

        Args:
            model: Model identifier (e.g., "gpt-5.4", "claude-sonnet-4-20250514")
            model_provider: Provider name (e.g., "openai", "anthropic")
            model_endpoint: API base URL for custom providers

        Returns:
            Shell script block to write auth.json and config.toml
        """
        is_custom = model_provider and model_provider != "openai"

        if is_custom:
            # Custom provider: generate provider-specific config
            provider_key = model_provider.replace("-", "_").replace(" ", "_")
            env_key = f"{model_provider.upper().replace('-', '_')}_API_KEY"
            # The Preloop gateway exposes the Responses API and translates it to the
            # configured upstream provider. Direct OpenAI-compatible providers such as
            # DeepSeek generally only expose chat/completions.
            wire_api = "responses" if provider_key == "preloop" else "chat"

            if not model_endpoint:
                logger.warning(
                    f"Custom model provider '{model_provider}' has no api_endpoint configured. "
                    f"base_url will be omitted from config.toml."
                )

            # Only include base_url if we have an actual endpoint URL
            base_url_line = f'base_url = "{model_endpoint}"' if model_endpoint else ""

            auth_block = f"""# Create auth.json with provider API key
cat > ~/.codex/auth.json << EOF
{{
  "{env_key}": "${env_key}",
  "OPENAI_API_KEY": "$OPENAI_API_KEY"
}}
EOF

# Create config.toml with custom model provider and MCP server configuration
cat > ~/.codex/config.toml << EOF
model_provider = "{provider_key}"
model = "{model}"

rmcp_client = true

[model_providers.{provider_key}]
name = "{model_provider.title()}"
{base_url_line}
env_key = "{env_key}"
wire_api = "{wire_api}"

[mcp_servers.preloop]
url = "$PRELOOP_MCP_URL"
bearer_token_env_var = "PRELOOP_API_TOKEN"
tool_timeout_sec = $MCP_TOOL_TIMEOUT_SEC
EOF"""
        else:
            # Standard OpenAI config
            auth_block = f"""# Create auth.json with OpenAI API key
cat > ~/.codex/auth.json << EOF
{{
  "OPENAI_API_KEY": "$OPENAI_API_KEY"
}}
EOF

# Create config.toml with model and MCP server configuration
cat > ~/.codex/config.toml << EOF
model = "{model}"

rmcp_client = true

[mcp_servers.preloop]
url = "$PRELOOP_MCP_URL"
bearer_token_env_var = "PRELOOP_API_TOKEN"
tool_timeout_sec = $MCP_TOOL_TIMEOUT_SEC
EOF"""

        return auth_block

    async def _prepare_environment(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Prepare Codex-specific environment variables.

        Args:
            execution_context: Execution context

        Returns:
            Environment variables dict
        """
        env = {}

        # Add API key - use provider-specific env var for custom models
        model_provider = execution_context.get("model_provider", "openai").lower()
        if execution_context.get("model_gateway_enabled"):
            gateway_provider = (
                execution_context.get("model_gateway_provider") or "preloop"
            ).lower()
            gateway_token = execution_context.get("model_gateway_token")
            if gateway_token:
                custom_env_key = f"{gateway_provider.upper().replace('-', '_')}_API_KEY"
                env[custom_env_key] = gateway_token
                env["OPENAI_API_KEY"] = gateway_token
                env["PRELOOP_MODEL_GATEWAY_TOKEN"] = gateway_token
        elif "model_api_key" in execution_context:
            if model_provider == "openai" or not model_provider:
                env["OPENAI_API_KEY"] = execution_context["model_api_key"]
            else:
                # Custom provider: set both the custom env var and OPENAI_API_KEY
                # (OPENAI_API_KEY is still needed as a fallback for codex internals)
                custom_env_key = f"{model_provider.upper()}_API_KEY"
                env[custom_env_key] = execution_context["model_api_key"]
                env["OPENAI_API_KEY"] = execution_context["model_api_key"]

        # HOME is set by the container setup (container.py) based on the
        # configured UID. Don't hardcode it here.

        # Configure language runtimes for codex-universal image
        # These env vars tell the image which versions to set up
        env["CODEX_ENV_PYTHON_VERSION"] = os.getenv("CODEX_ENV_PYTHON_VERSION", "3.12")
        env["CODEX_ENV_NODE_VERSION"] = os.getenv("CODEX_ENV_NODE_VERSION", "20")
        env["CODEX_ENV_RUST_VERSION"] = os.getenv("CODEX_ENV_RUST_VERSION", "1.87.0")
        env["CODEX_ENV_GO_VERSION"] = os.getenv("CODEX_ENV_GO_VERSION", "1.23.8")
        env["CODEX_ENV_SWIFT_VERSION"] = os.getenv("CODEX_ENV_SWIFT_VERSION", "6.2")
        env["CODEX_ENV_RUBY_VERSION"] = os.getenv("CODEX_ENV_RUBY_VERSION", "3.4.4")
        env["CODEX_ENV_PHP_VERSION"] = os.getenv("CODEX_ENV_PHP_VERSION", "8.4")

        # Configure MCP tool timeout based on approval workflows
        # Base timeout is 600 seconds (10 minutes) - increased from 5 minutes
        # This is higher than the default 60s to account for approval workflows
        mcp_timeout = 600

        # Check if there are approval workflows that may require longer timeouts
        account_id = execution_context.get("account_id")
        if account_id:
            try:
                # Query all tool configurations and approval workflows for this account
                from preloop.models.db.session import get_db_context
                from preloop.models.crud import tool_configuration as tool_config_crud
                from preloop.models.crud import (
                    approval_workflow as approval_workflow_crud,
                )

                with get_db_context() as db:
                    max_approval_timeout = 0
                    has_escalation = False

                    # Get all tool configurations for this account
                    tool_configs = tool_config_crud.get_multi_by_account(
                        db, account_id=account_id, limit=1000
                    )

                    # Check each tool configuration for approval workflows
                    for config in tool_configs:
                        if config.approval_workflow_id:
                            # Get approval workflow
                            workflow = approval_workflow_crud.get(
                                db, id=config.approval_workflow_id
                            )
                            if workflow and workflow.timeout_seconds:
                                max_approval_timeout = max(
                                    max_approval_timeout, workflow.timeout_seconds
                                )
                                # Check for escalation
                                if workflow.escalation_workflow:
                                    has_escalation = True

                    # Set MCP timeout based on approval workflows
                    if max_approval_timeout > 0:
                        # Use twice the approval timeout if there's escalation
                        if has_escalation:
                            mcp_timeout = max_approval_timeout * 2
                        else:
                            mcp_timeout = max_approval_timeout

                        self.logger.info(
                            f"Set MCP_TOOL_TIMEOUT to {mcp_timeout}s based on approval workflows "
                            f"(max_approval_timeout={max_approval_timeout}, has_escalation={has_escalation})"
                        )
            except Exception as e:
                self.logger.warning(
                    f"Failed to query approval workflows for MCP timeout calculation: {e}. "
                    f"Using default timeout of {mcp_timeout}s"
                )

        env["MCP_TOOL_TIMEOUT"] = str(mcp_timeout)
        # Store timeout in context for use in config.toml generation
        execution_context["_mcp_tool_timeout"] = mcp_timeout
        self.logger.info(
            f"MCP_TOOL_TIMEOUT set to {mcp_timeout}s (will be configured in config.toml)"
        )

        return env
