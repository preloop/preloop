"""OpenCode CLI agent implementation."""

import base64
import json
import logging
import os
import shlex
from typing import Any, Dict

from aiodocker.exceptions import DockerError

from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api

from .container import ContainerAgentExecutor

logger = logging.getLogger(__name__)


def _opencode_provider_local_model_id(model: str, provider: str) -> str:
    """
    Return the model id used inside OpenCode's provider.models map.

    Strips a single leading ``{provider}/`` prefix so registry keys stay
    provider-local (OpenCode resolves models against the suffix of the
    top-level ``model`` field, not a duplicated ``provider/provider/...`` path).
    """
    m = (model or "").strip()
    p = (provider or "").strip().lower()
    if not m or not p:
        return m
    prefix = f"{p}/"
    if m.lower().startswith(prefix):
        rest = m[len(prefix) :].strip()
        return rest if rest else m
    return m


class OpenCodeAgent(ContainerAgentExecutor):
    """
    OpenCode CLI agent executor.

    Runs the OpenCode CLI tool (https://github.com/anomalyco/opencode) in a Docker
    container for autonomous coding tasks.  OpenCode is provider-agnostic and
    supports any LLM configured by the user.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize OpenCode agent.

        Args:
            config: Agent configuration including:
                - model: Model identifier to use (required, no default)
                - custom settings for OpenCode CLI
        """
        image = os.getenv("OPENCODE_IMAGE", "docker/sandbox-templates:opencode")

        # Auto-detect Kubernetes environment or use explicit env var
        use_k8s = self._detect_kubernetes_environment()

        super().__init__(
            agent_type="opencode",
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
        Start OpenCode agent with specialized configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID or pod name
        """
        # Enhance execution context with OpenCode-specific settings
        opencode_context = execution_context.copy()

        # Extract OpenCode config
        agent_config = execution_context.get("agent_config", {})

        # Set model - prefer model_identifier from AIModel, fall back to agent_config
        model_identifier = execution_context.get("model_identifier")
        agent_model = agent_config.get("model")

        self.logger.info(
            f"OpenCode model resolution: model_identifier={model_identifier}, "
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
        )
        if not model:
            raise ValueError(
                "No model specified for OpenCode agent. "
                "Set model_identifier or agent_config.model."
            )
        opencode_context["opencode_model"] = model

        self.logger.info(f"Starting OpenCode CLI with model={model}")

        # Start the container with enhanced context
        return await super().start(opencode_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start OpenCode CLI in a Docker container.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Log execution context for debugging
        self.logger.info(
            f"_start_docker_container called with opencode_model={execution_context.get('opencode_model')}, "
            f"model_identifier={execution_context.get('model_identifier')}, "
            f"has_model_api_key={('model_api_key' in execution_context)}"
        )

        # Prepare OpenCode-specific environment variables
        env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication
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
        self.logger.info(f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for OpenCode config")

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

        # Build the OpenCode script using shared method
        script = self._build_opencode_script(execution_context)

        # Determine working directory based on git clone configuration
        working_dir = "/workspace"
        git_clone_config = execution_context.get("git_clone_config")
        if git_clone_config:
            repositories = git_clone_config.get("repositories", [])
            if repositories:
                # Use the first repository's clone path as working directory
                clone_path = repositories[0].get("clone_path", "/workspace")
                if clone_path.startswith("/"):
                    working_dir = clone_path
                else:
                    working_dir = f"/workspace/{clone_path}"
                self.logger.info(
                    f"Setting OpenCode working directory to git repository: {working_dir}"
                )

        # Extract model for logging
        model = (
            execution_context.get("opencode_model")
            or execution_context.get("model_identifier")
            or "unknown"
        )

        self.logger.info(
            f"Container config: model={model}, env_vars={list(env.keys())}"
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
                "NetworkMode": os.getenv("AGENT_NETWORK_MODE", "bridge"),
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
                f"Started OpenCode CLI container {container_id[:12]} for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start OpenCode CLI container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start OpenCode CLI container: {e}")

    def _build_opencode_script(self, execution_context: Dict[str, Any]) -> str:
        """
        Build the OpenCode initialization and execution script.

        This script is used by both Docker and Kubernetes modes.

        Args:
            execution_context: Execution context

        Returns:
            Shell script to execute
        """
        prompt = execution_context["prompt"]
        model = execution_context.get("opencode_model") or execution_context.get(
            "model_identifier"
        )
        if not model:
            raise ValueError("No model specified for OpenCode agent.")

        model_provider = execution_context.get("model_provider", "anthropic").lower()

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
# Run post-execution commands (push, PR/MR) if opencode succeeded
if [ "$OPENCODE_EXIT_CODE" -eq "0" ]; then
    echo "========================================="
    echo "Running post-execution git operations..."
    echo "========================================="
    {post_exec_commands}
fi
"""

        # Get execution details for logging
        execution_id = execution_context.get("execution_id", "unknown")
        flow_name = execution_context.get("flow_name", "unknown")

        # Convert timeout from seconds to milliseconds for OpenCode config
        mcp_timeout_ms = execution_context.get("_mcp_tool_timeout", 600) * 1000

        # Build the OpenCode config JSON for MCP server
        opencode_config = self._build_opencode_config(
            model, model_provider, execution_context, mcp_timeout_ms
        )
        opencode_model_arg = shlex.quote(str(opencode_config["model"]))
        opencode_config_json = json.dumps(opencode_config, indent=2)
        # For the unquoted heredoc, escape "$schema" so the shell doesn't
        # try to expand it as a variable.  All other $-prefixed strings
        # ($PRELOOP_MCP_URL, $PRELOOP_API_TOKEN) are intentionally left
        # unescaped so the shell expands them to their env-var values.
        opencode_config_shell = opencode_config_json.replace('"$schema"', '"\\$schema"')

        # Base64-encode the prompt so it can be safely embedded in
        # the shell script without heredoc delimiter injection risk.
        prompt_b64 = base64.b64encode(prompt.encode()).decode()

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
echo "Agent Type: OpenCode"
echo "Model: {model}"
echo "Provider: {model_provider}"
echo "Start Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "=================================================="
echo ""

# Run initialization commands (git clone, custom commands) if any
{init_commands}

# Configure git to trust all directories (needed for cloned repos)
git config --global --add safe.directory '*'

# Install OpenCode CLI
npm install -g opencode-ai@latest

# Write OpenCode configuration (unquoted heredoc for shell variable expansion).
# The dollar-sign in schema key is escaped so it stays literal; env vars
# PRELOOP_MCP_URL and PRELOOP_API_TOKEN are expanded by the shell directly.
mkdir -p /workspace
cat > /workspace/opencode.json << OPENCODE_CONFIG_EOF
{opencode_config_shell}
OPENCODE_CONFIG_EOF

cat > /tmp/opencode-json-log-filter.js <<'JS'
const readline = require("node:readline");

const SENTINEL = "FLOW_EXECUTION_SUCCESS";

function eventName(event) {{
  return String(event.type || event.event || "").toLowerCase();
}}

function collectTextValues(value, name, output) {{
  if (Array.isArray(value)) {{
    for (const item of value) {{
      collectTextValues(item, name, output);
    }}
    return;
  }}

  if (!value || typeof value !== "object") {{
    return;
  }}

  const valueType = String(value.type || "").toLowerCase();
  for (const key of ["text", "content", "message"]) {{
    const text = value[key];
    if (
      typeof text === "string" &&
      (name.includes("message") ||
        name.includes("part") ||
        name.includes("text") ||
        valueType === "text" ||
        valueType === "assistant" ||
        text.includes(SENTINEL))
    ) {{
      output.push(text);
    }}
  }}

  for (const nested of Object.values(value)) {{
    collectTextValues(nested, name, output);
  }}
}}

const rl = readline.createInterface({{ input: process.stdin }});

rl.on("line", (line) => {{
  if (!line) {{
    return;
  }}

  let event;
  try {{
    event = JSON.parse(line);
  }} catch {{
    console.log(line);
    return;
  }}

  const seen = new Set();
  const values = [];
  collectTextValues(event, eventName(event), values);
  for (const text of values) {{
    if (!text || seen.has(text)) {{
      continue;
    }}
    seen.add(text);
    for (const outputLine of text.split(/\\r?\\n/)) {{
      console.log(outputLine);
    }}
  }}
}});
JS

# Debug: Show config (with keys masked)
echo "=== OpenCode Configuration ==="
echo "Model: {model}"
echo "Provider: {model_provider}"
echo "MCP Server: $PRELOOP_MCP_URL"
echo "MCP Timeout: {mcp_timeout_ms}ms"
echo "Working Directory: $(pwd)"
echo "=============================="

# Write prompt via base64 to prevent heredoc delimiter injection.
# The prompt may originate from external events (webhooks, triggers)
# and could contain arbitrary text including shell metacharacters.
echo '{prompt_b64}' | base64 -d > /tmp/prompt.txt

# Signal to the orchestrator that the agent is about to start.
# Sentinel detection is suppressed until this marker is seen in logs.
echo "PRELOOP_AGENT_EXEC_START"

# Run OpenCode with the prompt.
# opencode run accepts messages as positional args and runs non-interactively.
# TODO(reliability): If opencode supports reading from stdin or passing a file
# directly, we should switch to that instead of positional args $(cat ...) to avoid E2BIG on very large prompts.
# Auto-approve all permission requests to avoid hangs.
# We use '--' to prevent argument injection if the prompt starts with a hyphen.
set +e
opencode run --format json --model {opencode_model_arg} --dangerously-skip-permissions -- "$(cat /tmp/prompt.txt)" | node /tmp/opencode-json-log-filter.js
PIPE_CODES=("${{PIPESTATUS[@]}}")
OPENCODE_EXIT_CODE=${{PIPE_CODES[0]}}
FILTER_EXIT_CODE=${{PIPE_CODES[1]:-0}}
set -e
if [ "$FILTER_EXIT_CODE" -ne "0" ]; then
    echo "OpenCode JSON log filter exited with code: $FILTER_EXIT_CODE"
fi
if [ "$OPENCODE_EXIT_CODE" -ne "0" ]; then
    echo "OpenCode command failed; see CLI output above."
fi

echo ""
echo "=================================================="
echo "OpenCode CLI exited with code: $OPENCODE_EXIT_CODE"
echo "=================================================="
{post_exec_block}
# Exit with opencode's exit code
exit $OPENCODE_EXIT_CODE
"""
        return script

    def _build_opencode_config(
        self,
        model: str,
        model_provider: str,
        execution_context: Dict[str, Any],
        mcp_timeout_ms: int,
    ) -> Dict[str, Any]:
        """
        Build the opencode.json configuration object.

        Configures the model provider and the Preloop MCP server connection.

        OpenCode expects:
        - model in "provider/model" format (e.g., "custom/glm-5")
        - provider section with api.name (adapter like "openai"), api.baseURL,
          and a models registry for custom endpoints

        Args:
            model: Model identifier (e.g., "glm-5", "claude-sonnet-4-20250514")
            model_provider: Provider name (e.g., "anthropic", "openai", "custom")
            execution_context: Execution context
            mcp_timeout_ms: MCP tool timeout in milliseconds

        Returns:
            Configuration dict to be serialized as opencode.json
        """
        gateway_enabled = bool(execution_context.get("model_gateway_enabled"))
        effective_model_provider = (
            execution_context.get("model_gateway_provider")
            if gateway_enabled
            else model_provider or execution_context.get("model_provider")
        ) or "anthropic"
        effective_model_provider = str(effective_model_provider).strip().lower()

        if gateway_enabled:
            model_endpoint = (
                gateway_url_for_api(
                    execution_context.get("model_gateway_url"), "openai"
                )
                or ""
            )
        else:
            model_endpoint = execution_context.get("model_endpoint") or ""

        # Fallback: resolve endpoint from environment if not set in the AI model.
        if (
            not model_endpoint
            and effective_model_provider
            and effective_model_provider != "openai"
        ):
            env_key = f"{effective_model_provider.upper().replace('-', '_')}_API_BASE"
            model_endpoint = os.getenv(env_key) or os.getenv("CUSTOM_API_BASE", "")

        # Local model id for provider registry lookup (must match the suffix of
        # ``model_qualified``, never a duplicated ``{provider}/{provider}/...``).
        model_local_id = _opencode_provider_local_model_id(
            model, effective_model_provider
        )
        # OpenCode splits the model field on "/" to get providerID/modelID.
        # Without the slash, it treats the entire string as the provider.
        model_qualified = f"{effective_model_provider}/{model_local_id}"

        config: Dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": model_qualified,
            "small_model": model_qualified,
            "autoupdate": False,
            "share": "disabled",
            "enabled_providers": [effective_model_provider],
            "permission": "allow",
            "mcp": {
                "preloop": {
                    "type": "remote",
                    "url": "$PRELOOP_MCP_URL",
                    "headers": {
                        "Authorization": "Bearer $PRELOOP_API_TOKEN",
                    },
                    "timeout": mcp_timeout_ms,
                    "enabled": True,
                }
            },
        }

        # Add provider configuration for custom/non-builtin endpoints.
        # OpenCode schema requires:
        #   npm   – AI SDK adapter package (e.g. "@ai-sdk/openai-compatible")
        #   options.baseURL – API endpoint
        #   models – map of model-id → {name}
        if model_endpoint:
            if gateway_enabled and model_provider in ("google", "gemini"):
                config["provider"] = {
                    effective_model_provider: {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": model_endpoint,
                            "apiKey": "$OPENAI_API_KEY",
                            "timeout": 120000,
                            "chunkTimeout": 120000,
                        },
                        "models": {
                            model_local_id: {"name": model},
                        },
                    }
                }
            elif not gateway_enabled and model_provider in ("google", "gemini"):
                config["provider"] = {
                    effective_model_provider: {
                        "npm": "@ai-sdk/google",
                        "options": {
                            "baseURL": model_endpoint,
                            "apiKey": "$GOOGLE_API_KEY",
                            "timeout": 120000,
                            "chunkTimeout": 120000,
                        },
                        "models": {
                            model_local_id: {"name": model},
                        },
                    }
                }
            else:
                config["provider"] = {
                    effective_model_provider: {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": model_endpoint,
                            "apiKey": "$OPENAI_API_KEY",
                            "timeout": 120000,
                            "chunkTimeout": 120000,
                        },
                        "models": {
                            model_local_id: {"name": model},
                        },
                    }
                }

        return config

    async def _start_kubernetes_pod(self, execution_context: Dict[str, Any]) -> str:
        """
        Override to add OpenCode-specific command to Kubernetes pod.

        Similar to Codex, we only set args (not command) to preserve the
        image's entrypoint that sets up the environment.
        """
        # Get the script to execute
        script = self._build_opencode_script(execution_context)

        # Store script in execution context
        execution_context["_opencode_script"] = script

        # Set command and args for Kubernetes.
        # Unlike codex-universal (whose ENTRYPOINT runs `exec bash "$@"`),
        # the OpenCode image has no shell-forwarding entrypoint, so we must
        # explicitly set the command to /bin/bash.
        execution_context["_container_command"] = ["/bin/bash"]
        execution_context["_container_args"] = ["-c", script]

        # Prepare OpenCode-specific environment variables
        opencode_env = await self._prepare_environment(execution_context)

        # Add account API token for Preloop MCP authentication
        account_api_token = execution_context.get("account_api_token")
        if account_api_token:
            opencode_env["PRELOOP_API_TOKEN"] = account_api_token
        else:
            self.logger.warning("No account API token provided for Preloop MCP access")

        # Set Preloop MCP URL (for Kubernetes)
        opencode_env["PRELOOP_MCP_URL"] = os.getenv(
            "PRELOOP_MCP_URL_K8S",
            os.getenv("PRELOOP_MCP_URL", "http://preloop-api:8000/mcp/v1"),
        )

        # Add MCP_TOOL_TIMEOUT_SEC
        mcp_timeout = execution_context.get("_mcp_tool_timeout", 600)
        opencode_env["MCP_TOOL_TIMEOUT_SEC"] = str(mcp_timeout)
        self.logger.info(
            f"Set MCP_TOOL_TIMEOUT_SEC={mcp_timeout} for OpenCode (Kubernetes)"
        )

        # Add MCP configuration
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])

        if allowed_mcp_servers or allowed_mcp_tools:
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            opencode_env.update(mcp_env)

            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            opencode_env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        execution_context["_agent_env"] = opencode_env

        # Call parent implementation
        return await super()._start_kubernetes_pod(execution_context)

    async def _prepare_environment(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Prepare OpenCode-specific environment variables.

        OpenCode is provider-agnostic — set the API key env var that matches
        the configured provider (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY).

        Args:
            execution_context: Execution context

        Returns:
            Environment variables dict
        """
        env = {}

        # Add API key for the configured provider
        model_provider = execution_context.get("model_provider", "anthropic").lower()
        if execution_context.get("model_gateway_enabled"):
            gateway_provider = (
                execution_context.get("model_gateway_provider") or "preloop"
            ).lower()
            gateway_token = execution_context.get("model_gateway_token")
            if gateway_token:
                provider_env_key = (
                    f"{gateway_provider.upper().replace('-', '_')}_API_KEY"
                )
                env[provider_env_key] = gateway_token
                env["OPENAI_API_KEY"] = gateway_token
                env["PRELOOP_MODEL_GATEWAY_TOKEN"] = gateway_token
        elif "model_api_key" in execution_context:
            # Set the provider-specific env var
            provider_env_key = f"{model_provider.upper().replace('-', '_')}_API_KEY"
            env[provider_env_key] = execution_context["model_api_key"]

            # Also set OPENAI_API_KEY as fallback for OpenAI-compatible providers
            if model_provider != "openai":
                env["OPENAI_API_KEY"] = execution_context["model_api_key"]

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
