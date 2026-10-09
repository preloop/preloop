"""Pi and DeepSeek Harness execution on ephemeral Docker/Kubernetes workers."""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api
from preloop.utils.execve_limits import (
    PROMPT_FILE_PATH,
    build_prompt_materialization_shell,
)

from .container import ContainerAgentExecutor, KUBERNETES_AVAILABLE
from .images import default_agent_image
from .kubernetes import detect_kubernetes_environment


class ExtensionHarnessAgent(ContainerAgentExecutor):
    """Run a pinned harness with the same native plugin used by CLI onboarding."""

    harness: str
    supports_confirmation_nudge = True

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(
            agent_type=self.harness,
            config=config,
            image=config.get("image")
            or config.get("docker_image")
            or default_agent_image(self.harness)
            or "node:22-bookworm",
            use_kubernetes=detect_kubernetes_environment(),
        )

    async def start(self, execution_context: dict[str, Any]) -> str:
        context = dict(execution_context)
        context["_agent_env"] = await self._prepare_environment(context)
        # Docker bootstraps fresh volume ownership; Kubernetes uses fsGroup.
        context["_container_user"] = (
            "10000:10000" if self.use_kubernetes and KUBERNETES_AVAILABLE else "0:0"
        )
        context["_container_command"] = ["/bin/bash"]
        context["_container_args"] = ["-c", self._build_harness_script(context)]
        return await super().start(context)

    async def _prepare_environment(self, context: dict[str, Any]) -> dict[str, str]:
        gateway = bool(context.get("model_gateway_enabled"))
        model = (
            (context.get("model_gateway_model_alias") if gateway else None)
            or context.get("model_identifier")
            or (context.get("agent_config") or {}).get("model")
        )
        if not model:
            raise ValueError(f"No model specified for {self.harness} agent")
        provider = str(context.get("model_provider") or "openai").lower()
        endpoint = (
            gateway_url_for_api(context.get("model_gateway_url"), "openai")
            if gateway
            else context.get("model_endpoint")
        )
        api = "openai-completions"
        if not gateway and provider == "anthropic":
            api = "anthropic-messages"
            endpoint = endpoint or "https://api.anthropic.com"
        if not endpoint:
            endpoint = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com",
                "openrouter": "https://openrouter.ai/api/v1",
            }.get(provider)
        if not endpoint:
            raise ValueError(f"A model endpoint is required for provider {provider}")
        token = context.get("model_gateway_token" if gateway else "model_api_key")
        if not token:
            raise ValueError(f"A model credential is required for {self.harness}")
        model_entry: dict[str, Any] = {"id": model}
        parameters = context.get("model_parameters") or {}
        max_tokens = parameters.get("max_output_tokens") or parameters.get("max_tokens")
        if (
            isinstance(max_tokens, int)
            and not isinstance(max_tokens, bool)
            and max_tokens > 0
        ):
            model_entry["maxTokens"] = max_tokens
        api_url = os.getenv("PRELOOP_URL", "http://host.docker.internal:8000").rstrip(
            "/"
        )
        mcp_url = os.getenv(
            "PRELOOP_MCP_URL_K8S" if self.use_kubernetes else "PRELOOP_MCP_URL"
        )
        if mcp_url:
            api_url = mcp_url.removesuffix("/mcp/v1")
        mcp_config = MCPConfigService.generate_mcp_config(
            context.get("allowed_mcp_servers") or [],
            context.get("allowed_mcp_tools") or [],
            preloop_url=api_url,
        )
        # Add auth after generation: the service logs the generated document.
        for server in mcp_config["mcpServers"].values():
            server["headers"] = {
                "Authorization": "Bearer " + (context.get("account_api_token") or "")
            }
        return {
            "PRELOOP_HARNESS": self.harness,
            "PRELOOP_HARNESS_MODEL": json.dumps(
                {"api": api, "baseUrl": endpoint, "models": [model_entry]}
            ),
            "PRELOOP_MODEL_TOKEN": str(token),
            "PRELOOP_API_TOKEN": context.get("account_api_token") or "",
            "PRELOOP_API_URL": api_url,
            "MCP_CONFIG_JSON": json.dumps(mcp_config),
            "PI_CODING_AGENT_DIR": "/tmp/preloop-pi",
            "DSH_HOME": "/tmp/preloop-dsh",
            "PRELOOP_NATIVE_APPROVALS": "on"
            if (context.get("agent_config") or {}).get("native_tool_approvals")
            else "off",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "DSH_TELEMETRY_DISABLED": "true",
            "HOME": "/tmp/preloop-home",
        }

    def _build_harness_script(self, context: dict[str, Any]) -> str:
        """Deliver prompts over stdin and preserve the harness's failure status."""
        package, command = (
            (
                "@earendil-works/pi-coding-agent@0.85.1",
                "pi --print --mode text --extension /opt/preloop-harness/pi.mjs",
            )
            if self.harness == "pi"
            else ("@deepseek-ai/dsh@0.1.5-rc.2", "dsh --profile headless")
        )
        # Custom worker images may bake both packages. Generic Node workers
        # install to writable /tmp; no root permission or global npm install.
        executable = "pi" if self.harness == "pi" else "dsh"
        version = package.rsplit("@", 1)[1]
        plugin_install = "npm install --ignore-scripts --no-audit --no-fund --prefix /tmp/preloop-harness-tools @preloop-ai/harness-plugin@0.1.0"
        runtime_install = f"npm install --ignore-scripts --no-audit --no-fund --prefix /tmp/preloop-harness-tools {package}"
        if self.environment_profile:
            plugin_install = "echo PRELOOP_SETUP_FAILED missing_harness_plugin; exit 78"
            runtime_install = "echo PRELOOP_SETUP_FAILED environment_harness_version_mismatch; exit 78"
        init = self._prepare_init_commands(context)
        workspace = self._primary_workspace_path(
            context, context.get("git_clone_config") or {}
        )
        post = self._prepare_git_post_execution_commands(context)
        command = command.replace("/opt/preloop-harness/", '"$PRELOOP_PLUGIN_DIR"/')
        root_bootstrap = ""
        if context.get("_container_user") != "10000:10000":
            root_bootstrap = """
# Docker creates fresh named volumes as root.
if [ "$(id -u)" -eq 0 ]; then
    mkdir -p /workspace /tmp/preloop-home
    chown -R 10000:10000 /workspace /tmp/preloop-home
    if [ -n "${PRELOOP_DOCKER_SCRIPT_PATH:-}" ] && [ -r "$PRELOOP_DOCKER_SCRIPT_PATH" ]; then
        # A large script arrived chunked and runs from a file, where
        # BASH_EXECUTION_STRING is unset.
        exec setpriv --reuid 10000 --regid 10000 --clear-groups /bin/bash "$PRELOOP_DOCKER_SCRIPT_PATH"
    fi
    exec setpriv --reuid 10000 --regid 10000 --clear-groups /bin/bash -c "${BASH_EXECUTION_STRING:-${PRELOOP_RUNNER_SCRIPT:?Missing harness script}}"
fi
"""
        return f"""set -euo pipefail
umask 077
{root_bootstrap}
mkdir -p /tmp/preloop-home
export npm_config_cache=/tmp/preloop-npm-cache
export PATH="/tmp/preloop-harness-tools/node_modules/.bin:$PATH"
PRELOOP_PLUGIN_DIR=/opt/preloop-harness
if [ ! -f "$PRELOOP_PLUGIN_DIR/bootstrap.mjs" ]; then
    {plugin_install}
    PRELOOP_PLUGIN_DIR=/tmp/preloop-harness-tools/node_modules/@preloop-ai/harness-plugin
fi
if ! command -v {executable} >/dev/null || [ "$({executable} --version)" != {shlex.quote(version)} ]; then
    {runtime_install}
fi
{build_prompt_materialization_shell(context["prompt"])}
node "$PRELOOP_PLUGIN_DIR/bootstrap.mjs"
{init}
cd {shlex.quote(workspace)}
echo PRELOOP_AGENT_EXEC_START
set +e
{command} < {PROMPT_FILE_PATH}
PRELOOP_HARNESS_EXIT=$?
set -e
if [ "$PRELOOP_HARNESS_EXIT" -eq 0 ]; then
    {post or ":"}
fi
exit "$PRELOOP_HARNESS_EXIT"
"""


class PiAgent(ExtensionHarnessAgent):
    """Pi coding agent executor."""

    harness = "pi"


class DeepSeekAgent(ExtensionHarnessAgent):
    """DeepSeek Harness (dsh) executor, independent of the model provider."""

    harness = "deepseek"
