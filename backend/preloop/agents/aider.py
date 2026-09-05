"""Aider agent implementation."""

import json
import logging
import os
from typing import Any, Dict

from aiodocker.exceptions import DockerError

from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api

from .container import ContainerAgentExecutor
from .images import default_agent_image

logger = logging.getLogger(__name__)


class AiderAgent(ContainerAgentExecutor):
    """
    Aider CE agent executor.

    Runs Aider CE (Community Edition with MCP support) in a Docker container
    for autonomous coding tasks with access to Preloop MCP tools.

    Note: Uses dustinwashington/aider-ce Docker image which includes MCP support.
    The official paulgauthier/aider image does not support MCP servers.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Aider agent.

        Args:
            config: Agent configuration including:
                - model: AI model to use (default: gpt-5.4)
                - edit_format: Edit format (default: whole)
                - custom settings for Aider
        """
        image = default_agent_image("aider") or "dustinwashington/aider-ce:v0.88.6"

        super().__init__(
            agent_type="aider",
            config=config,
            image=image,
            use_kubernetes=os.getenv("USE_KUBERNETES", "false").lower() == "true",
        )

    async def start(self, execution_context: Dict[str, Any]) -> str:
        """
        Start Aider agent with specialized configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID or pod name
        """
        # Enhance execution context with Aider-specific settings
        aider_context = execution_context.copy()

        # Extract Aider config
        agent_config = execution_context.get("agent_config", {})

        # Set Aider model - prefer model_identifier from AIModel, fall back to agent_config
        model = (
            (
                execution_context.get("model_gateway_model_alias")
                if execution_context.get("model_gateway_enabled")
                else None
            )
            or execution_context.get("model_identifier")
            or agent_config.get("model")
            or "gpt-5.4"
        )
        aider_context["aider_model"] = model

        # Set edit format
        edit_format = agent_config.get("edit_format", "whole")
        aider_context["aider_edit_format"] = edit_format

        self.logger.info(
            f"Starting Aider with model={model}, edit_format={edit_format}"
        )

        # Start the container with enhanced context
        return await super().start(aider_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start Aider in a Docker container.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Prepare Aider-specific environment variables
        env = await self._prepare_environment(execution_context)

        # Add MCP configuration using MCP config service
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])
        account_api_token = execution_context.get("account_api_token")

        self.logger.info(
            f"MCP Configuration Check: "
            f"allowed_mcp_servers={allowed_mcp_servers}, "
            f"allowed_mcp_tools={allowed_mcp_tools}, "
            f"account_api_token={'present' if account_api_token else 'missing'}"
        )

        if allowed_mcp_servers or allowed_mcp_tools:
            self.logger.info("Generating MCP configuration for Aider CE container")

            # Generate MCP environment variables
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            env.update(mcp_env)

            # Add account API token for Preloop MCP authentication
            if account_api_token:
                env["PRELOOP_API_TOKEN"] = account_api_token
                self.logger.info("Added PRELOOP_API_TOKEN to environment")
            else:
                self.logger.warning(
                    "No account API token provided for Preloop MCP access"
                )

            # Generate MCP config file
            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)
            self.logger.info(
                f"MCP config generated with {len(allowed_mcp_servers)} servers and "
                f"{len(allowed_mcp_tools)} tools. Config size: {len(env['MCP_CONFIG_JSON'])} chars"
            )
            self.logger.debug(f"MCP config content: {mcp_config}")
        else:
            self.logger.warning(
                "No MCP servers or tools configured for this flow execution. "
                "Aider CE will run without MCP tool access."
            )

        # Build the command to run Aider with the prompt
        prompt = execution_context["prompt"]
        model = execution_context.get("aider_model", "gpt-5.4")
        edit_format = execution_context.get("aider_edit_format", "whole")

        # Escape prompt for shell (use single quotes to avoid escaping issues)
        escaped_prompt = prompt.replace("'", "'\\''")

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
# Run post-execution commands (push, PR/MR) if aider succeeded
if [ "$AIDER_EXIT_CODE" -eq "0" ]; then
    echo "========================================="
    echo "Running post-execution git operations..."
    echo "========================================="
    {post_exec_commands}
