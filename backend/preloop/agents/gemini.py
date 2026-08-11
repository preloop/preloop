"""Google Gemini CLI agent implementation."""

import base64
import json
import logging
import os
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from aiodocker.exceptions import DockerError

from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api

from .container import ContainerAgentExecutor

logger = logging.getLogger(__name__)

# gemini-cli internal helper aliases that must be pinned to the flow's model
# when running behind the Preloop gateway.  Stock gemini-cli resolves these to
# hardcoded Google model names (gemini-2.5-flash / gemini-3-flash-preview via
# the `gemini-*-flash-base` alias chain), which do not exist behind the
# gateway and 404 (issue #212).  The failing calls are swallowed by the CLI,
# silently degrading loop detection, the next-speaker (auto-continue) check,
# tool-output summarization and chat compression — and a run whose
# continuation check dies mid-review exits 0 without the success sentinel.
#
# Deliberately NOT pinned: `web-fetch` and `web-search`.  Their primary path
# requires Google server-side tools (urlContext / googleSearch) that no
# gateway model provides; pinning them would make the flow model hallucinate
# page content instead of failing over to `web-fetch-fallback`, which fetches
# the page locally and IS pinned to the flow model below.
GEMINI_CLI_HELPER_ALIASES = (
    "loop-detection",
    "loop-detection-double-check",
    "next-speaker-checker",
    "web-fetch-fallback",
    "summarizer-default",
    "summarizer-shell",
    "edit-corrector",
    "llm-edit-fixer",
    "chat-compression-default",
    "classifier",
    "prompt-completion",
    "fast-ack-helper",
    "context-snapshotter",
)


