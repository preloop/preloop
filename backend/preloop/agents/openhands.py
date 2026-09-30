"""OpenHands agent implementation."""

import json
import logging
import os
import shlex
from typing import Any, Dict

from aiodocker.exceptions import DockerError

from preloop.agents.resources import docker_memory_bytes
from preloop.services.mcp_config_service import MCPConfigService
from preloop.services.model_runtime_resolver import gateway_url_for_api
from preloop.utils.execve_limits import (
    MAX_LEGACY_PROMPT_BYTES,
    PROMPT_FILE_PATH,
    build_prompt_materialization_shell,
    prompt_transport_env,
)
from preloop.utils.git_credentials import (
    GitCredential,
    build_credential_setup_shell,
    needs_http_path_scoping,
    strip_url_credentials,
)

from .container import ContainerAgentExecutor
from .images import default_agent_image

logger = logging.getLogger(__name__)


class OpenHandsAgent(ContainerAgentExecutor):
    """
    OpenHands agent executor.

    Runs OpenHands (formerly OpenDevin) in a Docker container for
    autonomous software development tasks.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize OpenHands agent.

        Args:
            config: Agent configuration including:
                - agent_type: Specific OpenHands agent type (CodeActAgent, etc.)
                - max_iterations: Maximum number of agent iterations
                - custom settings for OpenHands
        """
        image = default_agent_image("openhands") or "spacebridge/openhands:latest-tmux"

        super().__init__(
            agent_type="openhands",
            config=config,
            image=image,
            use_kubernetes=os.getenv("USE_KUBERNETES", "false").lower() == "true",
        )

    async def start(self, execution_context: Dict[str, Any]) -> str:
        """
        Start OpenHands agent with specialized configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID or pod name
        """
        # Enhance execution context with OpenHands-specific settings
        openhands_context = execution_context.copy()

        # Extract OpenHands agent config
        agent_config = execution_context.get("agent_config", {})

        # Set OpenHands agent type (CodeActAgent, PlannerAgent, etc.)
        openhands_agent_type = agent_config.get("agent_type", "CodeActAgent")
        openhands_context["openhands_agent_type"] = openhands_agent_type

        # Set max iterations
        max_iterations = agent_config.get("max_iterations", 10)
        openhands_context["max_iterations"] = max_iterations

        self.logger.info(
            f"Starting OpenHands with agent_type={openhands_agent_type}, "
            f"max_iterations={max_iterations}"
        )

        # Start the container with enhanced context
        return await super().start(openhands_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start OpenHands in a Docker container with headless mode configuration.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Prepare OpenHands-specific environment variables
        env = await self._prepare_environment(execution_context)

        # Add MCP configuration using MCP config service
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])
        account_api_token = execution_context.get("account_api_token")

        if allowed_mcp_servers or allowed_mcp_tools:
            # Generate MCP environment variables
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            env.update(mcp_env)

            # Add account API token for Preloop MCP authentication
            if account_api_token:
                env["PRELOOP_API_TOKEN"] = account_api_token
            else:
                self.logger.warning(
                    "No account API token provided for Preloop MCP access"
                )

            # Generate MCP config file (will be used by agents that support config files)
            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        # The prompt travels as base64 chunks and is reassembled inside the
        # container. It is never interpolated into this bash -c string, so a
        # quote in a PR body cannot break -t and a large prompt cannot become
        # one execve string (preloop.utils.execve_limits).
        cmd = [
            "bash",
            "-c",
            self._build_openhands_script(execution_context),
        ]

        # Container configuration
        container_config = {
            "Image": self.image,
            "Env": [
                f"{k}={v}"
                for k, v in self._apply_git_credential_env(
                    env, execution_context
                ).items()
            ],
            # Override entrypoint completely - set to empty list to disable entrypoint.sh
            "Entrypoint": [],
            # Run OpenHands in headless mode
            "Cmd": cmd,
            "WorkingDir": "/app",
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
                "Memory": docker_memory_bytes(os.getenv("AGENT_MEMORY_LIMIT", "4g")),
                "CpuQuota": int(os.getenv("AGENT_CPU_QUOTA", "100000")),
            },
        }

        self._guard_docker_launch_payload(
            container_config, what=f"{self.agent_type} container for {execution_id}"
        )

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
                f"Started OpenHands container {container_id[:12]} in headless mode for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start OpenHands container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start OpenHands container: {e}")

    def _build_openhands_script(self, execution_context: Dict[str, Any]) -> str:
        """Build the Docker/Kubernetes bash script for an OpenHands launch.

        The rendered prompt is not interpolated here. It arrives as base64
        chunks in the environment and is reassembled into
        :data:`PROMPT_FILE_PATH` by the materialization block. ``-t`` then
        reads that file through quoted command substitution, so a ``"`` in
        the prompt cannot close the shell string.

        The inner ``python -m openhands.core.main -t "$(cat ...)"`` still
        expands the prompt into that process's argv. Gemini and OpenCode
        no longer do that (issue #692); this OpenHands residual is left
        alone.
        """
        prompt = execution_context["prompt"]
        max_iterations = execution_context.get("max_iterations", 10)
        init_commands = self._prepare_init_commands(execution_context)
        prompt_block = build_prompt_materialization_shell(prompt)
        # Double quotes around $(cat ...) preserve whitespace. The
        # substitution result is not re-parsed, so quotes in the file do
        # not need escaping.
        task_flag = f'-t "$(cat {PROMPT_FILE_PATH})"'
        launch = (
            "cd /app && /app/.venv/bin/python -m openhands.core.main "
            f"{task_flag} -i {max_iterations}"
        )
        body = f"{init_commands} && {launch}" if init_commands else launch
        return f"""set -e
# Materialize the rendered prompt from its chunked environment transport.
{prompt_block}

{body}
"""

    async def _start_kubernetes_pod(self, execution_context: Dict[str, Any]) -> str:
        """Run the same OpenHands script on Kubernetes as on Docker."""
        script = self._build_openhands_script(execution_context)
        execution_context["_container_command"] = ["bash"]
        execution_context["_container_args"] = ["-c", script]
        execution_context["_agent_env"] = await self._prepare_environment(
            execution_context
        )
        return await super()._start_kubernetes_pod(execution_context)

    async def _prepare_environment(
        self, execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Prepare OpenHands-specific environment variables.

        Args:
            execution_context: Execution context

        Returns:
            Environment variables dict
        """
        prompt = execution_context["prompt"]
        env = {
            "AGENT_TYPE": execution_context.get("openhands_agent_type", "CodeActAgent"),
            "MAX_ITERATIONS": str(execution_context.get("max_iterations", 10)),
            "RUNTIME": "local",  # Use local runtime - runs directly in the container without Docker-in-Docker
            "WORKSPACE_BASE": "/workspace",  # Working directory for the agent
        }
        # Chunked PRELOOP_AGENT_PROMPT_* plus AGENT_PROMPT_FILE. AGENT_PROMPT
        # is set only while the prompt is <= 64 KiB.
        env.update(prompt_transport_env(prompt))
        # Historic OpenHands PROMPT env: same 64 KiB budget as AGENT_PROMPT
        # so it cannot be the execve string that breaks the launch.
        if len(prompt.encode("utf-8")) <= MAX_LEGACY_PROMPT_BYTES:
            env["PROMPT"] = prompt

        # Add AI model configuration
        if execution_context.get("model_gateway_enabled"):
            model = execution_context.get(
                "model_gateway_model_alias"
            ) or execution_context.get("model_identifier")
            if model:
                env["LLM_MODEL"] = (
                    model if str(model).startswith("openai/") else f"openai/{model}"
                )
            gateway_token = execution_context.get("model_gateway_token")
            if gateway_token:
                env["LLM_API_KEY"] = gateway_token
                env["OPENAI_API_KEY"] = gateway_token
                env["PRELOOP_MODEL_GATEWAY_TOKEN"] = gateway_token
            gateway_url = gateway_url_for_api(
                execution_context.get("model_gateway_url")
                or execution_context.get("model_endpoint"),
                "openai",
            )
            if gateway_url:
                env["LLM_BASE_URL"] = gateway_url
                env["LLM_API_BASE"] = gateway_url
                env["OPENAI_API_BASE"] = gateway_url
            env["LLM_PROVIDER"] = "openai"
        else:
            if "model_identifier" in execution_context:
                env["LLM_MODEL"] = execution_context["model_identifier"]
            if "model_api_key" in execution_context:
                env["LLM_API_KEY"] = execution_context["model_api_key"]
            if "model_provider" in execution_context:
                env["LLM_PROVIDER"] = execution_context["model_provider"]

        # Add model parameters if specified
        model_params = execution_context.get("model_parameters") or {}
        if model_params and "temperature" in model_params:
            env["LLM_TEMPERATURE"] = str(model_params["temperature"])
        if model_params and "max_tokens" in model_params:
            env["LLM_MAX_TOKENS"] = str(model_params["max_tokens"])

        # MCP configuration is already added by ContainerAgentExecutor
        # OpenHands can access MCP tools via the environment variables:
        # - MCP_ALLOWED_SERVERS: comma-separated list of allowed servers
        # - MCP_ALLOWED_TOOLS: JSON map of server -> [tools]
        # - PRELOOP_MCP_URL: URL to Preloop MCP endpoint

        return env

    def _prepare_init_commands(self, execution_context: Dict[str, Any]) -> str:
        """
        Prepare initialization commands (git clone, custom commands).

        Args:
            execution_context: Execution context

        Returns:
            Shell command string to run before agent starts, or empty string if none
        """
        commands = []

        # Prepare git clone command if enabled
        git_clone_config = execution_context.get("git_clone_config")
        self.logger.info(f"Git clone config: {git_clone_config}")

        if git_clone_config:
            is_enabled = git_clone_config.get("enabled", False)
            repositories = git_clone_config.get("repositories", [])
            trigger_project_id = execution_context.get("trigger_project_id")

            self.logger.info(
                f"Git clone check: enabled={is_enabled}, "
                f"repositories={len(repositories)}, "
                f"trigger_project_id={trigger_project_id}"
            )

            # Attempt clone if: has repositories OR (enabled AND has trigger project)
            if repositories or (is_enabled and trigger_project_id):
                git_cmd = self._prepare_git_clone_command(execution_context)
                if git_cmd:
                    commands.append(git_cmd)
                    self.logger.info(
                        "Git clone commands added (length=%d)", len(git_cmd)
                    )
                else:
                    self.logger.warning(
                        "Git clone was configured but no commands were generated"
                    )
        else:
            self.logger.debug("No git_clone_config in execution context")

        # Review baseline resolved from a previous execution (see base
        # class): before the seeds, so an explicit seed at the same path
        # is written last and wins.
        baseline_cmd = self._prepare_workspace_baseline_commands(execution_context)
        if baseline_cmd:
            commands.append(baseline_cmd)

        # Seed /workspace files declared on the trigger payload (see base
        # class): after git clone, before custom commands.
        seed_cmd = self._prepare_workspace_seed_commands(execution_context)
        if seed_cmd:
            commands.append(seed_cmd)

        # Prepare custom commands if enabled
        custom_commands = execution_context.get("custom_commands")
        if custom_commands and custom_commands.get("enabled"):
            custom_cmds = custom_commands.get("commands", [])
            for cmd in custom_cmds:
                # Sanitize command to prevent shell injection
                # Note: These commands come from admin-only configuration
                commands.append(cmd)

        # Join all commands with &&
        if commands:
            return " && ".join(commands)
        return ""

    def _prepare_git_clone_command(self, execution_context: Dict[str, Any]) -> str:
        """
        Prepare git clone commands for multiple repositories.

        Args:
            execution_context: Execution context

        Returns:
            Git clone commands string (multiple commands joined with &&) or empty string
        """
        try:
            git_config = execution_context.get("git_clone_config", {})
            repositories = git_config.get("repositories", [])

            # If no repositories configured but git clone is enabled,
            # create a default repository entry using trigger project
            if not repositories:
                trigger_project_id = execution_context.get("trigger_project_id")
                if trigger_project_id:
                    self.logger.info(
                        f"No repositories configured, using trigger project: {trigger_project_id}"
                    )
                    # Create a virtual repository entry using trigger project
                    repositories = [
                        {
                            "project_id": trigger_project_id,
                            "clone_path": "/workspace",
                        }
                    ]
                else:
                    self.logger.warning(
                        "No repositories configured and no trigger project available for git clone"
                    )
                    return ""

            clone_commands = []
            credentials: Dict[int, GitCredential] = {}
            trigger_data = execution_context.get("trigger_event_data", {})
            trigger_project_id = execution_context.get("trigger_project_id")

            for idx, repo_config in enumerate(repositories):
                # Get repository URL
                repo_url = repo_config.get("repository_url")

                # If no URL, try to get from project or trigger event
                if not repo_url:
                    project_id = repo_config.get("project_id") or trigger_project_id
                    if project_id:
                        self.logger.info(
                            f"Using project {project_id} for repository #{idx + 1}"
                        )
                        # Try to extract from trigger event data
                        repo_url = self._extract_repo_url_from_trigger(trigger_data)

                if not repo_url:
                    self.logger.warning(
                        f"No repository URL found for repo #{idx + 1}. "
                        f"Trigger project ID: {trigger_project_id}"
                    )
                    continue

                # Resolve the tracker credential without touching the URL, so
                # the remote written into .git/config stays secret-free and
                # `git remote -v` cannot leak it (issue #173).
                credential = self._build_git_credential(
                    repo_url, repo_config, execution_context
                )
                if credential is not None:
                    credentials[idx] = credential

                repo_url = strip_url_credentials(repo_url)

                # Get clone path - if it starts with /, use as-is (absolute), otherwise make it relative to /workspace
                clone_path = repo_config.get("clone_path", f"workspace-{idx + 1}")
                if clone_path.startswith("/"):
                    # Absolute path - use as-is
                    full_path = clone_path
                else:
                    # Relative path - prepend /workspace/
                    full_path = f"/workspace/{clone_path}"

                # Get branch if specified
                branch = repo_config.get("branch")
                branch_arg = f" -b {shlex.quote(branch)}" if branch else ""

                # Build git clone command
                git_cmd = (
                    f"git clone{branch_arg} "
                    f"{shlex.quote(repo_url)} {shlex.quote(full_path)}"
                )
                clone_commands.append(git_cmd)

                self.logger.info(f"Prepared git clone command for {full_path}")

            if not clone_commands:
                return ""

            self._register_git_credentials(execution_context, credentials)

            # Create workspace directory, install the credential helper, then
            # clone all repos.
            all_commands = [
                "mkdir -p /workspace",
                build_credential_setup_shell(
                    use_http_path=needs_http_path_scoping(credentials.values())
                ),
            ] + clone_commands
            return " && ".join(all_commands)

        except Exception as e:
            self.logger.error(f"Error preparing git clone command: {e}", exc_info=True)
            return ""

    # Note: _extract_repo_url_from_trigger and _get_token_from_project are
    # inherited from ContainerAgentExecutor