fi
"""

        # Use bash wrapper to capture exit codes properly
        # Aider may not exit with non-zero on errors, so we check for error patterns
        aider_cmd = f"""
set -e
set -o pipefail

# Run initialization commands (git clone, custom commands) if any
{init_commands}

# Ensure workspace is writable (create if doesn't exist)
if [ ! -d /workspace ]; then
    echo "Creating /workspace directory..."
    mkdir -p /workspace
fi

# Configure git to trust the workspace directory
git config --global --add safe.directory /workspace

# Trust all git repositories in workspace (needed for cloned repos)
git config --global --add safe.directory '*'

# Create .aider directory in workspace if it doesn't exist
if [ ! -d /workspace/.aider ]; then
    echo "Creating /workspace/.aider directory..."
    mkdir -p /workspace/.aider/caches
    chmod -R 755 /workspace/.aider 2>/dev/null || true
fi

# Set up MCP configuration if provided
# Aider looks for MCP config at ~/.aider/mcp_settings.json by default
echo "========================================="
echo "Checking for MCP configuration..."
echo "MCP_CONFIG_JSON environment variable length: ${{#MCP_CONFIG_JSON}}"
echo "PRELOOP_API_TOKEN set: $([ ! -z \\"$PRELOOP_API_TOKEN\\" ] && echo 'yes' || echo 'no')"
echo "PRELOOP_MCP_URL: ${{PRELOOP_MCP_URL:-not set}}"
echo "========================================="

if [ ! -z "$MCP_CONFIG_JSON" ]; then
    echo "========================================="
    echo "Setting up MCP configuration..."
    echo "========================================="
    echo "$MCP_CONFIG_JSON" | tee /workspace/.aider/mcp_settings.json
    chmod 644 /workspace/.aider/mcp_settings.json
    echo "========================================="
    echo "MCP config written to /workspace/.aider/mcp_settings.json"
    echo "Content verification:"
    cat /workspace/.aider/mcp_settings.json
    echo ""
    echo "========================================="
else
    echo "========================================="
    echo "WARNING: MCP_CONFIG_JSON is not set!"
    echo "MCP tools will NOT be available to Aider CE"
    echo "========================================="
fi

# Configure litellm to drop unsupported parameters
export LITELLM_DROP_PARAMS=True

# Prepare model name with provider prefix if needed for litellm
# Some providers need explicit prefixes, OpenAI models work without prefix
MODEL_NAME="{model}"

if [ "{"true" if execution_context.get("model_gateway_enabled") else "false"}" = "true" ]; then
    if [[ "$MODEL_NAME" != openai/* ]]; then
        MODEL_NAME="openai/$MODEL_NAME"
    fi
    echo "Using Preloop OpenAI-compatible gateway model: $MODEL_NAME"
# Check if model already has a provider prefix (contains /)
elif [[ "$MODEL_NAME" != */* ]]; then
    # No prefix present, check if we need to add one based on model name
    if [[ "$MODEL_NAME" == deepseek-* ]] || [[ "$MODEL_NAME" == *deepseek* ]]; then
        MODEL_NAME="deepseek/$MODEL_NAME"
        echo "Using DeepSeek provider: $MODEL_NAME"
    elif [[ "$MODEL_NAME" == claude-* ]] || [[ "$MODEL_NAME" == *anthropic* ]]; then
        MODEL_NAME="anthropic/$MODEL_NAME"
        echo "Using Anthropic provider: $MODEL_NAME"
    elif [[ "$MODEL_NAME" == gemini-* ]] || [[ "$MODEL_NAME" == *gemini* ]]; then
        MODEL_NAME="gemini/$MODEL_NAME"
        echo "Using Google Gemini provider: $MODEL_NAME"
    elif [[ "$MODEL_NAME" == gpt-* ]] || [[ "$MODEL_NAME" == o1-* ]] || [[ "$MODEL_NAME" == *openai* ]]; then
        echo "Using OpenAI model: $MODEL_NAME"
    else
        echo "Using model: $MODEL_NAME"
    fi
else
    echo "Using model with explicit provider: $MODEL_NAME"
fi