def _gemini_cli_base_url(endpoint: str) -> str:
    """Return the Gemini CLI base URL before the SDK-appended API version."""
    parsed = urlsplit(endpoint.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1beta"):
        path = path[: -len("/v1beta")].rstrip("/")
    return urlunsplit(parsed._replace(path=path))


class GeminiAgent(ContainerAgentExecutor):
    """
    Google Gemini CLI agent executor.

    Runs Google's Gemini CLI tool (https://github.com/google-gemini/gemini-cli) in a Docker
    container for autonomous coding tasks.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Gemini agent.

        Args:
            config: Agent configuration including:
                - model: Gemini model to use (default: gemini-3-pro-preview)
                - custom settings for Gemini CLI
        """
        # Use the Gemini CLI sandbox image that supports `gemini mcp add`
        image = os.getenv(
            "GEMINI_IMAGE",
            "docker/sandbox-templates:gemini",
        )

        # Auto-detect Kubernetes environment or use explicit env var
        use_k8s = self._detect_kubernetes_environment()

        super().__init__(
            agent_type="gemini",
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
        Start Gemini agent with specialized configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID or pod name
        """
        # Enhance execution context with Gemini-specific settings
        gemini_context = execution_context.copy()

        # Extract Gemini config
        agent_config = execution_context.get("agent_config", {})

        # Set Gemini model - prefer model_identifier from AIModel, fall back to agent_config
        model_identifier = execution_context.get("model_identifier")
        agent_model = agent_config.get("model")

        self.logger.info(
            f"Gemini model resolution: model_identifier={model_identifier}, "
            f"agent_config.model={agent_model}"
        )

        model = model_identifier or agent_model or "gemini-3-pro-preview"
        gemini_context["gemini_model"] = model

        self.logger.info(f"Starting Gemini CLI with model={model}")

        # Start the container with enhanced context
        return await super().start(gemini_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start Gemini CLI in a Docker container.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Log execution context for debugging
        self.logger.info(
            f"_start_docker_container called with gemini_model={execution_context.get('gemini_model')}, "
            f"model_identifier={execution_context.get('model_identifier')}, "
            f"has_model_api_key={('model_api_key' in execution_context)}"
        )

        # Prepare Gemini-specific environment variables
        env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication (always for Gemini)
        account_api_token = execution_context.get("account_api_token")
        if account_api_token:
            env["PRELOOP_API_TOKEN"] = account_api_token
        else:
            self.logger.warning("No account API token provided for Preloop MCP access")

        # Set Preloop MCP URL (defaults to host.docker.internal for container access)
        env["PRELOOP_MCP_URL"] = os.getenv(
            "PRELOOP_MCP_URL", "http://host.docker.internal:8000/mcp/v1"
        )

        # Add MCP_TOOL_TIMEOUT_SEC for config substitution
        mcp_timeout = execution_context.get("_mcp_tool_timeout", 600)
        env["MCP_TOOL_TIMEOUT_SEC"] = str(mcp_timeout)
        self.logger.info(f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for Gemini config")

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

        # Build the Gemini script using shared method
        script = self._build_gemini_script(execution_context)

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
                    f"Setting Gemini working directory to git repository: {working_dir}"
                )

        # Extract model for logging
        model = (
            execution_context.get("gemini_model")
            or execution_context.get("model_identifier")
            or "gemini-3-pro-preview"
        )

        self.logger.info(
            f"Container config: model={model}, "
            f"has_api_key={'GEMINI_API_KEY' in env}, "
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
            "Cmd": ["/bin/bash", "-c", script],
            "WorkingDir": working_dir,
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
                f"Started Gemini CLI container {container_id[:12]} for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start Gemini CLI container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start Gemini CLI container: {e}")

    def _build_gateway_helper_settings(self, model: str) -> Dict[str, Any]:
        """
        Build gemini-cli settings pinning internal helper models to the flow model.

        Only used for Preloop-gateway runs.  Two things are configured:

        - ``modelConfigs.customAliases``: every internal helper alias
          (loop detection, next-speaker check, web-fetch fallback,
          summarizers, chat compression, ...) is pointed at the flow's
          gateway model alias.  Stock gemini-cli resolves these helpers to
          hardcoded Google models that the gateway does not serve, so every
          helper call 404s and is silently swallowed (issue #212).  The
          next-speaker check failing this way is what lets the CLI stop
          mid-task with exit code 0 and no success sentinel.
        - ``model.disableLoopDetection``: the CLI's own loop detection is
          redundant here — the orchestrator has its own MCP tool-loop
          backstop and execution timeouts — and its LLM-based check both
          costs gateway tokens (it ships recent history every ~30 turns) and
          can false-positive on legitimately repetitive review workloads,
          aborting the run without the sentinel.

        ``web-fetch`` and ``web-search`` primaries are intentionally NOT
        pinned: they rely on Google server-side tools (urlContext /
        googleSearch) that gateway models cannot provide.  Left alone, their
        404 makes web_fetch fail over to the local ``web-fetch-fallback``
        path, which IS pinned and works with any model.
        """
        return {
            "model": {
                "disableLoopDetection": True,
            },
            "modelConfigs": {
                "customAliases": {
                    alias: {"modelConfig": {"model": model}}
                    for alias in GEMINI_CLI_HELPER_ALIASES
                }
            },
        }

    def _build_gemini_script(self, execution_context: Dict[str, Any]) -> str:
        """
        Build the Gemini initialization and execution script.

        This script is used by both Docker and Kubernetes modes.

        Args:
            execution_context: Execution context

        Returns:
            Shell script to execute
        """
        prompt = execution_context["prompt"]
        model = (
            execution_context.get("gemini_model")
            or execution_context.get("model_identifier")
            or "gemini-3-pro-preview"
        )

        # Base64-encode the prompt so it can be safely embedded in
        # the shell script without heredoc delimiter injection risk.
        prompt_b64 = base64.b64encode(prompt.encode()).decode()

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
# Run post-execution commands (push, PR/MR) if gemini succeeded
if [ "$GEMINI_EXIT_CODE" -eq "0" ]; then
    echo "========================================="
    echo "Running post-execution git operations..."
    echo "========================================="
    {post_exec_commands}
fi
"""

        # Get execution details for logging
        execution_id = execution_context.get("execution_id", "unknown")
        flow_name = execution_context.get("flow_name", "unknown")

        # Convert timeout from seconds to milliseconds for Gemini CLI
        mcp_timeout_ms = execution_context.get("_mcp_tool_timeout", 600) * 1000

        # For Preloop-gateway runs, write a settings.json that pins gemini-cli's
        # internal helper models to the flow's model and disables the CLI's own
        # loop detection (see _build_gateway_helper_settings).  Base64-encoded
        # for safe shell embedding.  Written BEFORE `gemini mcp add`, which
        # merges the MCP server into the same user settings file.
        gateway_settings_block = ""
        if execution_context.get("model_gateway_enabled"):
            settings_json = json.dumps(
                self._build_gateway_helper_settings(model), indent=2
            )
            settings_b64 = base64.b64encode(settings_json.encode()).decode()
            gateway_settings_block = f"""
# Pin gemini-cli internal helper models (loop detection, next-speaker check,
# web-fetch fallback, summarizers, compression) to the flow's gateway model.
# Stock gemini-cli calls hardcoded Google models for these, which the Preloop
# gateway does not serve (404 -> silent helper degradation, issue #212).
echo '{settings_b64}' | base64 -d > "$HOME/.gemini/settings.json"
"""

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
echo "Agent Type: Gemini"
echo "Model: {model}"
echo "Start Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "=================================================="
echo ""

# Run initialization commands (git clone, custom commands) if any
{init_commands}

# Configure git to trust all directories (needed for cloned repos)
git config --global --add safe.directory '*'

# Verify API key is set
if [ -z "$GEMINI_API_KEY" ]; then
    echo "ERROR: GEMINI_API_KEY is not set"
    exit 1
fi

# Configure Gemini CLI MCP servers
mkdir -p ~/.gemini
{gateway_settings_block}
# Register the Preloop MCP server via `gemini mcp add`.
# Flags: -t http (HTTP transport), -s user (user scope),
#         --trust (auto-approve), -H (custom header).
gemini mcp add preloop "$PRELOOP_MCP_URL" \
  -t http \
  -s user \
  --trust \
  -H "Authorization: Bearer $PRELOOP_API_TOKEN"

# Debug: Show config (with token masked)
echo "=== Gemini Configuration ==="
echo "Model: {model}"
echo "MCP Server: $PRELOOP_MCP_URL"
echo "MCP Timeout: {mcp_timeout_ms}ms"
echo "Working Directory: $(pwd)"
echo "==========================="

# Write prompt via base64 to prevent heredoc delimiter injection.
# The prompt may originate from external events (webhooks, triggers)
# and could contain arbitrary text including shell metacharacters.
echo '{prompt_b64}' | base64 -d > /tmp/prompt.txt

# Signal to the orchestrator that the agent is about to start.
# Sentinel detection is suppressed until this marker is seen in logs.
echo "PRELOOP_AGENT_EXEC_START"

# Run Gemini CLI with the prompt
# --yolo: Skip confirmation prompts for tool usage
# -m: Specify the model
# --prompt: Pass the prompt (read from file)
gemini --yolo -m "{model}" --prompt "$(cat /tmp/prompt.txt)"
GEMINI_EXIT_CODE=$?

echo ""
echo "=================================================="
echo "Gemini CLI exited with code: $GEMINI_EXIT_CODE"
echo "=================================================="
{post_exec_block}
# Exit with gemini's exit code
exit $GEMINI_EXIT_CODE
"""
        return script

    async def _start_kubernetes_pod(self, execution_context: Dict[str, Any]) -> str:
        """
        Override to add Gemini-specific command to Kubernetes pod.

        Similar to Codex, we only set args (not command) to preserve the
        image's entrypoint that sets up the environment.
        """
        # Get the script to execute
        script = self._build_gemini_script(execution_context)

        # Store script in execution context
        execution_context["_gemini_script"] = script

        # Set command and args for Kubernetes.
        # The Gemini sandbox image has no shell-forwarding entrypoint,
        # so we must explicitly set the command to /bin/bash.
        execution_context["_container_command"] = ["/bin/bash"]
        execution_context["_container_args"] = ["-c", script]

        # Prepare Gemini-specific environment variables
        gemini_env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication
        account_api_token = execution_context.get("account_api_token")
        if account_api_token:
            gemini_env["PRELOOP_API_TOKEN"] = account_api_token
        else:
            self.logger.warning("No account API token provided for Preloop MCP access")

        # Set Preloop MCP URL (for Kubernetes)
        gemini_env["PRELOOP_MCP_URL"] = os.getenv(
            "PRELOOP_MCP_URL_K8S",
            os.getenv("PRELOOP_MCP_URL", "http://preloop-api:8000/mcp/v1"),
        )

        # Add MCP_TOOL_TIMEOUT_SEC
        mcp_timeout = execution_context.get("_mcp_tool_timeout", 600)
        gemini_env["MCP_TOOL_TIMEOUT_SEC"] = str(mcp_timeout)
        self.logger.info(
            f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for Gemini (Kubernetes)"
        )

        # Add MCP configuration
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])

        if allowed_mcp_servers or allowed_mcp_tools:
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            gemini_env.update(mcp_env)

            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            gemini_env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        execution_context["_agent_env"] = gemini_env

        # Call parent implementation
        return await super()._start_kubernetes_pod(execution_context)

    async def _prepare_environment(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Prepare Gemini-specific environment variables.

        Args:
            execution_context: Execution context

        Returns:
            Environment variables dict
        """
        env = {}

        # API key for Gemini CLI:
        # - Direct provider: use provider secret (model_api_key).
        # - Preloop gateway: use the short-lived gateway token (model_gateway_token);
        #   model_api_key is intentionally None in that mode (see resolve_ai_model_runtime).
        if execution_context.get("model_gateway_enabled"):
            gateway_token = execution_context.get("model_gateway_token")
            if gateway_token:
                env["GEMINI_API_KEY"] = gateway_token
            env["GEMINI_API_KEY_HEADER"] = "x-goog-api-key"
        elif execution_context.get("model_api_key"):
            env["GEMINI_API_KEY"] = execution_context["model_api_key"]

        if execution_context.get("model_endpoint"):
            model_endpoint = execution_context["model_endpoint"]
            if execution_context.get("model_gateway_enabled"):
                model_endpoint = gateway_url_for_api(model_endpoint, "gemini")
                model_endpoint = _gemini_cli_base_url(model_endpoint)
                env["GOOGLE_GENAI_API_VERSION"] = "v1beta"
            env["GEMINI_API_BASE_URL"] = model_endpoint
            env["GOOGLE_GEMINI_BASE_URL"] = model_endpoint

        # HOME is set by the container setup (container.py) based on the
        # configured UID. Don't hardcode it here.

        # Configure MCP tool timeout based on approval workflows
        # Base timeout is 600 seconds (10 minutes)
        mcp_timeout = 600

        # Check if there are approval workflows that may require longer timeouts
        account_id = execution_context.get("account_id")
        if account_id:
            try:
                from preloop.models.db.session import get_db_context
                from preloop.models.crud import tool_configuration as tool_config_crud
                from preloop.models.crud import (
                    approval_workflow as approval_workflow_crud,
                )

                with get_db_context() as db:
                    max_approval_timeout = 0
                    has_escalation = False

                    tool_configs = tool_config_crud.get_multi_by_account(
                        db, account_id=account_id, limit=1000
                    )

                    for config in tool_configs:
                        if config.approval_workflow_id:
                            workflow = approval_workflow_crud.get(
                                db, id=config.approval_workflow_id
                            )
                            if workflow and workflow.timeout_seconds:
                                max_approval_timeout = max(
                                    max_approval_timeout, workflow.timeout_seconds
                                )
                                if workflow.escalation_workflow:
                                    has_escalation = True

                    if max_approval_timeout > 0:
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
        # Store timeout in context for use in config generation
        execution_context["_mcp_tool_timeout"] = mcp_timeout
        self.logger.info(f"MCP_TOOL_TIMEOUT set to {mcp_timeout}s")

        return env
