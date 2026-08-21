"""Container-based agent executor for Docker and Kubernetes."""

import base64
import binascii
import io
import json
import logging
import os
import shlex
import tarfile
from typing import Any, Dict, Optional

import aiodocker
from aiodocker.exceptions import DockerError

from .base import AgentExecutionResult, AgentExecutor, AgentStatus
from .failure_analysis import analyze_agent_failure
from preloop.services.mcp_config_service import MCPConfigService
from preloop.utils.git_credentials import (
    GitCredential,
    build_credential_env,
    build_credential_setup_shell,
    credential_username,
    git_token_env_var,
    needs_http_path_scoping,
    strip_url_credentials,
)
from preloop.utils.repo_urls import repo_url_log_location, tracker_host_kind
from preloop.utils.secret_scrubbing import scrub_secret_lines, scrub_secrets
from preloop.utils.workspace_seed import (
    build_workspace_seed_shell,
    parse_workspace_files,
)

logger = logging.getLogger(__name__)

# Path inside the agent container where eval/observe flows write their
# structured result report (see backend/presets/003-observe-eval.yaml).
RESULT_ARTIFACT_PATH = "/workspace/result.json"
# Guardrail: refuse to persist oversized artifacts (the preset asks agents to
# keep result.json small and reference workspace files for bulky output).
MAX_RESULT_ARTIFACT_BYTES = 256 * 1024

# Directory inside the agent container where audit-style presets write their
# evidence pack (see backend/presets/004..006). Captured as a tar.gz archive.
EVIDENCE_DIR_PATH = "/workspace/evidence"
# Cap on the COMPRESSED evidence archive. On Kubernetes the archive travels
# base64-encoded through the pod log stream, so this must stay comfortably
# inside the kubelet's default 10 MiB container-log rotation limit.
MAX_EVIDENCE_ARCHIVE_BYTES = 2 * 1024 * 1024

# Bounded tail for terminal-path pod log reads on Kubernetes. The artifact
# emission always TRAILS the agent output and its payload is capped by the two
# byte limits above, so a window of (worst-case emission lines + a generous
# status-scan window) is guaranteed to contain the COMPLETE emission plus at
# least as much real agent output as the pre-wrapper tail=1000 status read
# inspected. Worst case emission: both byte caps base64-encoded at the
# narrowest wrap width in the wild (60 cols), plus marker lines.
_WORST_CASE_EMISSION_LINES = (
    MAX_RESULT_ARTIFACT_BYTES + MAX_EVIDENCE_ARCHIVE_BYTES
) * 4 // (3 * 60) + 64
K8S_TERMINAL_LOG_TAIL_LINES = _WORST_CASE_EMISSION_LINES + 2000

# Marker-line prefix for the Kubernetes artifact log channel. Every line the
# emission wrapper prints starts with this prefix, so operator-facing log
# consumers can filter the (potentially large, base64) blocks statelessly.
# Grammar:
#   PRELOOP_ARTIFACT_BEGIN <channel> <status> [<size_bytes>]
#   PRELOOP_ARTIFACT_B64 <base64-chunk>          (0..n lines)
#   PRELOOP_ARTIFACT_END <channel>
# where <channel> is "result" or "evidence" and <status> is one of
# present | absent | too_large | error.
ARTIFACT_STREAM_LINE_PREFIX = "PRELOOP_ARTIFACT_"

# Environment variable carrying the original (unwrapped) agent script when the
# Kubernetes artifact-emission wrapper is applied.
K8S_INNER_SCRIPT_ENV = "PRELOOP_INNER_SCRIPT"

# Wrapper applied to Kubernetes agent scripts. Runs the unchanged agent script
# in a CHILD shell (so its own `trap ... EXIT` and `exit $rc` cannot skip the
# epilogue), then emits result.json and the evidence pack into stdout between
# PRELOOP_ARTIFACT_* markers. `base64 < file` (stdin form) is portable across
# GNU coreutils, busybox and BSD; wrapped or single-line output are both
# accepted by the parser.
#
# Security note: the emission duplicates result/evidence content into the pod
# log, where it is retained by the kubelet until log rotation and readable by
# anyone with pod-log access in the agent namespace. Deployments must keep
# that RBAC scoped as tightly as the account-scoped API/DB column. The
# object-storage follow-up (tracked on the PR) moves the evidence channel off
# the log stream entirely.
K8S_ARTIFACT_WRAPPER_SCRIPT = f"""
_preloop_emit_artifacts() {{
    if [ -f {RESULT_ARTIFACT_PATH} ]; then
        _pl_size=$(wc -c < {RESULT_ARTIFACT_PATH} | tr -d ' ')
        if [ "$_pl_size" -gt {MAX_RESULT_ARTIFACT_BYTES} ] 2>/dev/null; then
            echo "PRELOOP_ARTIFACT_BEGIN result too_large $_pl_size"
        else
            echo "PRELOOP_ARTIFACT_BEGIN result present $_pl_size"
            base64 < {RESULT_ARTIFACT_PATH} | sed 's/^/PRELOOP_ARTIFACT_B64 /'
        fi
        echo "PRELOOP_ARTIFACT_END result"
    else
        echo "PRELOOP_ARTIFACT_BEGIN result absent"
        echo "PRELOOP_ARTIFACT_END result"
    fi
    if [ -d {EVIDENCE_DIR_PATH} ]; then
        if tar -czf /tmp/preloop-evidence.tar.gz -C /workspace evidence 2>/dev/null; then
            _pl_esize=$(wc -c < /tmp/preloop-evidence.tar.gz | tr -d ' ')
            if [ "$_pl_esize" -gt {MAX_EVIDENCE_ARCHIVE_BYTES} ] 2>/dev/null; then
                echo "PRELOOP_ARTIFACT_BEGIN evidence too_large $_pl_esize"
            else
                echo "PRELOOP_ARTIFACT_BEGIN evidence present $_pl_esize"
                base64 < /tmp/preloop-evidence.tar.gz | sed 's/^/PRELOOP_ARTIFACT_B64 /'
            fi
        else
            echo "PRELOOP_ARTIFACT_BEGIN evidence error"
        fi
        echo "PRELOOP_ARTIFACT_END evidence"
    else
        echo "PRELOOP_ARTIFACT_BEGIN evidence absent"
        echo "PRELOOP_ARTIFACT_END evidence"
    fi
}}
if [ -z "${{{K8S_INNER_SCRIPT_ENV}}}" ]; then
    echo "ERROR: {K8S_INNER_SCRIPT_ENV} is not set" >&2
    exit 1
fi
bash -c "${{{K8S_INNER_SCRIPT_ENV}}}"
_preloop_rc=$?
_preloop_emit_artifacts
exit $_preloop_rc
"""


def _exception_message(exc: BaseException) -> str:
    """Return a useful message for exceptions whose str() is empty."""
    return str(exc) or exc.__class__.__name__


try:
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.client.rest import ApiException

    KUBERNETES_AVAILABLE = True
except ImportError:
    KUBERNETES_AVAILABLE = False
    logger.warning(
        "kubernetes_asyncio not available, Kubernetes execution will not be supported"
    )