# Run aider-ce with properly formatted model name
# Aider CE will automatically look for MCP config at /workspace/.aider/mcp_settings.json
echo "========================================="
echo "Starting Aider CE..."
echo "Model: $MODEL_NAME"
echo "Edit format: {edit_format}"
echo "MCP config file exists: $([ -f /workspace/.aider/mcp_settings.json ] && echo 'yes' || echo 'no')"
if [ -f /workspace/.aider/mcp_settings.json ]; then
    echo "MCP config file size: $(wc -c < /workspace/.aider/mcp_settings.json) bytes"
fi
echo "========================================="

OUTPUT=$(aider-ce --model "$MODEL_NAME" --edit-format {edit_format} --yes --no-suggest-shell-commands --message '{escaped_prompt}' 2>&1) || EXIT_CODE=$?

# Print the output
echo "$OUTPUT"

# Check for error patterns in output
if echo "$OUTPUT" | grep -qiE "(error|exception|failed|fatal|traceback)"; then
    echo "[WRAPPER] Detected error in aider output, will skip git operations"
    AIDER_EXIT_CODE=1
else
    AIDER_EXIT_CODE=${{EXIT_CODE:-0}}
fi
{post_exec_block}
# Exit with aider's exit code
exit $AIDER_EXIT_CODE
"""

        # Container configuration
        container_config = {
            "Image": self.image,
            "Env": [
                f"{k}={v}"
                for k, v in self._apply_git_credential_env(
                    env, execution_context
                ).items()
            ],
            "Entrypoint": ["bash", "-c"],  # Override image entrypoint
            "Cmd": [aider_cmd],  # Pass the script as single argument to bash -c
            "WorkingDir": "/workspace",
            # Note: Not setting User to avoid permission issues with volume mounts
            # The aider-ce image runs as root by default which is acceptable for isolated containers
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
                # Use tmpfs for workspace to avoid volume permission issues
                # tmpfs is an in-memory filesystem that's automatically cleaned up
                "Tmpfs": {"/workspace": "rw,size=2g,mode=1777"},
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
                f"Started Aider container {container_id[:12]} for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start Aider container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start Aider container: {e}")

    async def _prepare_environment(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Prepare Aider-specific environment variables.

        Args:
            execution_context: Execution context

        Returns:
            Environment variables dict
        """
        env = {}

        # Set aider directories to /workspace to avoid permission issues
        # Aider tries to write to /app/.aider by default which is not writable
        env["AIDER_CACHE_DIR"] = "/workspace/.aider/caches"
        env["AIDER_HISTORY_FILE"] = "/workspace/.aider/.aider.chat.history.md"
        env["AIDER_INPUT_HISTORY_FILE"] = "/workspace/.aider/.aider.input.history"

        # Disable version check to avoid permission issues
        env["AIDER_CHECK_UPDATE"] = "false"

        if execution_context.get("model_gateway_enabled"):
            gateway_token = execution_context.get("model_gateway_token")
            if gateway_token:
                env["OPENAI_API_KEY"] = gateway_token
                env["PRELOOP_MODEL_GATEWAY_TOKEN"] = gateway_token

            gateway_url = gateway_url_for_api(
                execution_context.get("model_gateway_url")
                or execution_context.get("model_endpoint"),
                "openai",
            )
            if gateway_url:
                env["OPENAI_API_BASE"] = gateway_url
        # Add AI model configuration based on provider
        elif "model_api_key" in execution_context:
            api_key = execution_context["model_api_key"]
            model_provider = execution_context.get("model_provider", "").lower()

            # Set provider-specific environment variables
            if model_provider == "deepseek":
                env["DEEPSEEK_API_KEY"] = api_key
            elif model_provider == "anthropic":
                env["ANTHROPIC_API_KEY"] = api_key
            elif model_provider == "openai":
                env["OPENAI_API_KEY"] = api_key
            elif model_provider == "google" or model_provider == "gemini":
                env["GEMINI_API_KEY"] = api_key
                env["GOOGLE_API_KEY"] = api_key  # Some tools use this
            elif model_provider == "cohere":
                env["COHERE_API_KEY"] = api_key
            elif model_provider == "azure":
                env["AZURE_API_KEY"] = api_key
            else:
                # Default to OpenAI for unknown providers
                env["OPENAI_API_KEY"] = api_key
                # Also set a generic API key that some models might use
                env[f"{model_provider.upper()}_API_KEY"] = api_key

        return env