class ContainerAgentExecutor(AgentExecutor):
    """
    Execute agents in isolated Docker containers or Kubernetes pods.

    This is the production-ready executor that runs agents in isolated
    environments with proper resource limits, networking, and security.
    """

    def __init__(
        self,
        agent_type: str,
        config: Dict[str, Any],
        image: str,
        use_kubernetes: bool = False,
    ):
        """
        Initialize the container agent executor.

        Args:
            agent_type: Type of agent
            config: Agent configuration
            image: Docker image to use for the agent
            use_kubernetes: Whether to use Kubernetes instead of Docker
        """
        super().__init__(agent_type, config)
        self.image = image
        self.use_kubernetes = use_kubernetes
        self._docker_client: Optional[aiodocker.Docker] = None
        self._containers: Dict[str, Any] = {}  # Track running containers
        self._k8s_initialized = False
        self._k8s_api_client: Optional[Any] = None  # Store ApiClient for proper cleanup
        self._k8s_batch_api: Optional[Any] = None
        self._k8s_core_api: Optional[Any] = None
        # Get agent namespace from environment or use default
        self.agent_namespace = os.getenv(
            "AGENT_EXECUTION_NAMESPACE", "agent-executions"
        )
        # One bounded pod-log read per finished job, shared by the terminal
        # path's three consumers (status scan, result channel, evidence
        # channel). Executor instances live for a single execution, so no
        # eviction is needed.
        self._k8s_terminal_log_cache: Dict[str, list[str]] = {}

    async def _get_docker_client(self) -> aiodocker.Docker:
        """Get or create Docker client."""
        if self._docker_client is None:
            self._docker_client = aiodocker.Docker()
        return self._docker_client

    async def _init_kubernetes_clients(self):
        """Initialize Kubernetes API clients."""
        if not KUBERNETES_AVAILABLE:
            raise RuntimeError("kubernetes_asyncio is not installed")

        if not self._k8s_initialized:
            # Load in-cluster config when running inside K8s, otherwise load from kubeconfig
            try:
                config.load_incluster_config()
                self.logger.info("Loaded in-cluster Kubernetes config")
            except config.ConfigException:
                await config.load_kube_config()
                self.logger.info("Loaded Kubernetes config from kubeconfig")

            # Create ApiClient for proper resource management
            self._k8s_api_client = client.ApiClient()
            self._k8s_batch_api = client.BatchV1Api(self._k8s_api_client)
            self._k8s_core_api = client.CoreV1Api(self._k8s_api_client)
            self._k8s_initialized = True

    async def aclose(self) -> None:
        """Release Docker and Kubernetes client connections."""
        if self._docker_client is not None:
            await self._docker_client.close()
            self._docker_client = None

        if self._k8s_api_client is not None:
            await self._k8s_api_client.close()
            self._k8s_api_client = None
            self._k8s_batch_api = None
            self._k8s_core_api = None
            self._k8s_initialized = False

    async def start(self, execution_context: Dict[str, Any]) -> str:
        """
        Start the agent in a Docker container or K8s pod.

        Args:
            execution_context: Execution context with prompt, config, etc.

        Returns:
            Container ID or K8s pod name as session reference
        """
        execution_id = execution_context["execution_id"]

        self.logger.info(
            f"Starting {self.agent_type} agent in container for execution {execution_id}"
        )

        # Check if Kubernetes is requested but not available - fall back to Docker
        if self.use_kubernetes and not KUBERNETES_AVAILABLE:
            self.logger.warning(
                "Kubernetes execution requested but kubernetes_asyncio is not available. "
                "Falling back to Docker execution."
            )
            return await self._start_docker_container(execution_context)

        if self.use_kubernetes:
            return await self._start_kubernetes_pod(execution_context)
        else:
            return await self._start_docker_container(execution_context)

    async def _start_docker_container(self, execution_context: Dict[str, Any]) -> str:
        """
        Start agent in a Docker container.

        Args:
            execution_context: Execution context

        Returns:
            Container ID
        """
        docker = await self._get_docker_client()
        execution_id = execution_context["execution_id"]

        # Prepare environment variables
        env = {
            "FLOW_ID": execution_context["flow_id"],
            "EXECUTION_ID": execution_id,
            "AGENT_PROMPT": execution_context["prompt"],
            "AGENT_CONFIG": str(execution_context.get("agent_config", {})),
        }

        # Add AI model credentials if available
        if "model_api_key" in execution_context:
            env["AI_MODEL_API_KEY"] = execution_context["model_api_key"]
        if "model_identifier" in execution_context:
            env["AI_MODEL"] = execution_context["model_identifier"]
        if "model_provider" in execution_context:
            env["AI_MODEL_PROVIDER"] = execution_context["model_provider"]

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

        # Create a writable workspace volume for the container
        # This ensures the agent has write permissions
        workspace_volume = f"agent-workspace-{execution_id}"

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
                    f"Setting container working directory to git repository: {working_dir}"
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
            "User": "10000:10000",  # Explicitly set user and group
            "WorkingDir": working_dir,  # Set working directory to git repo if configured
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
                # Mount workspace volume with proper permissions
                "Binds": [f"{workspace_volume}:/workspace:rw"],
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
                f"Started container {container_id[:12]} for execution {execution_id}"
            )
            return container_id

        except DockerError as e:
            self.logger.error(
                f"Failed to start container for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start agent container: {e}")

    async def _start_kubernetes_pod(self, execution_context: Dict[str, Any]) -> str:
        """
        Start agent in a Kubernetes Job.

        Args:
            execution_context: Execution context

        Returns:
            Job name (used as session reference)
        """
        await self._init_kubernetes_clients()

        execution_id = execution_context["execution_id"]
        flow_id = execution_context["flow_id"]

        # Generate unique job name (K8s names must be DNS-1123 compliant)
        job_name = f"agent-{execution_id}".replace("_", "-").lower()

        # Prepare environment variables
        # Start with agent-specific env if provided by any subclass.
        # Subclasses store their env under a generic key "_agent_env".
        # Falls back to "_codex_env" for backward compatibility.
        env = (
            execution_context.get("_agent_env")
            or execution_context.get("_codex_env")
            or {}
        ).copy()

        # Add base environment variables
        env.update(
            {
                "FLOW_ID": flow_id,
                "EXECUTION_ID": execution_id,
                "AGENT_PROMPT": execution_context["prompt"],
                "AGENT_CONFIG": str(execution_context.get("agent_config", {})),
            }
        )

        # Add AI model credentials if available (only if not already set by agent-specific env)
        if "model_api_key" in execution_context and "OPENAI_API_KEY" not in env:
            env["AI_MODEL_API_KEY"] = execution_context["model_api_key"]
        if "model_identifier" in execution_context:
            env["AI_MODEL"] = execution_context["model_identifier"]
        if "model_provider" in execution_context:
            env["AI_MODEL_PROVIDER"] = execution_context["model_provider"]

        # Add MCP configuration
        allowed_mcp_servers = execution_context.get("allowed_mcp_servers", [])
        allowed_mcp_tools = execution_context.get("allowed_mcp_tools", [])
        account_api_token = execution_context.get("account_api_token")

        if allowed_mcp_servers or allowed_mcp_tools:
            mcp_env = MCPConfigService.generate_mcp_environment_vars(
                allowed_mcp_servers, allowed_mcp_tools
            )
            env.update(mcp_env)

            if account_api_token:
                env["PRELOOP_API_TOKEN"] = account_api_token

            mcp_config = MCPConfigService.generate_mcp_config(
                allowed_mcp_servers,
                allowed_mcp_tools,
                account_api_token=account_api_token,
            )
            env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)

        # Convert env dict to list of V1EnvVar. Git credentials are merged in
        # here rather than baked into the agent script, so the token stays out
        # of the pod's command line (issue #173).
        env_vars = [
            client.V1EnvVar(name=k, value=v)
            for k, v in self._apply_git_credential_env(env, execution_context).items()
        ]

        # Get resource limits from config or use defaults
        memory_limit = os.getenv("AGENT_MEMORY_LIMIT", "2Gi")
        cpu_limit = os.getenv("AGENT_CPU_LIMIT", "1")
        memory_request = os.getenv("AGENT_MEMORY_REQUEST", "512Mi")
        cpu_request = os.getenv("AGENT_CPU_REQUEST", "250m")

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
                    f"Setting pod working directory to git repository: {working_dir}"
                )

        # Check if subclass provided custom command/args (e.g., CodexAgent)
        command = execution_context.get("_container_command")
        args = execution_context.get("_container_args")

        # Wrap `bash -c <script>` invocations with the artifact-emission
        # epilogue so result.json / the evidence pack become retrievable from
        # the pod's log stream after completion (a finished pod's filesystem
        # is unreachable through the API). The original script moves into an
        # env var and runs unchanged in a child shell.
        wrapped = self._wrap_kubernetes_args_for_artifacts(args)
        if wrapped is not None:
            args, inner_script = wrapped
            env_vars.append(
                client.V1EnvVar(name=K8S_INNER_SCRIPT_ENV, value=inner_script)
            )

        # Run as root by default — codex-universal installs runtimes (nvm, pyenv,
        # cargo, phpenv) under /root and hardcodes /root/.nvm/nvm.sh in /etc/profile.
        # Set AGENT_RUN_AS_NON_ROOT=true to use UID 1000 (for images that support it).
        run_as_non_root = os.getenv("AGENT_RUN_AS_NON_ROOT", "false").lower() == "true"
        agent_uid = 1000 if run_as_non_root else 0
        agent_gid = 1000 if run_as_non_root else 0
        home_dir = "/home/agent" if run_as_non_root else "/root"

        # Volume mounts: /workspace for git repos.
        # No init container needed — the container overlay FS makes the image's
        # filesystem writable per-pod, so pre-installed tools (nvm, node, etc.)
        # in /root are available instantly without copying.
        volumes = [
            client.V1Volume(
                name="workspace",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
        ]
        volume_mounts = [
            client.V1VolumeMount(
                name="workspace", mount_path="/workspace", sub_path=None
            ),
        ]

        if run_as_non_root:
            # Non-root needs a writable HOME via emptyDir (can't write to /root).
            volumes.append(
                client.V1Volume(
                    name="agent-home",
                    empty_dir=client.V1EmptyDirVolumeSource(),
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="agent-home", mount_path=home_dir, sub_path=None
                )
            )
            env_vars.append(client.V1EnvVar(name="HOME", value=home_dir))
        # When running as root, /root comes from the image overlay (writable,
        # with all pre-installed tools) — no emptyDir mount needed.

        # Container specification with hardened security context
        container = client.V1Container(
            name="agent",
            image=self.image,
            env=env_vars,
            command=command,  # Optional: set by subclasses like CodexAgent
            args=args,  # Optional: set by subclasses like CodexAgent
            working_dir=working_dir,  # Set working directory to git repo if configured
            resources=client.V1ResourceRequirements(
                limits={"memory": memory_limit, "cpu": cpu_limit},
                requests={"memory": memory_request, "cpu": cpu_request},
            ),
            security_context=client.V1SecurityContext(
                run_as_user=agent_uid,
                run_as_non_root=run_as_non_root,
                read_only_root_filesystem=False,
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(
                    drop=["ALL"],
                    # Root needs DAC_OVERRIDE to write to image files that lack
                    # the owner-write bit (e.g. /root/.nvm/ in codex-universal).
                    add=["DAC_OVERRIDE", "CHOWN", "FOWNER"]
                    if not run_as_non_root
                    else None,
                ),
            ),
            volume_mounts=volume_mounts,
        )

        # Pod template specification — no init containers for instant startup.
        # Each pod gets a fresh overlay filesystem from the image, so there is
        # no data leakage between executions.
        pod_template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={
                    "preloop.flow_id": flow_id,
                    "preloop.execution_id": execution_id,
                    "preloop.agent_type": self.agent_type,
                    "app": "agent-execution",
                }
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                containers=[container],
                security_context=client.V1PodSecurityContext(
                    run_as_user=agent_uid,
                    run_as_group=agent_gid,
                    fs_group=agent_gid,
                ),
                volumes=volumes,
            ),
        )

        # Job specification with TTL for auto-cleanup after completion
        # Set AGENT_JOB_TTL_SECONDS to a higher value (e.g., 86400 for 24 hours)
        # to keep completed/failed pods around for debugging with:
        #   kubectl exec -it <pod-name> -n <namespace> -- /bin/bash
        # Note: Pods are only accessible until TTL expires after completion
        ttl_seconds = int(os.getenv("AGENT_JOB_TTL_SECONDS", "3600"))
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.agent_namespace,
                labels={
                    "preloop.flow_id": flow_id,
                    "preloop.execution_id": execution_id,
                    "preloop.agent_type": self.agent_type,
                },
            ),
            spec=client.V1JobSpec(
                template=pod_template,
                backoff_limit=0,  # Don't retry failed jobs
                ttl_seconds_after_finished=ttl_seconds,  # Auto-cleanup after completion
            ),
        )

        try:
            # Create the Job
            await self._k8s_batch_api.create_namespaced_job(
                namespace=self.agent_namespace, body=job
            )

            self.logger.info(
                f"Started Kubernetes Job {job_name} in namespace {self.agent_namespace} "
                f"for execution {execution_id}"
            )
            return job_name

        except ApiException as e:
            self.logger.error(
                f"Failed to create Kubernetes Job for execution {execution_id}: {e}"
            )
            raise RuntimeError(f"Failed to start agent Job: {e}")

    async def get_status(self, session_reference: str) -> AgentStatus:
        """
        Get the status of a container.

        Args:
            session_reference: Container ID

        Returns:
            Agent status
        """
        if self.use_kubernetes:
            return await self._get_kubernetes_status(session_reference)

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)
            info = await container.show()

            state = info["State"]
            if state["Running"]:
                return AgentStatus.RUNNING
            elif state["Status"] == "created":
                return AgentStatus.STARTING
            elif state["Status"] == "exited":
                if state["ExitCode"] == 0:
                    return AgentStatus.SUCCEEDED
                else:
                    return AgentStatus.FAILED
            else:
                return AgentStatus.STOPPED

        except DockerError as e:
            self.logger.error(
                f"Failed to get status for container {session_reference}: {e}"
            )
            return AgentStatus.FAILED

    async def _get_kubernetes_status(self, job_name: str) -> AgentStatus:
        """
        Get status of a Kubernetes Job.

        Args:
            job_name: Name of the Job

        Returns:
            Agent status based on Job/Pod state
        """
        await self._init_kubernetes_clients()

        try:
            # Get Job status
            job = await self._k8s_batch_api.read_namespaced_job_status(
                name=job_name, namespace=self.agent_namespace
            )

            # Check Job conditions
            if job.status.active and job.status.active > 0:
                return AgentStatus.RUNNING

            if job.status.succeeded and job.status.succeeded > 0:
                return AgentStatus.SUCCEEDED

            if job.status.failed and job.status.failed > 0:
                return AgentStatus.FAILED

            # If no pods have started yet, it's starting
            if (
                not job.status.active
                and not job.status.succeeded
                and not job.status.failed
            ):
                return AgentStatus.STARTING

            return AgentStatus.RUNNING

        except ApiException as e:
            if e.status == 404:
                self.logger.warning(f"Job {job_name} not found")
                return AgentStatus.FAILED
            self.logger.error(f"Failed to get status for Job {job_name}: {e}")
            return AgentStatus.FAILED

    @staticmethod
    def _format_kubernetes_pod_wait_message(pod: Any) -> str:
        """Return a useful message for pods that exist but cannot stream logs yet."""
        pod_name = getattr(getattr(pod, "metadata", None), "name", "unknown")
        phase = getattr(getattr(pod, "status", None), "phase", None) or "unknown"

        status = getattr(pod, "status", None)
        for container_status in getattr(status, "container_statuses", None) or []:
            waiting = getattr(getattr(container_status, "state", None), "waiting", None)
            if waiting:
                reason = getattr(waiting, "reason", None) or "Waiting"
                message = getattr(waiting, "message", None)
                return f"[WARN] Kubernetes pod {pod_name} is {phase}: {reason}" + (
                    f" - {message}" if message else ""
                )

        for condition in getattr(status, "conditions", None) or []:
            if (
                getattr(condition, "type", None) == "PodScheduled"
                and getattr(condition, "status", None) == "False"
            ):
                reason = getattr(condition, "reason", None) or "Unschedulable"
                message = getattr(condition, "message", None)
                return f"[WARN] Kubernetes pod {pod_name} is {phase}: {reason}" + (
                    f" - {message}" if message else ""
                )

        return (
            f"[WARN] Kubernetes pod {pod_name} is {phase}; logs are not available yet"
        )

    async def get_result(self, session_reference: str) -> AgentExecutionResult:
        """
        Get the result of a container execution.

        Args:
            session_reference: Container ID or Job name

        Returns:
            Execution result
        """
        if self.use_kubernetes:
            return await self._get_kubernetes_result(session_reference)

        status = await self.get_status(session_reference)

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)
            info = await container.show()

            # Get exit code
            exit_code = info["State"].get("ExitCode")

            # Get logs
            logs = await self.get_logs(session_reference, tail=1000)
            output_summary = "\n".join(logs[-50:]) if logs else None

            # Check for error patterns in logs even if exit code is 0
            error_message = None
            logs_text = "\n".join(logs) if logs else ""
            has_error_pattern = self._detect_error_in_logs(logs_text)

            # Override status if we detect errors in logs
            if has_error_pattern and status == AgentStatus.SUCCEEDED:
                self.logger.warning(
                    f"Container {session_reference[:12]} exited with code 0 but logs contain critical errors. "
                    "Marking as FAILED."
                )
                status = AgentStatus.FAILED
            elif not has_error_pattern and status == AgentStatus.SUCCEEDED:
                # Log when we successfully ignore benign error patterns
                if "error" in logs_text.lower() or "no commits" in logs_text.lower():
                    self.logger.info(
                        f"Container {session_reference[:12]} exited with code 0. "
                        "Logs contain benign messages (e.g., 'no commits'), not marking as failed."
                    )

            failure_analysis = None
            if status == AgentStatus.FAILED:
                # Analyse the full logs once and keep the whole verdict:
                # only the message survives into FlowExecution.error_message,
                # so the transient/terminal classification must travel on the
                # result itself for the orchestrator's retry decision.
                failure_analysis = analyze_agent_failure(logs_text)
                error_message = (
                    info["State"].get("Error")
                    or failure_analysis.message
                    or f"Container exited with code {exit_code}"
                )

            return AgentExecutionResult(
                status=status,
                session_reference=session_reference,
                output_summary=output_summary,
                error_message=error_message,
                exit_code=exit_code,
                failure_analysis=failure_analysis,
            )

        except DockerError as e:
            self.logger.error(
                f"Failed to get result for container {session_reference}: {e}"
            )
            return AgentExecutionResult(
                status=AgentStatus.FAILED,
                session_reference=session_reference,
                error_message=str(e),
            )

    async def _get_kubernetes_result(self, job_name: str) -> AgentExecutionResult:
        """
        Get the result of a Kubernetes Job execution.

        Args:
            job_name: Name of the Job

        Returns:
            Execution result
        """
        status = await self.get_status(job_name)

        try:
            await self._init_kubernetes_clients()

            # Get logs from the shared bounded terminal read. A small tail
            # (the pre-wrapper tail=1000) is no longer safe: the server
            # applies tail_lines BEFORE we filter the artifact emission, and
            # a present evidence block can occupy tens of thousands of
            # trailing lines — evicting the success sentinel from any small
            # window. The shared read's bound is derived from the emission
            # byte caps, so after filtering the emission lines out we still
            # hold at least as much real agent output as before the wrapper.
            raw_lines = await self._get_kubernetes_terminal_logs(job_name)
            logs = [
                line
                for line in raw_lines
                if not line.strip().startswith(ARTIFACT_STREAM_LINE_PREFIX)
            ]
            output_summary = "\n".join(logs[-50:]) if logs else None

            # Check for error patterns in logs
            error_message = None
            logs_text = "\n".join(logs) if logs else ""
            has_error_pattern = self._detect_error_in_logs(logs_text)

            # Override status if we detect errors in logs
            if has_error_pattern and status == AgentStatus.SUCCEEDED:
                self.logger.warning(
                    f"Job {job_name} succeeded but logs contain critical errors. "
                    "Marking as FAILED."
                )
                status = AgentStatus.FAILED
            elif not has_error_pattern and status == AgentStatus.SUCCEEDED:
                # Log when we successfully ignore benign error patterns
                if "error" in logs_text.lower() or "no commits" in logs_text.lower():
                    self.logger.info(
                        f"Job {job_name} succeeded. "
                        "Logs contain benign messages (e.g., 'no commits'), not marking as failed."
                    )

            # Try to get exit code from pod
            exit_code = None
            try:
                label_selector = f"job-name={job_name}"
                pods = await self._k8s_core_api.list_namespaced_pod(
                    namespace=self.agent_namespace, label_selector=label_selector
                )
                if pods.items:
                    pod = pods.items[0]
                    if pod.status.container_statuses:
                        container_status = pod.status.container_statuses[0]
                        if container_status.state.terminated:
                            exit_code = container_status.state.terminated.exit_code
            except Exception as e:
                self.logger.warning(f"Could not get exit code for Job {job_name}: {e}")

            failure_analysis = None
            if status == AgentStatus.FAILED:
                # Same as the Docker path: keep the full-log verdict on the
                # result, not just the message.
                failure_analysis = analyze_agent_failure(logs_text)
                error_message = failure_analysis.message or (
                    f"Job exited with code {exit_code}"
                    if exit_code is not None
                    else "Job failed"
                )

            return AgentExecutionResult(
                status=status,
                session_reference=job_name,
                output_summary=output_summary,
                error_message=error_message,
                exit_code=exit_code,
                failure_analysis=failure_analysis,
            )

        except ApiException as e:
            self.logger.error(f"Failed to get result for Job {job_name}: {e}")
            return AgentExecutionResult(
                status=AgentStatus.FAILED,
                session_reference=job_name,
                error_message=str(e),
            )

    # Success sentinel that agents print when completing successfully.
    # Must match FLOW_SUCCESS_SENTINEL in flow_orchestrator.py.
    FLOW_SUCCESS_SENTINEL = "FLOW_EXECUTION_SUCCESS"

    # Marker printed by the agent script before the agent command runs.
    # Must match AGENT_EXEC_START_MARKER in flow_orchestrator.py.
    AGENT_EXEC_START_MARKER = "PRELOOP_AGENT_EXEC_START"

    async def get_result_artifact(
        self, session_reference: str
    ) -> Optional[Dict[str, Any]]:
        """Capture the structured result artifact written by the agent.

        Reads ``RESULT_ARTIFACT_PATH`` (``/workspace/result.json``) out of the
        container via the Docker archive API — no log scraping or sentinel
        parsing. Works for both running and exited containers (containers are
        started with ``AutoRemove: False``).

        Returns the parsed JSON object, a wrapped ``{"error": ...}`` object
        when the file exists but is unusable (invalid JSON, not an object,
        oversized) or the fetch failed in a visible way, or ``None`` when the
        agent wrote no artifact — the normal case for non-eval flows.

        On Kubernetes a completed pod's filesystem is not reachable via the
        API, so the agent script is wrapped to emit the artifact into the pod
        log stream between ``PRELOOP_ARTIFACT_*`` markers; this method parses
        it back out of the logs (see ``K8S_ARTIFACT_WRAPPER_SCRIPT``).
        """
        if self.use_kubernetes:
            return await self._get_kubernetes_result_artifact(session_reference)

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)
            tar = await container.get_archive(RESULT_ARTIFACT_PATH)
        except DockerError as e:
            if e.status == 404:
                # File (or container) not found: the agent did not write a
                # result artifact — the normal case for non-eval flows.
                return None
            # Any other daemon status (500, 429, ...) is an infra failure,
            # not "no artifact". Keep it visible: an eval run whose artifact
            # could not be fetched must not look identical to a run that
            # reported nothing.
            self.logger.warning(
                f"Failed to read result artifact from container "
                f"{session_reference[:12]}: {_exception_message(e)}"
            )
            return {
                "error": "result_artifact_fetch_failed",
                "detail": _exception_message(e)[:500],
                "docker_status": e.status,
            }
        except Exception as e:
            self.logger.warning(
                f"Failed to read result artifact from container "
                f"{session_reference[:12]}: {_exception_message(e)}"
            )
            return None

        try:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return None
            if member.size > MAX_RESULT_ARTIFACT_BYTES:
                self.logger.warning(
                    f"Result artifact from container {session_reference[:12]} "
                    f"is too large ({member.size} bytes), not persisting content"
                )
                return {
                    "error": "result_artifact_too_large",
                    "size_bytes": member.size,
                    "limit_bytes": MAX_RESULT_ARTIFACT_BYTES,
                }
            fileobj = tar.extractfile(member)
            if fileobj is None:
                return None
            raw = fileobj.read(MAX_RESULT_ARTIFACT_BYTES + 1)
        finally:
            tar.close()

        return self._interpret_result_artifact_bytes(raw, session_reference)

    def _interpret_result_artifact_bytes(
        self, raw: bytes, session_reference: str
    ) -> Dict[str, Any]:
        """Parse captured result.json bytes with the shared validation rules.

        Shared by the Docker archive path and the Kubernetes log-channel path
        so both surface identical error objects.
        """
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.logger.warning(
                f"Result artifact from {session_reference[:40]} is not valid JSON: {e}"
            )
            return {
                "error": "result_artifact_invalid_json",
                "detail": str(e)[:500],
            }
        if not isinstance(parsed, dict):
            return {
                "error": "result_artifact_not_object",
                "detail": f"expected a JSON object, got {type(parsed).__name__}",
            }
        return parsed

    @staticmethod
    def _wrap_kubernetes_args_for_artifacts(
        args: Any,
    ) -> Optional[tuple[list, str]]:
        """Return wrapped ``(args, inner_script)`` for ``["-c", script]`` args.

        Only the ``bash -c <script>`` shape used by the shell-scripted agents
        (codex, gemini, opencode) is wrapped; anything else is left untouched
        and artifact capture degrades to the pre-wrapper behaviour (None).
        """
        if (
            isinstance(args, list)
            and len(args) == 2
            and args[0] == "-c"
            and isinstance(args[1], str)
        ):
            return ["-c", K8S_ARTIFACT_WRAPPER_SCRIPT], args[1]
        return None

    @staticmethod
    def _extract_artifact_stream(
        lines: list[str], channel: str
    ) -> Optional[Dict[str, Any]]:
        """Extract one artifact channel from pod log lines.

        Returns ``None`` when no BEGIN marker for ``channel`` exists (wrapper
        not applied, or logs rotated away), otherwise a dict with:
        ``status``: present | absent | too_large | error | truncated | corrupt
        ``size``: declared byte size when the marker carried one
        ``data``: decoded payload bytes when status == "present"
        """
        begin_prefix = f"{ARTIFACT_STREAM_LINE_PREFIX}BEGIN {channel}"
        end_line = f"{ARTIFACT_STREAM_LINE_PREFIX}END {channel}"
        b64_prefix = f"{ARTIFACT_STREAM_LINE_PREFIX}B64 "

        begin_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip().startswith(begin_prefix):
                begin_idx = idx
                break
        if begin_idx is None:
            return None

        marker_parts = lines[begin_idx].strip().split()
        # ["PRELOOP_ARTIFACT_BEGIN", channel, status, size?]
        status = marker_parts[2] if len(marker_parts) > 2 else "error"
        size: Optional[int] = None
        if len(marker_parts) > 3:
            try:
                size = int(marker_parts[3])
            except ValueError:
                size = None

        chunks: list[str] = []
        terminated = False
        for line in lines[begin_idx + 1 :]:
            stripped = line.strip()
            if stripped == end_line:
                terminated = True
                break
            if stripped.startswith(b64_prefix):
                chunks.append(stripped[len(b64_prefix) :])
        if not terminated:
            return {"status": "truncated", "size": size, "data": None}
        if status != "present":
            return {"status": status, "size": size, "data": None}
        try:
            data = base64.b64decode("".join(chunks), validate=True)
        except (binascii.Error, ValueError):
            return {"status": "corrupt", "size": size, "data": None}
        return {"status": "present", "size": size, "data": data}

    async def _get_kubernetes_terminal_logs(self, job_name: str) -> list[str]:
        """Read the tail of a finished Job's pod log once and cache it.

        The terminal path has three log consumers — status summarisation and
        error-pattern scanning (``_get_kubernetes_result``), the ``result``
        artifact channel and the ``evidence`` channel. They all share this
        single bounded read instead of each re-downloading the log.

        ``K8S_TERMINAL_LOG_TAIL_LINES`` is sized so the trailing artifact
        emission is ALWAYS fully inside the window (its payload is byte-capped
        and it is the last thing the wrapper prints), with a generous window
        of real agent output to spare for the status scan. Returns raw lines
        (artifact streams included); callers filter what they don't need.
        """
        cached = self._k8s_terminal_log_cache.get(job_name)
        if cached is not None:
            return cached
        lines = await self._get_kubernetes_logs(
            job_name, tail=K8S_TERMINAL_LOG_TAIL_LINES, include_artifact_streams=True
        )
        if lines:
            # Don't cache empty reads: they can be transient (pod listing
            # hiccup) and each caller degrades gracefully on its own.
            self._k8s_terminal_log_cache[job_name] = lines
        return lines

    async def _get_kubernetes_result_artifact(
        self, job_name: str
    ) -> Optional[Dict[str, Any]]:
        """Capture result.json emitted into the pod log stream on Kubernetes.

        The agent script wrapper emits the artifact between structured marker
        lines right before the container exits (see
        ``K8S_ARTIFACT_WRAPPER_SCRIPT``); this parses the last ``result``
        channel block out of the shared terminal log read.
        """
        try:
            lines = await self._get_kubernetes_terminal_logs(job_name)
        except Exception as e:
            self.logger.warning(
                f"Failed to read logs for result artifact of Job {job_name}: "
                f"{_exception_message(e)}"
            )
            return None

        stream = self._extract_artifact_stream(lines, "result")
        if stream is None:
            # Wrapper not applied (custom runner image / legacy job) or the
            # emission was rotated out of the log — same visibility as before
            # this feature existed.
            self.logger.debug(
                f"No result artifact emission found in logs of Job {job_name}"
            )
            return None
        status = stream["status"]
        if status == "absent":
            return None
        if status == "too_large":
            self.logger.warning(
                f"Result artifact from Job {job_name} is too large "
                f"({stream['size']} bytes), not persisting content"
            )
            return {
                "error": "result_artifact_too_large",
                "size_bytes": stream["size"],
                "limit_bytes": MAX_RESULT_ARTIFACT_BYTES,
            }
        if status != "present":
            # truncated / corrupt / error: an eval run whose artifact could
            # not be recovered must not look identical to one that reported
            # nothing.
            self.logger.warning(
                f"Result artifact emission from Job {job_name} is unusable "
                f"(status={status})"
            )
            return {
                "error": "result_artifact_fetch_failed",
                "detail": f"log emission {status}",
            }
        return self._interpret_result_artifact_bytes(stream["data"], job_name)

    async def get_evidence_archive(self, session_reference: str) -> Optional[bytes]:
        """Capture the evidence pack (``/workspace/evidence``) as tar.gz bytes.

        Docker: fetches the directory through the archive API and re-packs it
        as tar.gz. Kubernetes: decodes the base64 emission from the pod log
        stream (see ``K8S_ARTIFACT_WRAPPER_SCRIPT``). Best-effort: returns
        ``None`` when there is no evidence directory, when it exceeds
        ``MAX_EVIDENCE_ARCHIVE_BYTES``, or on fetch errors (all logged).
        """
        if self.use_kubernetes:
            return await self._get_kubernetes_evidence_archive(session_reference)
        return await self._get_docker_evidence_archive(session_reference)

    async def _get_kubernetes_evidence_archive(self, job_name: str) -> Optional[bytes]:
        try:
            lines = await self._get_kubernetes_terminal_logs(job_name)
        except Exception as e:
            self.logger.warning(
                f"Failed to read logs for evidence archive of Job {job_name}: "
                f"{_exception_message(e)}"
            )
            return None
        stream = self._extract_artifact_stream(lines, "evidence")
        if stream is None or stream["status"] == "absent":
            return None
        if stream["status"] != "present":
            self.logger.warning(
                f"Evidence archive from Job {job_name} not captured "
                f"(status={stream['status']}, size={stream['size']})"
            )
            return None
        return bytes(stream["data"])

    async def _get_docker_evidence_archive(
        self, session_reference: str
    ) -> Optional[bytes]:
        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)
            tar = await container.get_archive(EVIDENCE_DIR_PATH)
        except DockerError as e:
            if e.status != 404:
                self.logger.warning(
                    f"Failed to read evidence archive from container "
                    f"{session_reference[:12]}: {_exception_message(e)}"
                )
            return None
        except Exception as e:
            self.logger.warning(
                f"Failed to read evidence archive from container "
                f"{session_reference[:12]}: {_exception_message(e)}"
            )
            return None

        try:
            total_size = sum(m.size for m in tar.getmembers() if m.isfile())
            if total_size > MAX_EVIDENCE_ARCHIVE_BYTES:
                self.logger.warning(
                    f"Evidence pack from container {session_reference[:12]} "
                    f"is too large uncompressed ({total_size} bytes), "
                    "not capturing"
                )
                return None
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as out:
                for member in tar.getmembers():
                    if member.isfile():
                        fileobj = tar.extractfile(member)
                        if fileobj is not None:
                            out.addfile(member, fileobj)
                    elif member.isdir():
                        out.addfile(member)
        finally:
            tar.close()

        data = buffer.getvalue()
        if len(data) > MAX_EVIDENCE_ARCHIVE_BYTES:
            self.logger.warning(
                f"Evidence archive from container {session_reference[:12]} "
                f"is too large ({len(data)} bytes), not capturing"
            )
            return None
        return data

    def _detect_error_in_logs(self, logs_text: str) -> bool:
        """
        Detect if logs contain critical error patterns that indicate failure.

        This is a safety net for cases where the container exits with code 0
        but logs contain system-level errors (e.g. API auth failures, unhandled
        exceptions).  It does NOT determine success — that is solely based on
        the container exit code.

        The success sentinel is checked only in the portion of logs AFTER
        the AGENT_EXEC_START_MARKER to avoid false positives from prompt echo.

        Args:
            logs_text: Full log text

        Returns:
            True if critical error patterns detected, False otherwise
        """
        # Extract only the agent output (after the exec start marker)
        # to avoid false positives from prompt echo in init commands.
        agent_output = logs_text
        marker_idx = logs_text.find(self.AGENT_EXEC_START_MARKER)
        if marker_idx >= 0:
            agent_output = logs_text[marker_idx + len(self.AGENT_EXEC_START_MARKER) :]
            self.logger.info(
                f"[Sentinel] Exec start marker found at char {marker_idx}, "
                f"checking sentinel in {len(agent_output)} chars of agent output"
            )
        else:
            self.logger.warning(
                "[Sentinel] Exec start marker NOT found in logs — "
                "sentinel check will scan full log output"
            )

        # If the agent printed the success sentinel on its own line
        # (post-exec-marker), trust that it succeeded.
        sentinel_in_agent_output = any(
            line.strip() == self.FLOW_SUCCESS_SENTINEL
            for line in agent_output.splitlines()
        )
        # Also check if sentinel appears in the pre-marker section (prompt echo)
        sentinel_in_prompt = False
        if marker_idx >= 0:
            sentinel_in_prompt = any(
                line.strip() == self.FLOW_SUCCESS_SENTINEL
                for line in logs_text[:marker_idx].splitlines()
            )
            if sentinel_in_prompt:
                self.logger.info(
                    "[Sentinel] Sentinel also found in pre-marker output (prompt echo) — ignored"
                )

        if sentinel_in_agent_output:
            self.logger.info(
                "[Sentinel] Success sentinel found in agent output — "
                "treating as successful execution"
            )
            return False

        logs_lower = logs_text.lower()

        # Critical error patterns that always indicate failure
        # These are system-level errors, not user code output
        critical_error_patterns = [
            "litellm.badrequesterror",
            "litellm.authenticationerror",
            "litellm.ratelimiterror",
            "openaiexception",
            "anthropicexception",
            "traceback (most recent call last)",
            "fatal error",
            "critical:",
            "agent execution failed",
            "unhandled exception",
        ]

        for pattern in critical_error_patterns:
            if pattern in logs_lower:
                self.logger.info(
                    f"Critical error pattern '{pattern}' found in logs - "
                    "treating as failed execution"
                )
                return True

        # Heuristic: multiple "ERROR:" lines without benign context

        # Benign patterns - these are informational messages that might contain
        # "error" but don't indicate actual failure
        benign_patterns = [
            "no commits",
            "skipping push",
            "nothing to commit",
            "no changes",
            "up to date",
            "up-to-date",
            "already up to date",
            "everything up-to-date",
            "failed to create pr (may already exist)",
            "failed to create mr (may already exist)",
        ]

        # Check for "ERROR:" but filter out benign cases
        if "error:" in logs_lower:
            # Count occurrences to filter out single informational errors
            error_count = logs_lower.count("error:")

            # Check if any benign pattern is present in the logs
            # If a benign pattern exists, we're more lenient with error count threshold
            contains_benign_pattern = any(
                pattern in logs_lower for pattern in benign_patterns
            )

            # Multiple errors without any benign patterns suggest real failure
            if error_count >= 3 and not contains_benign_pattern:
                self.logger.info(
                    f"Heuristic detection: {error_count} 'error:' occurrences found "
                    "without benign patterns - treating as failed execution"
                )
                return True

        return False

    def _extract_error_from_logs(self, logs_text: str) -> str:
        """
        Extract a human-actionable error message from logs.

        Delegates to :func:`analyze_agent_failure`, which looks for the
        *meaningful* failure signal (an upstream provider status and the
        agent's own exhausted retry loop) anywhere in the log, rather than
        returning whatever the last error-shaped line happened to be. The tail
        of a failed run is usually a stack trace or a stringified error object
        (``[object Object]``), which names no cause.

        Message-only view: ``get_result`` calls :func:`analyze_agent_failure`
        directly so the full classification (``transient`` verdict, evidence)
        can travel on the ``AgentExecutionResult``.

        Args:
            logs_text: Full log text

        Returns:
            Extracted error message or empty string
        """
        return analyze_agent_failure(logs_text).message

    async def stop(self, session_reference: str) -> None:
        """
        Stop a running container.

        Args:
            session_reference: Container ID
        """
        if self.use_kubernetes:
            await self._stop_kubernetes_pod(session_reference)
            return

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)

            self.logger.info(f"Stopping container {session_reference[:12]}")
            await container.stop(t=30)  # 30 second grace period

            # Remove from tracking
            if session_reference in self._containers:
                del self._containers[session_reference]

        except DockerError as e:
            self.logger.error(f"Failed to stop container {session_reference}: {e}")
            raise

    async def _stop_kubernetes_pod(self, job_name: str) -> None:
        """
        Stop a Kubernetes Job by deleting it.

        Args:
            job_name: Name of the Job to delete
        """
        await self._init_kubernetes_clients()

        try:
            self.logger.info(f"Deleting Kubernetes Job {job_name}")

            # Delete the Job (this will also delete associated Pods)
            await self._k8s_batch_api.delete_namespaced_job(
                name=job_name,
                namespace=self.agent_namespace,
                propagation_policy="Foreground",  # Delete pods before deleting the job
            )

            self.logger.info(f"Successfully deleted Job {job_name}")

        except ApiException as e:
            if e.status == 404:
                self.logger.warning(f"Job {job_name} not found, already deleted")
            else:
                self.logger.error(f"Failed to delete Job {job_name}: {e}")
                raise

    async def get_logs(
        self, session_reference: str, tail: int | None = None
    ) -> list[str]:
        """
        Get logs from a container (batch mode).

        Output is scrubbed of known credential formats before it is returned,
        because every consumer of this method either persists the lines or
        shows them to a user (issue #173).

        Args:
            session_reference: Container ID or Job name
            tail: Number of recent log lines, or None for all logs

        Returns:
            List of log lines, with secrets redacted
        """
        if self.use_kubernetes:
            return scrub_secret_lines(
                await self._get_kubernetes_logs(session_reference, tail)
            )

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(session_reference)

            log_kwargs: dict = {"stdout": True, "stderr": True}
            if tail is not None:
                log_kwargs["tail"] = tail
            logs = await container.log(**log_kwargs)
            # Handle both bytes and str (aiodocker API can return either)
            decoded_logs = []
            for line in logs:
                if isinstance(line, bytes):
                    decoded_logs.append(line.decode("utf-8", errors="replace"))
                else:
                    decoded_logs.append(line)
            return scrub_secret_lines(decoded_logs)

        except DockerError as e:
            self.logger.error(
                f"Failed to get logs for container {session_reference}: {e}"
            )
            return []

    async def stream_logs(self, session_reference: str):
        """
        Stream logs from a container in real-time.

        Lines are scrubbed of known credential formats before they are yielded,
        so neither the persisted execution log nor the live console feed can
        carry a token (issue #173).

        Args:
            session_reference: Container ID or Job name

        Yields:
            Log lines as they are produced, with secrets redacted
        """
        if self.use_kubernetes:
            async for line in self._stream_kubernetes_logs(session_reference):
                yield scrub_secrets(line)
        else:
            async for line in self._stream_docker_logs(session_reference):
                yield scrub_secrets(line)

    async def _stream_docker_logs(self, container_id: str):
        """
        Stream logs from a Docker container.

        Args:
            container_id: Container ID

        Yields:
            Log lines in real-time
        """
        self.logger.info(
            f"Starting Docker log stream for container {container_id[:12]}"
        )
        line_count = 0

        try:
            docker = await self._get_docker_client()
            container = await docker.containers.get(container_id)

            self.logger.info(
                f"Got container object, starting log follow for {container_id[:12]}"
            )

            # Stream logs with follow=True
            async for line in container.log(
                stdout=True, stderr=True, follow=True, stream=True
            ):
                line_count += 1
                # Handle both bytes and str (aiodocker API can return either)
                if isinstance(line, bytes):
                    decoded_line = line.decode("utf-8", errors="replace").rstrip()
                else:
                    decoded_line = line.rstrip()

                if decoded_line:  # Skip empty lines
                    if line_count <= 5:  # Log first 5 lines for debugging
                        self.logger.debug(
                            f"Docker log line #{line_count}: {decoded_line[:100]}"
                        )
                    yield decoded_line

            self.logger.info(
                f"Docker log stream ended for {container_id[:12]}, total lines: {line_count}"
            )

        except DockerError as e:
            self.logger.error(
                f"Error streaming logs from container {container_id}: {e}"
            )
            yield f"[ERROR] Failed to stream logs: {e}"
        except Exception as e:
            error_message = _exception_message(e)
            self.logger.error(
                f"Unexpected error streaming Docker logs for {container_id}: {error_message}",
                exc_info=True,
            )
            yield f"[ERROR] Unexpected error: {error_message}"

    async def _get_kubernetes_logs(
        self,
        job_name: str,
        tail: int | None = None,
        include_artifact_streams: bool = False,
    ) -> list[str]:
        """
        Get logs from the Pod associated with a Kubernetes Job.

        Args:
            job_name: Name of the Job
            tail: Number of recent log lines, or None for all logs
            include_artifact_streams: Keep the ``PRELOOP_ARTIFACT_*`` emission
                lines (base64 result/evidence blocks). Off by default so
                operator-facing logs and summaries stay readable; only the
                artifact-capture paths turn this on.

        Returns:
            List of log lines
        """
        await self._init_kubernetes_clients()

        try:
            # List pods for this Job
            label_selector = f"job-name={job_name}"
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=self.agent_namespace, label_selector=label_selector
            )

            if not pods.items:
                self.logger.warning(f"No pods found for Job {job_name}")
                return []

            # Get logs from the first pod (Jobs typically have one pod)
            pod_name = pods.items[0].metadata.name
            pod = pods.items[0]
            if getattr(pod.status, "phase", None) == "Pending":
                return [self._format_kubernetes_pod_wait_message(pod)]

            log_kwargs: dict = {
                "name": pod_name,
                "namespace": self.agent_namespace,
                "_preload_content": False,  # Get raw response
            }
            if tail is not None:
                log_kwargs["tail_lines"] = tail

            logs = await self._k8s_core_api.read_namespaced_pod_log(**log_kwargs)

            # Read and decode the logs
            log_data = await logs.read()
            log_text = log_data.decode("utf-8", errors="replace")

            # Split into lines
            lines = log_text.strip().split("\n") if log_text.strip() else []
            if not include_artifact_streams:
                lines = [
                    line
                    for line in lines
                    if not line.strip().startswith(ARTIFACT_STREAM_LINE_PREFIX)
                ]
            return lines

        except ApiException as e:
            if e.status == 404:
                self.logger.warning(f"Job or Pod for {job_name} not found")
                return []
            self.logger.error(f"Failed to get logs for Job {job_name}: {e}")
            return []

    async def _stream_kubernetes_logs(self, job_name: str):
        """
        Stream logs from a Kubernetes Job's Pod in real-time.

        Args:
            job_name: Name of the Job

        Yields:
            Log lines as they are produced
        """
        await self._init_kubernetes_clients()

        try:
            # Wait for pod to be created (may take time after Job creation + init container)
            label_selector = f"job-name={job_name}"
            pod_name = None

            # Retry for up to 60 seconds to find the pod
            import asyncio

            for attempt in range(60):
                pods = await self._k8s_core_api.list_namespaced_pod(
                    namespace=self.agent_namespace, label_selector=label_selector
                )

                if pods.items:
                    pod_name = pods.items[0].metadata.name
                    self.logger.info(f"Found pod {pod_name} for Job {job_name}")
                    break

                if attempt < 59:
                    await asyncio.sleep(1)

            if not pod_name:
                self.logger.warning(
                    f"No pods found for Job {job_name} after 60 seconds"
                )
                yield f"[WARN] No pods found for Job {job_name}"
                return

            # Wait for main container to start (after init container completes)
            # Poll pod status until the main container is running or terminated
            pod = None
            container_ready = False
            for attempt in range(60):
                pod = await self._k8s_core_api.read_namespaced_pod(
                    name=pod_name, namespace=self.agent_namespace
                )

                # Check if pod has container statuses
                if pod.status.container_statuses:
                    container_status = pod.status.container_statuses[0]
                    # Container is running or terminated - logs are available
                    if (
                        container_status.state.running
                        or container_status.state.terminated
                    ):
                        self.logger.info(f"Main container ready for {pod_name}")
                        container_ready = True
                        break

                if attempt < 59:
                    await asyncio.sleep(1)

            if not container_ready:
                if pod is not None:
                    yield self._format_kubernetes_pod_wait_message(pod)
                else:
                    yield f"[WARN] Kubernetes pod {pod_name} is not ready for log streaming"
                return

            # Stream logs with follow=True
            response = await self._k8s_core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.agent_namespace,
                container="agent",  # Specify the main container (not init container)
                follow=True,
                _preload_content=False,  # Required for streaming
            )

            # Read lines from the stream
            async for line in response.content:
                decoded_line = line.decode("utf-8", errors="replace").rstrip()
                if decoded_line and not decoded_line.startswith(
                    ARTIFACT_STREAM_LINE_PREFIX
                ):
                    # Skip empty lines and the artifact emission block (base64
                    # result/evidence payload) — noise for live viewers; the
                    # capture path reads it from the pod log afterwards.
                    yield decoded_line

        except ApiException as e:
            if e.status == 404:
                self.logger.warning(f"Job or Pod for {job_name} not found")
                yield "[WARN] Job or Pod not found"
            else:
                self.logger.error(f"Error streaming logs for Job {job_name}: {e}")
                yield f"[ERROR] Failed to stream logs: {e}"
        except Exception as e:
            error_message = _exception_message(e)
            self.logger.error(
                f"Unexpected error streaming Kubernetes logs for {job_name}: {error_message}",
                exc_info=True,
            )
            yield f"[ERROR] Unexpected error: {error_message}"

    # Keys under which resolved git secrets are stashed on the execution
    # context, to be turned into container environment variables. Private to
    # this class; nothing outside the agent layer should read them.
    GIT_CREDENTIALS_CONTEXT_KEY = "_git_credentials"
    GIT_API_TOKENS_CONTEXT_KEY = "_git_api_tokens"

    def _register_git_credentials(
        self,
        execution_context: Dict[str, Any],
        credentials: Dict[int, GitCredential],
    ) -> None:
        """Stash resolved git-transport credentials for conversion to env vars."""

        if credentials:
            execution_context[self.GIT_CREDENTIALS_CONTEXT_KEY] = credentials

    def _register_git_api_token(
        self, execution_context: Dict[str, Any], repo_index: int, token: str
    ) -> str:
        """Stash a REST API token for one repository and return its env var name.

        The post-execution PR/MR calls talk to the GitHub/GitLab REST API, not
        to git, so they cannot use the credential helper. They read the token
        from this variable instead of having it baked into the shell script.
        """

        tokens = execution_context.setdefault(self.GIT_API_TOKENS_CONTEXT_KEY, {})
        tokens[repo_index] = token
        return git_token_env_var(repo_index)

    def _git_credential_env(self, execution_context: Dict[str, Any]) -> Dict[str, str]:
        """Return env vars carrying git secrets for this execution.

        Called by every container start path after the init and post-execution
        commands have been built, since that is when secrets are resolved.
        Returns an empty dict when the flow clones nothing or has no token.
        """

        credentials: Dict[int, GitCredential] = (
            execution_context.get(self.GIT_CREDENTIALS_CONTEXT_KEY) or {}
        )
        env = dict(
            build_credential_env(credentials[index] for index in sorted(credentials))
        )

        api_tokens: Dict[int, str] = (
            execution_context.get(self.GIT_API_TOKENS_CONTEXT_KEY) or {}
        )
        for repo_index, token in api_tokens.items():
            env[git_token_env_var(repo_index)] = token

        return env

    def _apply_git_credential_env(
        self, env: Dict[str, str], execution_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """Merge git credential env vars into an agent's environment."""

        env.update(self._git_credential_env(execution_context))
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
                self.logger.info(
                    f"Attempting git clone with {len(repositories)} repositories "
                    f"(trigger fallback: {not repositories and bool(trigger_project_id)})"
                )
                git_cmd = self._prepare_git_clone_command(execution_context)
                if git_cmd:
                    commands.append(git_cmd)
                    self.logger.info(
                        "Git clone commands added (length=%d)", len(git_cmd)
                    )
                else:
                    self.logger.warning(
                        "Git clone was configured but no commands were generated. "
                        f"Check trigger_project_id={trigger_project_id} and credentials."
                    )
            else:
                self.logger.info(
                    f"Git clone skipped: enabled={is_enabled}, "
                    f"repositories={len(repositories)}, "
                    f"trigger_project_id={trigger_project_id}"
                )
        else:
            self.logger.debug("No git_clone_config in execution context")

        # Seed /workspace files declared on the trigger payload. After git
        # clone (whose pre-clone backup would sweep earlier writes away) and
        # before custom commands (which may consume the seeded files).
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

    def _prepare_workspace_seed_commands(
        self, execution_context: Dict[str, Any]
    ) -> str:
        """Build shell commands writing trigger-payload ``workspace_files``.

        The orchestrator has already validated the declaration (and failed
        the execution otherwise); re-parsing here is a defense-in-depth guard
        that raises rather than materializing an unvalidated path.
        """
        trigger_data = execution_context.get("trigger_event_data") or {}
        payload = (
            trigger_data.get("payload") if isinstance(trigger_data, dict) else None
        )
        seeds = parse_workspace_files(payload)
        if not seeds:
            return ""
        self.logger.info(
            "Seeding %d workspace file(s) from trigger payload: %s",
            len(seeds),
            [seed.path for seed in seeds],
        )
        return build_workspace_seed_shell(seeds)

    def _resolve_git_clone_repositories(
        self, execution_context: Dict[str, Any], git_config: Dict[str, Any]
    ) -> list[Dict[str, Any]]:
        """Resolve repository entries from config or trigger project fallback."""

        repositories = git_config.get("repositories", [])
        if repositories:
            return repositories

        trigger_project_id = execution_context.get("trigger_project_id")
        if trigger_project_id:
            self.logger.info(
                f"No repositories configured, using trigger project: {trigger_project_id}"
            )
            return [
                {
                    "project_id": trigger_project_id,
                    "clone_path": "/workspace",
                }
            ]

        self.logger.warning(
            "No repositories configured and no trigger project available for git clone"
        )
        return []

    def _resolve_git_branch_plan(
        self, execution_context: Dict[str, Any], git_config: Dict[str, Any]
    ) -> tuple[str, str, Optional[str], str, str]:
        """Resolve source/target branches, commit SHA, and git identity settings."""

        git_user_name = git_config.get("git_user_name", "Preloop")
        git_user_email = git_config.get("git_user_email", "git@preloop.ai")
        source_branch = git_config.get("source_branch") or None
        target_branch = git_config.get("target_branch") or None
        trigger_data = execution_context.get("trigger_event_data", {})

        if not source_branch:
            source_branch = self._extract_source_branch_from_trigger(trigger_data)
        if not source_branch:
            source_branch = "main"

        if not target_branch:
            flow_name = execution_context.get("flow_name", "flow")
            execution_id = execution_context.get("execution_id", "exec")
            safe_flow_name = flow_name.lower().replace(" ", "-")[:30]
            target_branch = f"preloop/{safe_flow_name}-{execution_id[:8]}"

        commit_sha = self._extract_commit_sha_from_trigger(trigger_data)
        if commit_sha:
            self.logger.info(
                f"Extracted commit SHA from trigger event: {commit_sha[:8]}"
            )

        return source_branch, target_branch, commit_sha, git_user_name, git_user_email

    def _build_git_global_setup_commands(
        self, git_user_name: str, git_user_email: str
    ) -> list[str]:
        """Build one-time git identity and workspace setup commands."""

        return [
            "mkdir -p /workspace",
            f"git config --global user.name {shlex.quote(git_user_name)}",
            f"git config --global user.email {shlex.quote(git_user_email)}",
        ]

    def _resolve_repository_clone_url(
        self,
        repo_config: Dict[str, Any],
        repo_index: int,
        execution_context: Dict[str, Any],
        trigger_data: Dict[str, Any],
    ) -> Optional[str]:
        """Resolve a clone URL from repo config, project metadata, or trigger data."""

        repo_url = repo_config.get("repository_url")
        if repo_url:
            return repo_url

        project_id = repo_config.get("project_id")
        if not project_id:
            project_id = execution_context.get("trigger_project_id")
            if project_id:
                self.logger.info(
                    f"Using trigger project {project_id} for repository #{repo_index + 1}"
                )

        if project_id:
            repo_url = self._get_repo_url_from_project(
                project_id, execution_context.get("account_id")
            )
            if repo_url:
                self.logger.info(f"Resolved repository URL from project {project_id}")
            else:
                self.logger.warning(
                    f"Could not construct repository URL from project {project_id}"
                )

        if not repo_url:
            repo_url = self._extract_repo_url_from_trigger(trigger_data)
            if repo_url:
                self.logger.info("Extracted repository URL from trigger event data")

        return repo_url

    def _resolve_repository_token(
        self,
        repo_config: Dict[str, Any],
        execution_context: Dict[str, Any],
    ) -> tuple[Optional[str], Optional[str]]:
        """Return ``(token, tracker_type)`` for one repository entry."""

        tracker_id = repo_config.get("tracker_id")
        git_credentials_map = execution_context.get("git_credentials_map", {})

        if tracker_id and tracker_id in git_credentials_map:
            tracker_creds = git_credentials_map.get(tracker_id, {})
            return tracker_creds.get("token"), tracker_creds.get("tracker_type")

        trigger_project_id = execution_context.get("trigger_project_id")
        if trigger_project_id:
            return self._get_token_from_project(
                trigger_project_id, execution_context.get("account_id")
            )

        return None, None

    def _build_git_credential(
        self,
        repo_url: str,
        repo_config: Dict[str, Any],
        execution_context: Dict[str, Any],
    ) -> Optional[GitCredential]:
        """Resolve the credential for a repository without touching its URL.

        The returned credential is written to a git credential store inside the
        container. The clone URL itself stays credential-free, so ``git remote
        -v`` in the workspace cannot leak the token (issue #173).
        """

        safe_url = strip_url_credentials(repo_url)

        token, tracker_type = self._resolve_repository_token(
            repo_config, execution_context
        )
        if not token:
            self.logger.warning(
                "No token available for %s. "
                "Clone may fail if the repository is private.",
                repo_url_log_location(safe_url),
            )
            return None

        host_kind = tracker_host_kind(safe_url)
        if host_kind is None and tracker_type not in {"github", "gitlab"}:
            # Still authenticate: an unrecognized host is usually a self-hosted
            # instance, and refusing here would break clones that work today.
            # Only the username convention is uncertain, not the token itself.
            self.logger.warning(
                "Could not determine tracker type for %s (tracker_type=%s); "
                "using the generic credential username",
                repo_url_log_location(safe_url),
                tracker_type,
            )

        username = credential_username(host_kind, tracker_type)
        self.logger.info(
            "Prepared git credential for %s (user=%s, token not in URL)",
            repo_url_log_location(safe_url),
            username,
        )
        return GitCredential(repo_url=safe_url, username=username, token=token)

    def _resolve_repository_clone_path(
        self, repo_config: Dict[str, Any], repo_index: int
    ) -> str:
        """Resolve absolute clone path for a repository entry."""

        clone_path = repo_config.get("clone_path", f"/workspace-{repo_index + 1}")
        if clone_path.startswith("/"):
            return clone_path
        return f"/workspace/{clone_path}"

    def _resolve_repository_clone_branch(
        self,
        repo_config: Dict[str, Any],
        *,
        commit_sha: Optional[str],
        source_branch: str,
        trigger_data: Dict[str, Any],
    ) -> str:
        """Choose the branch passed to ``git clone -b`` for one repository."""

        repo_branch = repo_config.get("branch")
        if repo_branch:
            return repo_branch
        if commit_sha:
            clone_branch = (
                self._extract_target_branch_from_trigger(trigger_data) or "main"
            )
            self.logger.info(
                "Commit SHA %s available; cloning branch '%s' "
                "instead of source branch '%s'",
                commit_sha[:8],
                clone_branch,
                source_branch,
            )
            return clone_branch
        return source_branch

    def _build_git_pre_clone_shell(self, full_path: str) -> str:
        """Build shell that prepares the clone target directory."""

        return f"""
echo "Preparing clone directory: {full_path}"
if [ -d "{full_path}" ]; then
    if [ -d "{full_path}/.git" ]; then
        echo "WARNING: {full_path} already contains a git repository, will reset it"
        rm -rf "{full_path}"
    elif [ "$(ls -A {full_path} 2>/dev/null)" ]; then
        echo "WARNING: {full_path} is not empty, cleaning up non-essential files..."
        # Move any existing files to a backup location, preserving only reports if they exist
        mkdir -p /tmp/workspace-backup
        mv {full_path}/* /tmp/workspace-backup/ 2>/dev/null || true
        mv {full_path}/.[!.]* /tmp/workspace-backup/ 2>/dev/null || true
        echo "Backed up existing files to /tmp/workspace-backup"
    fi
fi
""".strip()

    def _build_git_clone_shell(
        self, repo_url: str, full_path: str, clone_branch: str
    ) -> str:
        """Build the guarded ``git clone`` shell command."""

        branch_arg = f" -b {shlex.quote(clone_branch)}" if clone_branch else ""
        return f"""
echo "Cloning repository to {full_path}..."
if ! git clone{branch_arg} {shlex.quote(repo_url)} {shlex.quote(full_path)}; then
    echo "========================================="
    echo "FATAL ERROR: Git clone failed!"
    echo "Could not clone repository to {full_path}"
    echo "Check repository URL, credentials, and network connectivity."
    echo "========================================="
    exit 1
fi
""".strip()

    def _build_git_branch_setup_shell(
        self,
        *,
        full_path: str,
        commit_sha: Optional[str],
        source_branch: str,
        target_branch: str,
        trigger_data: Dict[str, Any],
    ) -> str:
        """Build post-clone branch and commit checkout commands."""

        q_path = shlex.quote(full_path)
        q_source = shlex.quote(source_branch)
        q_target = shlex.quote(target_branch)

        if commit_sha:
            q_commit = shlex.quote(commit_sha)
            q_commit_short = shlex.quote(commit_sha[:8])
            mr_fetch_ref = self._extract_merge_request_ref_from_trigger(trigger_data)
            mr_fetch_line = ""
            if mr_fetch_ref:
                q_mr_ref = shlex.quote(mr_fetch_ref)
                mr_fetch_line = (
                    f"echo Fetching merge request ref {q_mr_ref}...\n"
                    f"git fetch origin {q_mr_ref}:preloop-mr-head "
                    f"2>/dev/null || true"
                )
            return f"""
cd {q_path}
echo "========================================="
echo Checking out specific commit: {q_commit}
echo "========================================="
if ! git checkout {q_commit} 2>/dev/null; then
    echo "Direct checkout failed, fetching commit..."
    git fetch origin {q_commit} 2>/dev/null || true
fi
if ! git checkout {q_commit} 2>/dev/null; then
    echo "Commit fetch failed, trying source branch {q_source}..."
    git fetch origin {q_source}:preloop-source-head 2>/dev/null || true
fi
if ! git checkout {q_commit} 2>/dev/null; then
{mr_fetch_line}
    if ! git checkout {q_commit} 2>/dev/null; then
        echo "========================================="
        echo "FATAL ERROR: Could not checkout commit {q_commit_short}"
        echo "Tried direct checkout, commit fetch, source branch, and MR ref."
        echo "--- diagnostics ---"
        # Every attempt above hides stderr so the fallback chain stays quiet on
        # the happy path. Once we have actually failed, re-run the fetch and
        # checkout WITH stderr so the real cause (auth failure, unknown ref,
        # commit force-pushed away) reaches the execution log instead of a
        # generic "could not checkout".
        echo "$ git fetch origin {q_commit}"
        git fetch origin {q_commit} 2>&1 | tail -n 5 || true
        echo "$ git checkout {q_commit}"
        git checkout {q_commit} 2>&1 | tail -n 5 || true
        echo "$ git remote -v"
        git remote -v 2>&1 | sed -n '1,2p' || true
        echo "available refs:"
        git for-each-ref --format='%(refname)' --count=20 2>&1 || true
        echo "========================================="
        exit 1
    fi
fi
echo Creating agent target branch {q_target} from commit {q_commit_short}
if ! git checkout -b {q_target}; then
    echo "========================================="
    echo "FATAL ERROR: Could not create target branch {q_target}"
    echo "========================================="
    exit 1
fi
cd /workspace
""".strip()

        return f"""
cd {q_path}
echo Setting up branches: source={q_source}, target={q_target}
# Checkout source branch (create if it doesn't exist remotely)
if ! git checkout {q_source} 2>/dev/null; then
    echo Source branch {q_source} not found, creating from current HEAD
    git checkout -b {q_source}
fi
# Create and checkout target branch for commits
if ! git checkout -b {q_target}; then
    echo "========================================="
    echo "FATAL ERROR: Could not create target branch {q_target}"
    echo "========================================="
    exit 1
fi
cd /workspace
""".strip()

    def _build_git_clone_validation_shell(
        self,
        *,
        full_path: str,
        source_branch: str,
        target_branch: str,
        commit_sha: Optional[str],
    ) -> str:
        """Build shell that verifies the clone succeeded."""

        q_path = shlex.quote(full_path)
        q_git_dir = shlex.quote(f"{full_path}/.git")
        q_source = shlex.quote(source_branch)
        q_target = shlex.quote(target_branch)
        sha_display = (
            f'\necho "  Commit: {shlex.quote(commit_sha)}"' if commit_sha else ""
        )
        return f"""
if [ ! -d {q_path} ] || [ ! -d {q_git_dir} ]; then
    echo "========================================="
    echo "FATAL ERROR: Git clone validation failed!"
    echo "Repository directory {q_path} does not exist or is not a git repository."
    echo "Flow execution cannot continue without repository access."
    echo "========================================="
    exit 1
fi
echo "========================================="
echo "✓ Repository successfully cloned to {q_path}"
echo "  Branch: {q_target} (from {q_source})"{sha_display}
echo "========================================="
""".strip()

    def _build_repository_clone_command_block(
        self,
        *,
        repo_config: Dict[str, Any],
        repo_index: int,
        execution_context: Dict[str, Any],
        source_branch: str,
        target_branch: str,
        commit_sha: Optional[str],
        trigger_data: Dict[str, Any],
        credentials: Optional[Dict[int, GitCredential]] = None,
    ) -> Optional[list[str]]:
        """Build shell command blocks for one repository.

        Any resolved credential is recorded in ``credentials`` rather than
        written into the clone URL, so the remote stored in ``.git/config``
        never contains a secret (issue #173).
        """

        repo_url = self._resolve_repository_clone_url(
            repo_config, repo_index, execution_context, trigger_data
        )
        if not repo_url:
            self.logger.error(
                f"No repository URL found for repo #{repo_index + 1}. "
                f"Please add 'repository_url' field to git_clone_config.repositories, "
                f"or select a project in the trigger configuration. "
                f"Repo config: {repo_config}, "
                f"Trigger project ID: {execution_context.get('trigger_project_id')}"
            )
            return None

        credential = self._build_git_credential(
            repo_url, repo_config, execution_context
        )
        if credential is not None and credentials is not None:
            credentials[repo_index] = credential

        # The URL that reaches `git clone` is always credential-free.
        repo_url = strip_url_credentials(repo_url)
        full_path = self._resolve_repository_clone_path(repo_config, repo_index)
        clone_branch = self._resolve_repository_clone_branch(
            repo_config,
            commit_sha=commit_sha,
            source_branch=source_branch,
            trigger_data=trigger_data,
        )

        return [
            self._build_git_pre_clone_shell(full_path),
            self._build_git_clone_shell(repo_url, full_path, clone_branch),
            self._build_git_branch_setup_shell(
                full_path=full_path,
                commit_sha=commit_sha,
                source_branch=source_branch,
                target_branch=target_branch,
                trigger_data=trigger_data,
            ),
            self._build_git_clone_validation_shell(
                full_path=full_path,
                source_branch=source_branch,
                target_branch=target_branch,
                commit_sha=commit_sha,
            ),
        ]

    def _prepare_git_clone_command(self, execution_context: Dict[str, Any]) -> str:
        """
        Prepare git clone commands for multiple repositories with branch management.

        Args:
            execution_context: Execution context

        Returns:
            Git clone commands string (multiple commands joined with &&) or empty string
        """
        try:
            git_config = execution_context.get("git_clone_config", {})
            repositories = self._resolve_git_clone_repositories(
                execution_context, git_config
            )
            if not repositories:
                return ""

            (
                source_branch,
                target_branch,
                commit_sha,
                git_user_name,
                git_user_email,
            ) = self._resolve_git_branch_plan(execution_context, git_config)
            trigger_data = execution_context.get("trigger_event_data", {})
            git_setup_commands = self._build_git_global_setup_commands(
                git_user_name, git_user_email
            )

            clone_commands: list[str] = []
            credentials: Dict[int, GitCredential] = {}
            configured_repos_count = 0
            for idx, repo_config in enumerate(repositories):
                command_block = self._build_repository_clone_command_block(
                    repo_config=repo_config,
                    repo_index=idx,
                    execution_context=execution_context,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    commit_sha=commit_sha,
                    trigger_data=trigger_data,
                    credentials=credentials,
                )
                if command_block is None:
                    continue

                clone_commands.extend(command_block)
                configured_repos_count += 1
                full_path = self._resolve_repository_clone_path(repo_config, idx)
                self.logger.info(
                    f"Prepared git clone for {full_path}: "
                    f"source={source_branch}, target={target_branch}"
                )

            if configured_repos_count == 0:
                error_msg = (
                    f"FATAL: Git clone configured with {len(repositories)} repositories "
                    f"but could not resolve repository URLs for any of them. "
                    f"Please ensure 'repository_url' is set in git_clone_config.repositories, "
                    f"or that the flow is triggered by a webhook with repository information."
                )
                self.logger.error(error_msg)
                return f'echo "{error_msg}" && exit 1'

            # Stash the credentials on the context so the agent can pass them
            # to the container as environment variables. They must never be
            # rendered into the script itself, which is echoed by some images
            # and can end up in `kubectl describe`.
            self._register_git_credentials(execution_context, credentials)

            credential_setup = build_credential_setup_shell(
                use_http_path=needs_http_path_scoping(credentials.values())
            )

            execution_context["_git_target_branch"] = target_branch
            execution_context["_git_source_branch"] = source_branch
            return " && ".join(git_setup_commands + [credential_setup] + clone_commands)

        except Exception as e:
            self.logger.error(f"Error preparing git clone command: {e}", exc_info=True)
            return ""

    def _prepare_git_post_execution_commands(
        self, execution_context: Dict[str, Any]
    ) -> str:
        """
        Prepare git commands to run after agent execution (push, PR/MR creation).

        Args:
            execution_context: Execution context

        Returns:
            Shell command string for post-execution git operations
        """
        try:
            git_config = execution_context.get("git_clone_config", {})

            if not git_config:
                self.logger.debug("No git_clone_config in execution context")
                return ""

            # Check for repositories - if they exist, we should have cloned them
            repositories = git_config.get("repositories", [])
            if not repositories:
                self.logger.debug("No repositories in git_clone_config")
                return ""

            target_branch = execution_context.get("_git_target_branch")
            source_branch = execution_context.get("_git_source_branch", "main")
            create_pr = git_config.get("create_pull_request", False)

            self.logger.info(
                f"Preparing post-execution git commands: "
                f"target_branch={target_branch}, source_branch={source_branch}, "
                f"create_pr={create_pr}, repos={len(repositories)}"
            )

            if not target_branch:
                return ""

            post_commands = []

            for idx, repo_config in enumerate(repositories):
                # Get clone path - handle absolute vs relative paths
                clone_path = repo_config.get("clone_path", f"/workspace-{idx + 1}")
                if clone_path.startswith("/"):
                    # Absolute path
                    full_path = clone_path
                else:
                    # Relative path - prepend /workspace/
                    full_path = f"/workspace/{clone_path}"

                # Get tracker info for PR/MR creation
                tracker_id = repo_config.get("tracker_id")
                git_credentials_map = execution_context.get("git_credentials_map", {})
                tracker_creds = git_credentials_map.get(tracker_id)

                if not tracker_creds:
                    continue

                tracker_type = tracker_creds.get("tracker_type")
                token = tracker_creds.get("token")

                # The REST API token is passed through the environment rather
                # than interpolated into the script, so it cannot leak via the
                # container command line, `kubectl describe`, or a shell trace
                # (issue #173). `token_ref` is the shell expansion to use.
                token_ref = ""
                if token:
                    token_ref = "${%s}" % self._register_git_api_token(
                        execution_context, idx, token
                    )

                # Commands to check for commits and push
                # Note: Directory is guaranteed to exist because git clone validation would have failed earlier
                repo_post_commands = [
                    f"cd {full_path}",
                    # Check if there are any commits on target branch vs source
                    f'COMMIT_COUNT=$(git rev-list --count {source_branch}..{target_branch} 2>/dev/null || echo "0")',
                    'if [ "$COMMIT_COUNT" -gt "0" ]; then',
                    f'  echo "Found $COMMIT_COUNT commits on {target_branch}, pushing..."',
                    f"  git push origin {target_branch}",
                ]

                # Add PR/MR creation if enabled
                if create_pr and token:
                    # Get Preloop URL for execution link
                    import os

                    preloop_url = os.getenv("PRELOOP_URL", "http://localhost:8000")
                    execution_id = execution_context.get("execution_id", "")
                    flow_name = execution_context.get("flow_name", "Automated changes")

                    # Check if user provided custom title/description
                    custom_pr_title = git_config.get("pull_request_title")
                    custom_pr_description = git_config.get("pull_request_description")

                    # Only use custom values if they're actually set (not None or empty)
                    use_custom = custom_pr_title and custom_pr_title.strip()

                    if tracker_type == "github":
                        # Extract owner/repo from URL
                        repo_url = self._extract_repo_url_from_trigger(
                            execution_context.get("trigger_event_data", {})
                        )

                        # If no URL from trigger, try to get from project configuration
                        if not repo_url:
                            project_id = repo_config.get("project_id")
                            if not project_id:
                                project_id = execution_context.get("trigger_project_id")
                            if project_id:
                                repo_url = self._get_repo_url_from_project(
                                    project_id, execution_context.get("account_id")
                                )
                                if repo_url:
                                    self.logger.info(
                                        f"Using repo URL from project {project_id} for PR creation"
                                    )

                        if repo_url:
                            # Parse owner/repo from URL like https://github.com/owner/repo
                            repo_parts = repo_url.rstrip("/").split("/")
                            if len(repo_parts) >= 2:
                                owner = repo_parts[-2]
                                repo = repo_parts[-1].replace(".git", "")

                                # Build PR creation command with dynamic title/description
                                if use_custom:
                                    # Use custom title and description
                                    pr_create_cmd = f"""
    curl -X POST \\
      -H "Authorization: token {token_ref}" \\
      -H "Accept: application/vnd.github.v3+json" \\
      https://api.github.com/repos/{owner}/{repo}/pulls \\
      -d "$(cat <<'PREOF'
{{
  "title": "{custom_pr_title}",
  "body": "{custom_pr_description or ""}",
  "head": "{target_branch}",
  "base": "{source_branch}"
}}
PREOF
)" \\
      || echo "Failed to create PR (may already exist)"
"""
                                else:
                                    # Build title and description from commits
                                    execution_link = f"{preloop_url}/console/flows/executions/{execution_id}"
                                    pr_create_cmd = f"""
    # Build PR title and description based on commit count
    if [ "$COMMIT_COUNT" -eq "1" ]; then
      # Single commit - use commit message
      PR_TITLE=$(git log -1 --format=%s {source_branch}..{target_branch})
      COMMIT_BODY=$(git log -1 --format=%b {source_branch}..{target_branch})
      PR_BODY="Automated changes from Preloop flow: [{flow_name}]({execution_link})\\n\\n$COMMIT_BODY"
    else
      # Multiple commits - use flow name and list commits
      PR_TITLE="[Preloop] {flow_name}"
      COMMIT_LIST=$(git log --format="- %s" {source_branch}..{target_branch})
      PR_BODY="Automated changes from Preloop flow: [{flow_name}]({execution_link})\\n\\n**Commits:**\\n$COMMIT_LIST"
    fi

    # Create PR with dynamic title/body
    curl -X POST \\
      -H "Authorization: token {token_ref}" \\
      -H "Accept: application/vnd.github.v3+json" \\
      https://api.github.com/repos/{owner}/{repo}/pulls \\
      -d "$(cat <<PREOF
{{
  "title": "$PR_TITLE",
  "body": "$PR_BODY",
  "head": "{target_branch}",
  "base": "{source_branch}"
}}
PREOF
)" \\
      || echo "Failed to create PR (may already exist)"
"""
                                repo_post_commands.append(pr_create_cmd)

                    elif tracker_type == "gitlab":
                        # Extract project path and GitLab host from URL
                        repo_url = self._extract_repo_url_from_trigger(
                            execution_context.get("trigger_event_data", {})
                        )

                        # If no URL from trigger, try to get from project configuration
                        if not repo_url:
                            project_id = repo_config.get("project_id")
                            if not project_id:
                                project_id = execution_context.get("trigger_project_id")
                            if project_id:
                                repo_url = self._get_repo_url_from_project(
                                    project_id, execution_context.get("account_id")
                                )
                                if repo_url:
                                    self.logger.info(
                                        f"Using repo URL from project {project_id} for MR creation"
                                    )

                        if repo_url:
                            # Parse GitLab host from URL (e.g., gitlab.spacecode.ai or gitlab.com)
                            from urllib.parse import urlparse

                            parsed_url = urlparse(repo_url)
                            gitlab_host = parsed_url.netloc
                            # Remove credentials if present (e.g., gitlab-ci-token:xxx@host)
                            if "@" in gitlab_host:
                                gitlab_host = gitlab_host.split("@")[-1]

                            # Parse project path from URL
                            repo_path = repo_url.rstrip("/").split("://")[-1]
                            repo_path = repo_path.split("/", 1)[-1].replace(".git", "")
                            # Remove credentials from path if present
                            if "@" in repo_path:
                                repo_path = repo_path.split("@", 1)[-1]

                            # URL encode the project path
                            import urllib.parse

                            encoded_path = urllib.parse.quote(repo_path, safe="")

                            self.logger.info(
                                "Creating GitLab MR (create_pr=%s)",
                                create_pr,
                            )

                            # Build MR creation command with dynamic title/description
                            if use_custom:
                                # Use custom title and description
                                mr_create_cmd = f"""
    echo "Creating Merge Request on {gitlab_host}..."
    curl -X POST \\
      -H "PRIVATE-TOKEN: {token_ref}" \\
      -H "Content-Type: application/json" \\
      https://{gitlab_host}/api/v4/projects/{encoded_path}/merge_requests \\
      -d "$(cat <<'MREOF'
{{
  "source_branch": "{target_branch}",
  "target_branch": "{source_branch}",
  "title": "{custom_pr_title}",
  "description": "{custom_pr_description or ""}"
}}
MREOF
)" \\
      || echo "Failed to create MR (may already exist)"
"""
                            else:
                                # Build title and description from commits
                                execution_link = f"{preloop_url}/console/flows/executions/{execution_id}"
                                mr_create_cmd = f"""
    # Build MR title and description based on commit count
    if [ "$COMMIT_COUNT" -eq "1" ]; then
      # Single commit - use commit message
      MR_TITLE=$(git log -1 --format=%s {source_branch}..{target_branch})
      COMMIT_BODY=$(git log -1 --format=%b {source_branch}..{target_branch})
      MR_DESCRIPTION="Automated changes from Preloop flow: [{flow_name}]({execution_link})\\n\\n$COMMIT_BODY"
    else
      # Multiple commits - use flow name and list commits
      MR_TITLE="[Preloop] {flow_name}"
      COMMIT_LIST=$(git log --format="- %s" {source_branch}..{target_branch})
      MR_DESCRIPTION="Automated changes from Preloop flow: [{flow_name}]({execution_link})\\n\\n**Commits:**\\n$COMMIT_LIST"
    fi

    echo "Creating Merge Request on {gitlab_host}..."
    curl -X POST \\
      -H "PRIVATE-TOKEN: {token_ref}" \\
      -H "Content-Type: application/json" \\
      https://{gitlab_host}/api/v4/projects/{encoded_path}/merge_requests \\
      -d "$(cat <<MREOF
{{
  "source_branch": "{target_branch}",
  "target_branch": "{source_branch}",
  "title": "$MR_TITLE",
  "description": "$MR_DESCRIPTION"
}}
MREOF
)" \\
      || echo "Failed to create MR (may already exist)"
"""
                            repo_post_commands.append(mr_create_cmd)

                repo_post_commands.extend(
                    [
                        "else",
                        f'  echo "No commits on {target_branch}, skipping push"',
                        "fi",
                        "cd /workspace",
                    ]
                )

                post_commands.extend(repo_post_commands)

            if not post_commands:
                return ""

            # Join commands with newlines instead of && to properly handle if-else-fi blocks
            return "\n".join(post_commands)

        except Exception as e:
            self.logger.error(
                f"Error preparing git post-execution commands: {e}", exc_info=True
            )
            return ""

    def _get_repo_url_from_project(
        self, project_id: str, account_id: str
    ) -> Optional[str]:
        """Construct repository URL from project and tracker information.

        Uses the tracker URL and project slug to construct a clone URL in the
        format:
        - GitLab: https://{host}/{slug}.git
        - GitHub: https://github.com/{slug}.git

        The URL is deliberately credential-free (issue #173). The tracker token
        is still required for the lookup to succeed, because a project whose
        tracker has no key configured cannot be cloned at all, but the token is
        delivered separately through the git credential helper.

        Args:
            project_id: Project ID
            account_id: Account ID

        Returns:
            Credential-free repository clone URL, or None if not found
        """
        self.logger.info(
            f"Looking up repo URL for project_id={project_id}, account_id={account_id}"
        )
        try:
            from preloop.models.crud import crud_project, crud_tracker
            from preloop.models.db.session import get_db_session

            db = next(get_db_session())
            try:
                # Get project from database - don't filter by account_id since
                # Project doesn't have a direct account_id field
                project = crud_project.get(db, id=str(project_id))
                if not project:
                    self.logger.info(
                        f"Project {project_id} not found by ID, trying slug/identifier"
                    )
                    # Also try looking up by slug or identifier
                    project = crud_project.get_by_slug_or_identifier(
                        db, slug_or_identifier=str(project_id)
                    )

                if not project:
                    self.logger.error(
                        f"Project {project_id} not found in database by ID or slug. "
                        f"Account: {account_id}"
                    )
                    return None

                self.logger.info(
                    f"Found project: id={project.id}, slug={project.slug}, "
                    f"org_id={project.organization_id}"
                )

                if not project.slug:
                    self.logger.warning(
                        f"Project {project_id} has no slug, cannot construct repository URL"
                    )
                    return None

                # Get the organization to find the tracker
                organization = project.organization
                if not organization:
                    self.logger.warning(
                        f"Project {project_id} has no organization, cannot get tracker"
                    )
                    return None

                # Get the tracker
                tracker = crud_tracker.get(
                    db, id=organization.tracker_id, account_id=account_id
                )
                if not tracker:
                    self.logger.warning(
                        f"Tracker {organization.tracker_id} not found for account {account_id}"
                    )
                    return None

                # Get token from tracker (we always have tokens for GitHub/GitLab)
                token = tracker.resolved_api_key
                if not token:
                    self.logger.warning(
                        f"Tracker {tracker.id} has no API key configured"
                    )
                    return None

                # Construct the clone URL based on tracker type
                tracker_type = tracker.tracker_type.lower()
                slug = project.slug

                if tracker_type == "gitlab":
                    # GitLab format: https://{host}/{slug}.git (no credentials)
                    if not tracker.url:
                        self.logger.warning(
                            f"GitLab tracker {tracker.id} has no URL configured"
                        )
                        return None

                    # Parse the host from tracker URL
                    # tracker.url might be like "https://gitlab.spacecode.ai" or "https://gitlab.com"
                    from urllib.parse import urlparse

                    parsed = urlparse(tracker.url)
                    host = parsed.netloc or parsed.path

                    # Ensure slug ends with .git
                    if not slug.endswith(".git"):
                        slug = f"{slug}.git"

                    clone_url = f"https://{host}/{slug}"
                    self.logger.info(
                        f"Constructed GitLab clone URL for {slug} on {host}"
                    )
                    return clone_url

                elif tracker_type == "github":
                    # GitHub format: https://github.com/{slug}.git (no credentials)
                    # Ensure slug ends with .git
                    if not slug.endswith(".git"):
                        slug = f"{slug}.git"

                    clone_url = f"https://github.com/{slug}"
                    self.logger.info(f"Constructed GitHub clone URL for {slug}")
                    return clone_url

                else:
                    self.logger.warning(
                        f"Tracker type '{tracker_type}' not supported for git clone"
                    )
                    return None

            finally:
                db.close()

        except Exception as e:
            self.logger.error(
                f"Error constructing repository URL from project {project_id}: {e}",
                exc_info=True,
            )
            return None

    def _get_token_from_project(
        self, project_id: str, account_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Get the API token and tracker type from a project's tracker.

        Args:
            project_id: Project ID
            account_id: Account ID

        Returns:
            Tuple of (token, tracker_type) or (None, None) if not found
        """
        try:
            from preloop.models.crud import crud_project, crud_tracker
            from preloop.models.db.session import get_db_session

            db = next(get_db_session())
            try:
                project = crud_project.get(db, id=str(project_id))
                if not project:
                    return None, None

                organization = project.organization
                if not organization:
                    return None, None

                tracker = crud_tracker.get(db, id=organization.tracker_id)
                resolved_token = tracker.resolved_api_key if tracker else ""
                if not tracker or not resolved_token:
                    return None, None

                return resolved_token, tracker.tracker_type.lower()

            finally:
                db.close()

        except Exception as e:
            self.logger.warning(f"Error getting token from project {project_id}: {e}")
            return None, None

    def _extract_merge_request_ref_from_trigger(
        self, trigger_data: Dict[str, Any]
    ) -> Optional[str]:
        """Extract a git fetch ref for the MR/PR head commit.

        Supports:
        - GitLab: refs/merge-requests/{iid}/head
        - GitHub: pull/{number}/head
        """
        try:
            payload = trigger_data.get("payload", trigger_data)
            if not isinstance(payload, dict):
                return None

            obj_attrs = payload.get("object_attributes")
            if isinstance(obj_attrs, dict) and obj_attrs.get("iid") is not None:
                ref = f"refs/merge-requests/{obj_attrs['iid']}/head"
                self.logger.info(f"Extracted GitLab MR fetch ref: {ref}")
                return ref

            pr = payload.get("pull_request")
            if isinstance(pr, dict) and pr.get("number") is not None:
                ref = f"pull/{pr['number']}/head"
                self.logger.info(f"Extracted GitHub PR fetch ref: {ref}")
                return ref

            return None
        except Exception as e:
            self.logger.debug(f"Error extracting merge request ref from trigger: {e}")
            return None

    def _extract_target_branch_from_trigger(
        self, trigger_data: Dict[str, Any]
    ) -> Optional[str]:
        """Extract the PR/MR target/base branch name from trigger event data.

        Supports:
        - GitHub: payload.pull_request.base.ref
        - GitLab: payload.object_attributes.target_branch
        """
        try:
            payload = trigger_data.get("payload", trigger_data)
            if not isinstance(payload, dict):
                return None

            pr = payload.get("pull_request")
            if isinstance(pr, dict):
                base = pr.get("base")
                if isinstance(base, dict) and base.get("ref"):
                    branch = base["ref"]
                    self.logger.info(
                        f"Extracted target branch from GitHub PR: {branch}"
                    )
                    return branch

            obj_attrs = payload.get("object_attributes")
            if isinstance(obj_attrs, dict) and obj_attrs.get("target_branch"):
                branch = obj_attrs["target_branch"]
                self.logger.info(f"Extracted target branch from GitLab MR: {branch}")
                return branch

            project = payload.get("project")
            if isinstance(project, dict) and project.get("default_branch"):
                return project["default_branch"]

            return None
        except Exception as e:
            self.logger.debug(f"Error extracting target branch from trigger: {e}")
            return None

    def _extract_source_branch_from_trigger(
        self, trigger_data: Dict[str, Any]
    ) -> Optional[str]:
        """Extract the PR/MR source branch name from trigger event data.

        Supports:
        - GitHub: payload.pull_request.head.ref
        - GitLab: payload.object_attributes.source_branch
        """
        try:
            payload = trigger_data.get("payload", trigger_data)
            if not isinstance(payload, dict):
                return None

            # GitHub PR - head.ref is the source branch
            pr = payload.get("pull_request")
            if isinstance(pr, dict):
                head = pr.get("head")
                if isinstance(head, dict) and head.get("ref"):
                    branch = head["ref"]
                    self.logger.info(
                        f"Extracted source branch from GitHub PR: {branch}"
                    )
                    return branch

            # GitLab MR - object_attributes.source_branch
            obj_attrs = payload.get("object_attributes")
            if isinstance(obj_attrs, dict) and obj_attrs.get("source_branch"):
                branch = obj_attrs["source_branch"]
                self.logger.info(f"Extracted source branch from GitLab MR: {branch}")
                return branch

            return None
        except Exception as e:
            self.logger.debug(f"Error extracting source branch from trigger: {e}")
            return None

    def _extract_commit_sha_from_trigger(
        self, trigger_data: Dict[str, Any]
    ) -> Optional[str]:
        """Extract the commit SHA from trigger event data.

        Supports:
        - GitHub push: payload.head_commit.id or payload.after
        - GitHub PR: payload.pull_request.head.sha
        - GitLab MR: payload.object_attributes.last_commit.id or .sha
        """
        try:
            payload = trigger_data.get("payload", trigger_data)
            if not isinstance(payload, dict):
                return None

            # GitHub push event
            if "head_commit" in payload:
                sha = payload["head_commit"].get("id")
                if sha:
                    return sha

            # GitLab MR
            obj_attrs = payload.get("object_attributes", {})
            if isinstance(obj_attrs, dict):
                if "last_commit" in obj_attrs:
                    sha = obj_attrs["last_commit"].get("id")
                    if sha:
                        return sha
                if obj_attrs.get("sha"):
                    return obj_attrs["sha"]

            # GitHub PR
            pr = payload.get("pull_request")
            if isinstance(pr, dict):
                head = pr.get("head")
                if isinstance(head, dict) and head.get("sha"):
                    return head["sha"]

            # Direct references
            if "sha" in payload:
                return payload["sha"]
            if "after" in payload:
                return payload["after"]

            return None
        except Exception as e:
            self.logger.debug(f"Error extracting commit SHA from trigger: {e}")
            return None

    def _extract_repo_url_from_trigger(self, trigger_data: Dict[str, Any]) -> str:
        """Extract repository URL from trigger event data.

        The trigger_data structure can be:
        - {"payload": {"repository": {...}}} for GitHub webhooks
        - {"payload": {"project": {...}}} for GitLab webhooks
        - {"repository": {...}} if payload is at top level
        """
        try:
            # Check if the actual payload is nested under "payload" key
            payload = trigger_data.get("payload", trigger_data)
            if not isinstance(payload, dict):
                self.logger.debug(f"Payload is not a dict: {type(payload)}")
                return ""

            # GitHub structure
            if "repository" in payload:
                repo = payload["repository"]
                if isinstance(repo, dict):
                    url = repo.get("clone_url") or repo.get("html_url") or ""
                    if url:
                        self.logger.info(f"Found GitHub repo URL in trigger: {url}")
                    return url

            # GitLab structure
            if "project" in payload:
                project = payload["project"]
                if isinstance(project, dict):
                    url = (
                        project.get("http_url_to_repo") or project.get("web_url") or ""
                    )
                    if url:
                        self.logger.info(f"Found GitLab repo URL in trigger: {url}")
                    return url

            self.logger.debug(
                f"No repository/project found in trigger data. "
                f"Top-level keys: {list(trigger_data.keys())}, "
                f"Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}"
            )
            return ""
        except Exception as e:
            self.logger.error(f"Error extracting repo URL from trigger: {e}")
            return ""

    async def cleanup(self):
        """Cleanup resources (close Docker client, Kubernetes client, etc.)."""
        if self._docker_client:
            await self._docker_client.close()
            self._docker_client = None

        if self._k8s_api_client:
            await self._k8s_api_client.close()
            self._k8s_api_client = None
            self._k8s_batch_api = None
            self._k8s_core_api = None
            self._k8s_initialized = False
