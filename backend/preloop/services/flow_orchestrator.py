import logging
import uuid
import json
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import re

from sqlalchemy.orm import Session
from nats.aio.client import Client

from preloop.models import schemas
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
    crud_user,
)
from preloop.models.models.flow import Flow
from preloop.models.models.ai_model import AIModel
from preloop.models.models.runtime_session import RuntimeSession
from preloop.agents import create_agent_executor, AgentStatus
from preloop.services.prompt_resolvers import (
    resolver_registry,
    ResolverContext,
    TriggerEventResolver,
    ProjectResolver,
    AccountResolver,
)
from preloop.services.flow_execution_logger import FlowExecutionLogger
from preloop.sync.event_normalizer import attach_trigger_subject
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.utils.repo_urls import inject_oauth_token, tracker_host_kind
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

# Sentinel string that agents print when completing successfully.
FLOW_SUCCESS_SENTINEL = "FLOW_EXECUTION_SUCCESS"

# Marker printed by the agent script immediately before the agent command runs.
# Sentinel detection is suppressed until this marker is seen in the logs,
# preventing false positives from the prompt echo that contains the sentinel
# instruction text.
AGENT_EXEC_START_MARKER = "PRELOOP_AGENT_EXEC_START"
MCP_TOOL_LOOP_PATTERN_MAX_LENGTH = 3
MCP_TOOL_LOOP_MIN_REPETITIONS = 3
MCP_TOOL_LOOP_SINGLE_CALL_REPETITIONS = 4
MCP_TOOL_LOOP_DUPLICATE_WINDOW_SECONDS = 0.5

# Instruction appended to prompts to have agents signal success.
# IMPORTANT: The sentinel is kept INLINE (not on its own line) so that when
# the prompt text is echoed in logs, it cannot trigger the exact-line detector.
FLOW_SUCCESS_INSTRUCTION = f"""

---
IMPORTANT: When you have successfully completed your task, you MUST print the following marker on a line by itself: {FLOW_SUCCESS_SENTINEL}
Do not include any other text on the same line as the marker. This signals successful completion.
---"""


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types to serializable ones."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    return obj


def _exception_message(exc: BaseException) -> str:
    """Return a useful message for exceptions whose str() is empty."""
    return str(exc) or exc.__class__.__name__


class FlowExecutionOrchestrator:
    """Manages the end-to-end lifecycle of a single Flow invocation."""

    def __init__(
        self,
        db: Session,
        flow_id: uuid.UUID,
        trigger_event_data: Dict[str, Any],
        nats_client: Client,
    ):
        self.db = db
        self.flow_id = flow_id
        self.trigger_event_data = trigger_event_data
        self.flow: Optional[Flow] = None
        self.ai_model: Optional[AIModel] = None
        self.execution_log = None
        self.runtime_session: Optional[RuntimeSession] = None
        self.nats_client: Client = nats_client
        self.execution_logger = FlowExecutionLogger()
        self.temporary_api_key_id: Optional[uuid.UUID] = None
        self._log_streaming_task: Optional[asyncio.Task] = None
        self._command_subscription: Optional[Any] = None
        self._stop_requested = asyncio.Event()
        self._success_sentinel_seen = asyncio.Event()
        self._agent_exec_started = (
            False  # Set when AGENT_EXEC_START_MARKER seen in logs
        )
        self._user_messages: asyncio.Queue = asyncio.Queue()

        # Execution metrics tracked during execution
        self.total_tokens: int = 0
        self.tool_calls_count: int = 0
        self.estimated_cost: float = 0.0

        # Commit status tracking
        self._tracker_client = None
        self._commit_sha: Optional[str] = None
        self._status_context: str = "preloop"
        self._is_recovered: bool = False  # Set to True during execution recovery
        # Set when a sync worker owns this orchestrator (claim lease heartbeat).
        self._orchestrator_worker_id: Optional[str] = None

    def _extract_commit_sha(self) -> Optional[str]:
        """Extract the commit SHA from the trigger event data.

        Looks for commit SHA in common locations for different event types.
        """
        payload = self.trigger_event_data.get("payload", {})

        # Ensure payload is a dict (could be a string in edge cases)
        if not isinstance(payload, dict):
            logger.debug(f"Payload is not a dict: {type(payload)}")
            return None

        # Try common locations for commit SHA
        # GitHub push event
        if "head_commit" in payload:
            sha = payload["head_commit"].get("id")
            if sha:
                logger.debug(f"Found commit SHA in head_commit.id: {sha[:8]}")
                return sha

        # GitHub/GitLab pull request / merge request events
        object_attrs = payload.get("object_attributes", {})
        if object_attrs:
            # GitLab MR
            if "last_commit" in object_attrs:
                sha = object_attrs["last_commit"].get("id")
                if sha:
                    logger.debug(
                        f"Found commit SHA in object_attributes.last_commit.id: {sha[:8]}"
                    )
                    return sha
            # GitLab may also have sha directly
            if "sha" in object_attrs:
                sha = object_attrs["sha"]
                if sha:
                    logger.debug(
                        f"Found commit SHA in object_attributes.sha: {sha[:8]}"
                    )
                    return sha

        # GitHub PR event - check for head sha
        if "pull_request" in payload:
            pr = payload["pull_request"]
            if "head" in pr:
                sha = pr["head"].get("sha")
                if sha:
                    logger.debug(
                        f"Found commit SHA in pull_request.head.sha: {sha[:8]}"
                    )
                    return sha

        # Direct commit reference
        if "commit" in payload:
            commit = payload["commit"]
            if isinstance(commit, dict):
                sha = commit.get("sha") or commit.get("id")
                if sha:
                    logger.debug(f"Found commit SHA in commit: {sha[:8]}")
                    return sha

        # Check for sha at top level
        if "sha" in payload:
            logger.debug(f"Found commit SHA at top level: {payload['sha'][:8]}")
            return payload["sha"]

        # Check in after (for push events)
        if "after" in payload:
            logger.debug(f"Found commit SHA in after: {payload['after'][:8]}")
            return payload["after"]

        logger.debug(f"No commit SHA found in payload. Keys: {list(payload.keys())}")
        return None

    async def _get_tracker_client_for_status(self):
        """Get a tracker client for updating commit status.

        Returns None if we can't get a valid client (e.g., no project configured).
        """
        if self._tracker_client is not None:
            logger.debug("[CommitStatus] Using cached tracker client")
            return self._tracker_client

        try:
            # Get project from trigger_project_ids on the flow
            if not self.flow:
                logger.warning("[CommitStatus] No flow object available")
                return None

            # Get the first project ID from the array (if any)
            trigger_project_id = None
            if self.flow.trigger_project_ids and len(self.flow.trigger_project_ids) > 0:
                trigger_project_id = self.flow.trigger_project_ids[0]

            if not trigger_project_id:
                # This is expected for flows not tied to a specific project
                logger.debug(
                    "[CommitStatus] Flow has no trigger_project_ids - skipping status update"
                )
                return None

            from preloop.models.crud import crud_project
            from preloop.api.common import get_tracker_client

            project = crud_project.get(self.db, id=trigger_project_id)
            if not project:
                logger.warning(
                    f"[CommitStatus] Project not found for trigger_project_id: "
                    f"{trigger_project_id}"
                )
                return None

            if not project.organization_id:
                logger.warning(
                    f"[CommitStatus] Project {project.id} has no organization_id"
                )
                return None

            # Create a minimal user context for auth
            account = crud_account.get(self.db, id=self.flow.account_id)
            if not account:
                logger.warning(
                    f"[CommitStatus] Account not found: {self.flow.account_id}"
                )
                return None

            # Get the account owner or first admin
            users = crud_user.get_by_account(self.db, account_id=account.id, limit=1)
            if not users:
                logger.warning(
                    f"[CommitStatus] No users found for account: {account.id}"
                )
                return None

            logger.info(
                f"[CommitStatus] Getting tracker client for project {project.id}, "
                f"org {project.organization_id}, user {users[0].username}"
            )

            self._tracker_client = await get_tracker_client(
                organization_id=project.organization_id,
                project_id=project.id,
                db=self.db,
                current_user=users[0],
            )
            return self._tracker_client

        except Exception as e:
            logger.error(
                f"[CommitStatus] Exception getting tracker client: {e}",
                exc_info=True,
            )
            return None

    async def _update_commit_status(
        self,
        state: str,
        description: Optional[str] = None,
    ):
        """Update the commit status on the PR/MR.

        Args:
            state: Status state (pending, success, failure, error)
            description: Optional description text
        """
        # Only log at debug level initially - upgrade to info if we actually update
        logger.debug(
            f"[CommitStatus] Checking if status update needed (state='{state}')"
        )

        # Skip commit status updates during execution recovery
        # to avoid making external API calls for old/stale executions
        if self._is_recovered:
            logger.info("[CommitStatus] Skipping - execution is recovered")
            return

        if not self._commit_sha:
            self._commit_sha = self._extract_commit_sha()
            if self._commit_sha:
                logger.info(
                    f"[CommitStatus] Extracted commit SHA: {self._commit_sha[:8]}"
                )
            else:
                # Log more details about what's in trigger_event_data
                payload = self.trigger_event_data.get("payload", {})
                logger.info(
                    f"[CommitStatus] No commit SHA found. "
                    f"trigger_event_data keys: {list(self.trigger_event_data.keys())}, "
                    f"payload type: {type(payload).__name__}, "
                    f"payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}"
                )

        if not self._commit_sha:
            # This is expected for flows not triggered by commit/PR events
            logger.debug(
                "[CommitStatus] No commit SHA available - skipping status update"
            )
            return

        try:
            logger.info(
                f"[CommitStatus] Getting tracker client. "
                f"Flow ID: {self.flow_id}, "
                f"trigger_project_ids: {self.flow.trigger_project_ids if self.flow else None}"
            )

            tracker_client = await self._get_tracker_client_for_status()
            if not tracker_client:
                logger.warning(
                    f"[CommitStatus] Could not get tracker client. "
                    f"Flow trigger_project_ids: {self.flow.trigger_project_ids if self.flow else None}, "
                    f"account_id: {self.flow.account_id if self.flow else None}"
                )
                return

            logger.info(
                f"[CommitStatus] Got tracker client: {type(tracker_client).__name__}, "
                f"connection_details: {list(tracker_client.connection_details.keys()) if hasattr(tracker_client, 'connection_details') else 'N/A'}"
            )

            # Check if the tracker supports commit status
            if not hasattr(tracker_client, "create_commit_status"):
                logger.info(
                    f"[CommitStatus] Tracker {type(tracker_client).__name__} doesn't support commit status"
                )
                return

            # Build the target URL for the execution
            target_url = None
            if self.execution_log:
                # Construct absolute URL to the execution details page
                # GitHub/GitLab require absolute URLs for commit status links
                from preloop.config import settings

                base_url = getattr(settings, "preloop_url", None) or getattr(
                    settings, "PRELOOP_URL", None
                )
                if base_url:
                    # Remove trailing slash if present
                    base_url = base_url.rstrip("/")
                    target_url = (
                        f"{base_url}/console/flows/executions/{self.execution_log.id}"
                    )
                else:
                    # Fallback to relative path if no base URL configured
                    logger.warning(
                        "[CommitStatus] PRELOOP_URL not configured, using relative URL"
                    )
                    target_url = f"/console/flows/executions/{self.execution_log.id}"

            # Log the API call we're about to make
            logger.info(
                f"[CommitStatus] Calling create_commit_status: "
                f"sha={self._commit_sha[:8]}, state={state}, context={self._status_context}, "
                f"target_url={target_url[:50] if target_url else None}..."
            )

            await tracker_client.create_commit_status(
                sha=self._commit_sha,
                state=state,
                context=self._status_context,
                description=description,
                target_url=target_url,
            )

            logger.info(
                f"[CommitStatus] SUCCESS - Updated to '{state}' on {self._commit_sha[:8]}"
            )

        except Exception as e:
            # Don't fail the execution if status update fails
            logger.error(
                f"[CommitStatus] FAILED to update: {e}",
                exc_info=True,
            )

    @staticmethod
    async def send_command(
        execution_id: str,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        nats_client: Optional[Client] = None,
    ):
        """
        Send a command to a running flow execution via NATS.

        Args:
            execution_id: ID of the flow execution
            command: Command to send (e.g., 'stop', 'send_message')
            payload: Optional command payload
            nats_client: Optional NATS client (if not provided, will try to get from app state)

        Raises:
            RuntimeError: If NATS client is not available
        """
        # If nats_client not provided, try to get it from app state
        if nats_client is None:
            try:
                import inspect

                # Try to find the app instance in the call stack
                for frame_info in inspect.stack():
                    frame_locals = frame_info.frame.f_locals
                    if "request" in frame_locals:
                        request = frame_locals["request"]
                        if hasattr(request, "app") and hasattr(request.app, "state"):
                            nats_client = getattr(request.app.state, "nats", None)
                            break
            except Exception:
                # NATS may be unavailable when send_command is invoked outside a request.
                pass

        if nats_client is None:
            raise RuntimeError("NATS client not available or not connected")

        try:
            command_subject = f"flow-commands.{execution_id}"
            command_data = {"command": command, "payload": payload or {}}

            await nats_client.publish(
                command_subject, json.dumps(command_data).encode()
            )
            logger.info(
                f"Sent command '{command}' to execution {execution_id} via NATS"
            )
        except Exception as e:
            logger.error(f"Failed to send command via NATS: {e}", exc_info=True)
            raise

    # NATS max payload is typically 1MB; use 900KB to leave headroom
    NATS_MAX_PAYLOAD_BYTES = 900 * 1024

    async def _publish_update(self, message_type: str, payload: Dict[str, Any]):
        """
        Publishes a structured message to the NATS stream for real-time updates.
        Includes account_id for proper filtering to prevent cross-account data leaks.
        Automatically truncates large payloads to avoid NATS MaxPayloadError.
        """
        if not self.nats_client or not self.nats_client.is_connected:
            logger.warning("NATS client not available, skipping update publish.")
            return

        if not self.execution_log:
            logger.warning("Execution log not created yet, skipping update publish.")
            return

        try:
            message = {
                "execution_id": str(self.execution_log.id),
                "flow_id": str(self.flow_id),
                "account_id": str(self.flow.account_id)
                if self.flow and self.flow.account_id
                else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": message_type,
                "payload": payload,
            }
            subject = f"flow-updates.{self.execution_log.id}"
            encoded_message = json.dumps(message).encode()

            # Check if message exceeds NATS max payload
            if len(encoded_message) > self.NATS_MAX_PAYLOAD_BYTES:
                # Truncate the payload - specifically handle "line" field for log lines
                truncated_payload = dict(payload)
                if "line" in truncated_payload and isinstance(
                    truncated_payload["line"], str
                ):
                    # Calculate how much we need to truncate
                    excess = len(encoded_message) - self.NATS_MAX_PAYLOAD_BYTES
                    line = truncated_payload["line"]
                    # Truncate line with some extra margin
                    max_line_len = max(1000, len(line) - excess - 1000)
                    truncated_payload["line"] = (
                        line[:max_line_len]
                        + f"\n... [truncated {len(line) - max_line_len} chars]"
                    )
                    truncated_payload["truncated"] = True
                    message["payload"] = truncated_payload
                    encoded_message = json.dumps(message).encode()
                    logger.warning(
                        f"Truncated large log line ({len(line)} -> {max_line_len} chars) "
                        f"to fit NATS max payload"
                    )
                else:
                    # For other payload types, skip publishing this message
                    logger.warning(
                        f"Skipping {message_type} update: payload too large "
                        f"({len(encoded_message)} bytes > {self.NATS_MAX_PAYLOAD_BYTES})"
                    )
                    return

            await self.nats_client.publish(subject, encoded_message)
            logger.debug(f"Published {message_type} to NATS subject '{subject}'")
        except Exception as e:
            logger.error(f"Failed to publish update to NATS: {e}", exc_info=True)

    def _get_flow_details(self):
        """Retrieve the Flow definition and associated AIModel."""
        logger.info(f"Retrieving flow details for flow_id: {self.flow_id}")

        # Get flow - convert UUID to string for comparison
        flow_id_str = (
            str(self.flow_id) if isinstance(self.flow_id, uuid.UUID) else self.flow_id
        )
        # Use CRUD layer without account filtering since this is an internal service
        # and we don't have the account_id yet (it's a property of the flow itself)
        self.flow = crud_flow.get(self.db, id=flow_id_str)
        if not self.flow:
            raise ValueError(f"Flow with id {self.flow_id} not found")

        logger.info(
            f"Found flow: {self.flow.name} (agent_type: {self.flow.agent_type})"
        )

        # Get AI model if specified
        if self.flow.ai_model_id:
            ai_model_id_str = (
                str(self.flow.ai_model_id)
                if isinstance(self.flow.ai_model_id, uuid.UUID)
                else self.flow.ai_model_id
            )
            self.ai_model = crud_ai_model.get(self.db, id=ai_model_id_str)
            if not self.ai_model:
                logger.warning(
                    f"AI model {self.flow.ai_model_id} not found for flow {self.flow_id}"
                )
            else:
                logger.info(
                    f"Loaded AI model: {self.ai_model.name} ({self.ai_model.model_identifier})"
                )
        else:
            logger.info("No AI model specified for this flow")

    def _resolve_execution_model_runtime(self):
        """Resolve model runtime for the selected flow model."""
        if not self.ai_model:
            return None

        return resolve_ai_model_runtime(self.ai_model, allow_gateway=True)

    async def _resolve_prompt(self) -> str:
        """
        Resolve dynamic placeholders in the prompt template using registered resolvers.

        Supports placeholders like:
        - {{trigger_event.payload.issue.title}}
        - {{project.name}}
        - {{account.email}}
        """
        logger.info("Resolving prompt template")

        # Ensure resolvers are registered
        self._ensure_resolvers_registered()

        prompt_template = self.flow.prompt_template
        resolved_prompt = prompt_template

        # Create resolver context
        resolver_context = ResolverContext(
            db=self.db,
            trigger_event_data=self.trigger_event_data,
            flow_id=str(self.flow_id),
            execution_id=str(self.execution_log.id) if self.execution_log else "",
        )

        # Extract all {{placeholder}} patterns
        placeholders = re.findall(r"\{\{(\w+(?:\.\w+)*)\}\}", prompt_template)

        for placeholder in placeholders:
            # Split prefix and path (e.g., "trigger_event.payload.title" -> "trigger_event" + "payload.title")
            parts = placeholder.split(".", 1)
            prefix = parts[0]
            path = parts[1] if len(parts) > 1 else ""

            # Get resolver for this prefix
            resolver = resolver_registry.get(prefix)

            if resolver:
                try:
                    # Resolve the placeholder
                    value = await resolver.resolve(path, resolver_context)

                    if value is not None:
                        # Replace the placeholder with the value
                        resolved_prompt = resolved_prompt.replace(
                            f"{{{{{placeholder}}}}}", str(value)
                        )
                        logger.debug(f"Resolved {{{{{placeholder}}}}}: {value}")
                    else:
                        logger.warning(
                            f"Placeholder {{{{{placeholder}}}}} resolved to None, leaving as-is"
                        )
                except Exception as e:
                    logger.error(
                        f"Error resolving placeholder {{{{{placeholder}}}}}: {e}",
                        exc_info=True,
                    )
            else:
                # Try simple replacement from trigger_event_data for backwards compatibility
                value = self._simple_resolve(placeholder, self.trigger_event_data)
                if value is not None:
                    resolved_prompt = resolved_prompt.replace(
                        f"{{{{{placeholder}}}}}", str(value)
                    )
                    logger.debug(f"Simple resolved {{{{{placeholder}}}}}: {value}")
                else:
                    logger.warning(
                        f"No resolver found for prefix '{prefix}' and simple resolution failed for {{{{{placeholder}}}}}"
                    )

        # Append the success sentinel instruction so the agent can signal completion
        resolved_prompt = resolved_prompt + FLOW_SUCCESS_INSTRUCTION

        logger.info("Prompt resolution complete")
        return resolved_prompt

    def _ensure_resolvers_registered(self):
        """Ensure all built-in resolvers are registered."""
        # Register built-in resolvers if not already registered
        if not resolver_registry.get("trigger_event"):
            resolver_registry.register(TriggerEventResolver())
        if not resolver_registry.get("project"):
            resolver_registry.register(ProjectResolver())
        if not resolver_registry.get("account"):
            resolver_registry.register(AccountResolver())

    def _sync_runtime_session(
        self,
        *,
        session_reference: Optional[str] = None,
        ended_at: Optional[datetime] = None,
    ) -> Optional[RuntimeSession]:
        """Create or update the shared runtime session for this flow execution."""
        if not self.flow or not self.execution_log or not self.flow.account_id:
            return None

        now = datetime.now(timezone.utc)
        execution_started_at = getattr(self.execution_log, "start_time", None) or now
        previous_runtime_session = crud_runtime_session.get_by_source(
            self.db,
            account_id=self.flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(self.execution_log.id),
        )
        self.runtime_session = crud_runtime_session.upsert_by_source(
            self.db,
            account_id=self.flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(self.execution_log.id),
            session_reference=session_reference,
            runtime_principal_type="flow_execution",
            runtime_principal_id=str(self.execution_log.id),
            runtime_principal_name=self.flow.name,
            started_at=execution_started_at,
            last_activity_at=ended_at or now,
            ended_at=ended_at,
        )
        self.db.commit()
        self.db.refresh(self.runtime_session)

        event_type = None
        if previous_runtime_session is None:
            event_type = "created"
        elif ended_at is not None and previous_runtime_session.ended_at != ended_at:
            event_type = "ended"
        elif (
            session_reference is not None
            and previous_runtime_session.session_reference != session_reference
        ):
            event_type = "updated"

        if event_type:
            try:
                from preloop.plugins.base import get_plugin_manager

                plugin_manager = get_plugin_manager()
                audit_service = plugin_manager.get_service("audit_service")
                if audit_service:
                    audit_service.log_runtime_session_event(
                        db=self.db,
                        account_id=self.flow.account_id,
                        runtime_session_id=self.runtime_session.id,
                        event=event_type,
                        session_source_type=self.runtime_session.session_source_type,
                        session_source_id=self.runtime_session.session_source_id,
                        session_reference=self.runtime_session.session_reference,
                        runtime_principal_type=self.runtime_session.runtime_principal_type,
                        runtime_principal_id=self.runtime_session.runtime_principal_id,
                        runtime_principal_name=self.runtime_session.runtime_principal_name,
                        flow_execution_id=self.execution_log.id,
                    )
            except Exception:
                logger.debug("Failed to audit runtime session lifecycle", exc_info=True)
            emit_account_event(
                build_account_event(
                    account_id=str(self.flow.account_id),
                    topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                    event_type=f"runtime_session_{event_type}",
                    payload={
                        "runtime_session_id": str(self.runtime_session.id),
                        "session_source_type": self.runtime_session.session_source_type,
                        "session_source_id": self.runtime_session.session_source_id,
                        "session_reference": self.runtime_session.session_reference,
                        "runtime_principal_type": self.runtime_session.runtime_principal_type,
                        "runtime_principal_id": self.runtime_session.runtime_principal_id,
                        "runtime_principal_name": self.runtime_session.runtime_principal_name,
                        "started_at": self.runtime_session.started_at.isoformat()
                        if self.runtime_session.started_at
                        else None,
                        "last_activity_at": self.runtime_session.last_activity_at.isoformat()
                        if self.runtime_session.last_activity_at
                        else None,
                        "ended_at": self.runtime_session.ended_at.isoformat()
                        if self.runtime_session.ended_at
                        else None,
                    },
                    runtime_session_id=str(self.runtime_session.id),
                    flow_id=str(self.flow.id),
                    execution_id=str(self.execution_log.id),
                )
            )
            emit_account_event(
                build_account_event(
                    account_id=str(self.flow.account_id),
                    topic=ACCOUNT_TOPIC_AUDIT,
                    event_type="audit_event",
                    payload={
                        "action": f"runtime_session_{event_type}",
                        "runtime_session_id": str(self.runtime_session.id),
                        "session_source_type": self.runtime_session.session_source_type,
                        "session_source_id": self.runtime_session.session_source_id,
                        "session_reference": self.runtime_session.session_reference,
                        "runtime_principal_type": self.runtime_session.runtime_principal_type,
                        "runtime_principal_id": self.runtime_session.runtime_principal_id,
                        "runtime_principal_name": self.runtime_session.runtime_principal_name,
                        "flow_execution_id": str(self.execution_log.id),
                        "flow_id": str(self.flow.id),
                    },
                    runtime_session_id=str(self.runtime_session.id),
                    flow_id=str(self.flow.id),
                    execution_id=str(self.execution_log.id),
                )
            )
        return self.runtime_session

    def _create_temporary_api_token(self) -> tuple[Optional[str], Optional[uuid.UUID]]:
        """
        Create a temporary API token for this flow execution.

        Returns:
            Tuple of (token_key, token_id) or (None, None) if creation failed
        """
        from datetime import timedelta
        from preloop.models.crud import crud_user

        try:
            account = crud_account.get(self.db, id=self.flow.account_id)

            if not account:
                logger.warning(f"Account {self.flow.account_id} not found")
                return None, None

            principal_user = None
            if account.primary_user_id:
                principal_user = crud_user.get(self.db, id=account.primary_user_id)
                if principal_user and not principal_user.is_active:
                    principal_user = None  # Fall back to other active users

            if not principal_user:
                # Fall back to the first available active user for older accounts that
                # do not have `primary_user_id` populated yet, or if primary is inactive.
                users = crud_user.get_by_account(
                    self.db, account_id=self.flow.account_id
                )
                active_users = [u for u in users if u.is_active]
                if active_users:
                    principal_user = active_users[0]

            if not principal_user:
                logger.warning(
                    f"No active users found for account {self.flow.account_id}, "
                    f"cannot create API token"
                )
                return None, None

            # Create API key that expires in 2 hours
            expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
            runtime_session = self._sync_runtime_session()

            # Store flow execution context in the token for tool filtering
            context_data = {
                "flow_execution_id": str(self.execution_log.id)
                if self.execution_log
                else None,
                "runtime_session_id": (
                    str(runtime_session.id) if runtime_session is not None else None
                ),
                "flow_id": str(self.flow_id),
                "allowed_mcp_tools": self.flow.allowed_mcp_tools or [],
                "allowed_mcp_servers": self.flow.allowed_mcp_servers or [],
                "runtime_principal": {
                    "type": "flow_execution",
                    "id": str(self.execution_log.id) if self.execution_log else None,
                    "name": self.flow.name,
                    "user_id": str(principal_user.id),
                    "username": principal_user.username,
                },
            }

            api_key, token_key = crud_api_key.create_runtime_key(
                self.db,
                name=f"Flow Execution {self.execution_log.id if self.execution_log else 'temp'}",
                account_id=self.flow.account_id,
                user_id=principal_user.id,
                expires_at=expires_at,
                scopes=["mcp:read", "mcp:write"],
                context_data=context_data,
            )

            logger.info(
                "Created temporary API key record id=%s for flow execution %s "
                "(principal_user=%s), expires at %s",
                api_key.id,
                self.execution_log.id if self.execution_log else None,
                principal_user.username,
                expires_at,
            )

            return token_key, api_key.id

        except Exception as e:
            logger.error(
                "Failed to create temporary API key record: %s",
                type(e).__name__,
                exc_info=True,
            )
            self.db.rollback()
            return None, None

    def _cleanup_temporary_api_token(self):
        """Deactivate the temporary API token created for this flow execution."""
        if not self.temporary_api_key_id:
            return

        try:
            api_key = crud_api_key.deactivate(self.db, key_id=self.temporary_api_key_id)

            if api_key:
                # Log outcome only — key ids are treated as sensitive by CodeQL.
                logger.info("Deactivated temporary API key record")
            else:
                logger.warning("Temporary API key record not found for cleanup")

        except Exception as e:
            logger.error(
                "Failed to cleanup temporary API key record: %s",
                type(e).__name__,
                exc_info=True,
            )
            self.db.rollback()

    def _simple_resolve(self, placeholder: str, data: Dict[str, Any]) -> Optional[str]:
        """
        Simple fallback resolver for backwards compatibility.

        Args:
            placeholder: Placeholder string (e.g., "payload.issue.title")
            data: Dictionary to resolve from

        Returns:
            Resolved value or None
        """
        keys = placeholder.split(".")
        value = data

        try:
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return None

            return str(value) if value is not None else None
        except Exception:
            return None

    async def _perform_git_clone(self, work_dir: str) -> Optional[str]:
        """
        Perform git clone operation if configured.

        Args:
            work_dir: Working directory where the clone should happen

        Returns:
            Path to cloned repository or None if not configured/failed
        """
        if not self.flow.git_clone_config:
            logger.debug("Git clone not configured for this flow")
            return None

        git_config = self.flow.git_clone_config
        if not git_config.get("enabled", False):
            logger.debug("Git clone is disabled")
            return None

        logger.info("Performing git clone operation")

        try:
            # Get repository URL
            repo_url = git_config.get("repository_url")
            if not repo_url:
                # Try to get from trigger event (GitHub/GitLab)
                repo_url = self._resolve_repository_url_from_trigger()

            if not repo_url:
                logger.error("No repository URL configured or found in trigger event")
                return None

            # Get clone path
            clone_path = git_config.get("clone_path", "./workspace")
            full_clone_path = f"{work_dir}/{clone_path}"

            # Get branch from config or trigger event. When a commit SHA is available,
            # clone the MR/PR target branch — the source ref may not exist yet.
            branch = git_config.get("branch")
            commit_sha = self._extract_commit_sha()
            if not branch:
                if commit_sha:
                    branch = self._extract_pr_target_branch_from_trigger() or "main"
                    logger.info(
                        "Commit SHA %s available; cloning branch '%s' "
                        "instead of source branch",
                        commit_sha[:8],
                        branch,
                    )
                else:
                    branch = self._extract_pr_branch_from_trigger()

            branch_arg = f" -b {branch}" if branch else ""

            # Prepare git clone command
            use_tracker_creds = git_config.get("use_tracker_credentials", True)
            if use_tracker_creds:
                # Get tracker credentials from trigger event
                credentials = await self._get_tracker_credentials()
                if credentials:
                    # Inject credentials into URL
                    repo_url = self._inject_credentials_into_url(repo_url, credentials)

            clone_cmd = (
                f"git clone --recursive{branch_arg} {repo_url} {full_clone_path}"
            )

            logger.info(f"Executing git clone to {full_clone_path}")

            # Execute git clone
            process = await asyncio.create_subprocess_shell(
                clone_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(
                    f"Git clone failed with code {process.returncode}: {stderr.decode()}"
                )
                return None

            logger.info(f"Git clone successful: {stdout.decode()}")

            # Checkout the specific commit SHA from trigger event if available
            # This ensures we're reviewing the exact code from the PR/push event
            if commit_sha:
                logger.info(
                    f"Checking out specific commit SHA from trigger event: {commit_sha[:8]}"
                )
                checkout_process = await asyncio.create_subprocess_shell(
                    f"git checkout {commit_sha}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=full_clone_path,
                )
                checkout_stdout, checkout_stderr = await checkout_process.communicate()

                if checkout_process.returncode != 0:
                    # If checkout fails, try fetching commit, source branch, then MR ref
                    logger.warning(
                        f"Direct checkout failed, trying fetch first: {checkout_stderr.decode()}"
                    )
                    source_branch = self._extract_pr_branch_from_trigger()
                    mr_ref = self._extract_merge_request_ref_from_trigger()
                    fetch_cmds = [f"git fetch origin {commit_sha}"]
                    if source_branch:
                        fetch_cmds.append(
                            f"git fetch origin {source_branch}:preloop-source-head"
                        )
                    if mr_ref:
                        fetch_cmds.append(f"git fetch origin {mr_ref}:preloop-mr-head")
                    for fetch_cmd in fetch_cmds:
                        fetch_process = await asyncio.create_subprocess_shell(
                            fetch_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=full_clone_path,
                        )
                        await fetch_process.communicate()

                    checkout_process = await asyncio.create_subprocess_shell(
                        f"git checkout {commit_sha}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=full_clone_path,
                    )
                    (
                        checkout_stdout,
                        checkout_stderr,
                    ) = await checkout_process.communicate()

                    if checkout_process.returncode != 0:
                        logger.error(
                            f"Failed to checkout commit {commit_sha[:8]}: {checkout_stderr.decode()}"
                        )
                    else:
                        logger.info(
                            f"Successfully checked out commit {commit_sha[:8]} after fetch"
                        )
                else:
                    logger.info(f"Successfully checked out commit {commit_sha[:8]}")
            else:
                logger.debug(
                    "No commit SHA in trigger event - using default branch HEAD"
                )

            return full_clone_path

        except Exception as e:
            logger.error(f"Error during git clone: {e}", exc_info=True)
            return None

    def _extract_merge_request_ref_from_trigger(self) -> Optional[str]:
        """Extract a git fetch ref for the MR/PR head commit."""
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            object_attrs = payload.get("object_attributes", {})
            if object_attrs and object_attrs.get("iid") is not None:
                return f"refs/merge-requests/{object_attrs['iid']}/head"

            if "pull_request" in payload:
                pr = payload["pull_request"]
                if pr.get("number") is not None:
                    return f"pull/{pr['number']}/head"

            return None
        except Exception as e:
            logger.debug(f"Error extracting merge request ref: {e}")
            return None

    def _extract_pr_target_branch_from_trigger(self) -> Optional[str]:
        """Extract the PR/MR target/base branch name from the trigger event."""
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            if "pull_request" in payload:
                pr = payload["pull_request"]
                if "base" in pr and "ref" in pr["base"]:
                    branch = pr["base"]["ref"]
                    logger.debug(f"Extracted PR base branch: {branch}")
                    return branch

            object_attrs = payload.get("object_attributes", {})
            if object_attrs and "target_branch" in object_attrs:
                branch = object_attrs["target_branch"]
                logger.debug(f"Extracted MR target branch: {branch}")
                return branch

            project = payload.get("project")
            if isinstance(project, dict) and project.get("default_branch"):
                return project["default_branch"]

            return None
        except Exception as e:
            logger.debug(f"Error extracting PR target branch: {e}")
            return None

    def _extract_pr_branch_from_trigger(self) -> Optional[str]:
        """Extract the PR/MR source branch name from the trigger event.

        For pull requests, we want to clone the head/source branch so we have
        all the commits from the PR available for checkout.
        """
        try:
            payload = self.trigger_event_data.get("payload", {})

            if not isinstance(payload, dict):
                return None

            # GitHub PR - get the head branch (source branch of PR)
            if "pull_request" in payload:
                pr = payload["pull_request"]
                if "head" in pr and "ref" in pr["head"]:
                    branch = pr["head"]["ref"]
                    logger.debug(f"Extracted PR head branch: {branch}")
                    return branch

            # GitLab MR - get the source branch
            object_attrs = payload.get("object_attributes", {})
            if object_attrs and "source_branch" in object_attrs:
                branch = object_attrs["source_branch"]
                logger.debug(f"Extracted MR source branch: {branch}")
                return branch

            return None
        except Exception as e:
            logger.debug(f"Error extracting PR branch: {e}")
            return None

    def _resolve_trigger_project_id(self) -> Optional[str]:
        """Resolve the project that triggered this execution.

        Prefer the repository from the webhook payload (e.g. the MR's project)
        over the first entry in flow.trigger_project_ids, which may be a
        different repo when the flow watches multiple projects.
        """
        project_id = self.trigger_event_data.get("project_id")
        if project_id:
            return str(project_id)

        from preloop.services.flow_trigger_service import FlowTriggerService

        resolved = FlowTriggerService(self.db)._extract_project_id(
            self.trigger_event_data
        )
        if resolved:
            logger.info(f"Resolved trigger project from event payload: {resolved}")
            return resolved

        if self.flow.trigger_project_ids:
            fallback = str(self.flow.trigger_project_ids[0])
            logger.info(
                "No project in trigger event; using first flow trigger_project_id: "
                f"{fallback}"
            )
            return fallback

        return None

    def _resolve_repository_url_from_trigger(self) -> Optional[str]:
        """Extract repository URL from trigger event data."""
        try:
            # GitHub structure
            if "repository" in self.trigger_event_data:
                repo = self.trigger_event_data["repository"]
                if isinstance(repo, dict):
                    return repo.get("clone_url") or repo.get("html_url")

            # GitLab structure
            if "project" in self.trigger_event_data:
                project = self.trigger_event_data["project"]
                if isinstance(project, dict):
                    return project.get("http_url_to_repo") or project.get("web_url")

            return None
        except Exception as e:
            logger.error(f"Error extracting repository URL from trigger: {e}")
            return None

    async def _get_tracker_credentials(self) -> Optional[Dict[str, str]]:
        """Get tracker credentials from the database (deprecated - use _get_tracker_credentials_by_id)."""
        try:
            # Get tracker_id from trigger event or flow config
            tracker_id = self.trigger_event_data.get("tracker_id")
            if not tracker_id:
                logger.warning("No tracker_id in trigger event data")
                return None

            return await self._get_tracker_credentials_by_id(tracker_id)

        except Exception as e:
            logger.error(f"Error getting tracker credentials: {e}", exc_info=True)
            return None

    async def _get_tracker_credentials_by_id(
        self, tracker_id: str
    ) -> Optional[Dict[str, str]]:
        """Get tracker credentials by tracker ID."""
        try:
            from preloop.models.crud import crud_tracker

            tracker = crud_tracker.get(self.db, id=tracker_id)
            if not tracker:
                logger.warning(f"Tracker {tracker_id} not found")
                return None

            # Return credentials (api_key is encrypted in DB, should be decrypted here)
            return {
                "tracker_id": tracker_id,
                "token": tracker.resolved_api_key,
                "tracker_type": tracker.tracker_type,
            }

        except Exception as e:
            logger.error(
                f"Error getting tracker credentials for {tracker_id}: {e}",
                exc_info=True,
            )
            return None

    def _inject_credentials_into_url(
        self, repo_url: str, credentials: Dict[str, str]
    ) -> str:
        """Inject credentials into repository URL for authentication."""
        try:
            token = credentials.get("token")
            tracker_type = credentials.get("tracker_type")

            if not token:
                return repo_url

            host_kind = tracker_host_kind(repo_url)
            if host_kind in {"github", "gitlab"} or tracker_type in {
                "github",
                "gitlab",
            }:
                return inject_oauth_token(repo_url, token)

            # If we can't inject, return original URL
            logger.warning("Could not inject credentials into repository URL")
            return repo_url

        except Exception as e:
            logger.error(
                "Error injecting credentials: %s",
                type(e).__name__,
                exc_info=True,
            )
            return repo_url

    async def _execute_custom_commands(self, work_dir: str) -> bool:
        """
        Execute custom commands if configured (admin-only feature).

        Args:
            work_dir: Working directory where commands should run

        Returns:
            True if successful or not configured, False if failed
        """
        if not self.flow.custom_commands:
            logger.debug("Custom commands not configured for this flow")
            return True

        custom_cmds = self.flow.custom_commands
        if not custom_cmds.get("enabled", False):
            logger.debug("Custom commands are disabled")
            return True

        # Security check: Verify the flow was created by a superuser
        # This prevents non-admin users from executing arbitrary commands
        try:
            from preloop.models.crud import crud_user

            # Get all users from the account
            users = crud_user.get_by_account(self.db, account_id=self.flow.account_id)

            # Check if ANY user with owner role exists in this account
            # (Flow creation/update should have been blocked if user wasn't admin)
            has_admin = any(user.is_superuser for user in users)
            if not has_admin:
                logger.error(
                    "Custom commands configured but no admin users found for account. "
                    "This is a security violation - skipping custom commands."
                )
                return False

        except Exception as e:
            logger.error(f"Error verifying admin status: {e}", exc_info=True)
            return False

        commands = custom_cmds.get("commands", [])
        if not commands:
            logger.debug("No custom commands to execute")
            return True

        logger.info(f"Executing {len(commands)} custom command(s)")

        try:
            for idx, cmd in enumerate(commands):
                logger.info(
                    f"Executing custom command {idx + 1}/{len(commands)}: {cmd}"
                )

                process = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                )

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    logger.error(
                        f"Custom command failed with code {process.returncode}: {stderr.decode()}"
                    )
                    return False

                logger.info(f"Custom command output: {stdout.decode()}")

            logger.info("All custom commands executed successfully")
            return True

        except Exception as e:
            logger.error(f"Error executing custom commands: {e}", exc_info=True)
            return False

    async def _prepare_execution_context(self) -> Dict[str, Any]:
        """Prepare the full execution context for the agent."""
        logger.info(
            f"Preparing execution context for agent type: {self.flow.agent_type}"
        )

        resolved_prompt = await self._resolve_prompt()

        # Create short-lived API token for this flow execution
        account_api_token = None
        if self.flow.account_id:
            account_api_token, self.temporary_api_key_id = (
                self._create_temporary_api_token()
            )
            if not account_api_token:
                logger.warning(
                    "Could not create temporary API key record for account %s",
                    self.flow.account_id,
                )

        execution_context = {
            "flow_id": str(self.flow_id),
            "flow_name": self.flow.name,  # Used for generating git branch names
            "execution_id": str(self.execution_log.id),
            "prompt": resolved_prompt,
            "agent_type": self.flow.agent_type,
            "agent_config": self.flow.agent_config,
            "allowed_mcp_servers": self.flow.allowed_mcp_servers,
            "allowed_mcp_tools": self.flow.allowed_mcp_tools,
            "account_id": self.flow.account_id,
            "account_api_token": account_api_token,
            "git_clone_config": self.flow.git_clone_config,
            "custom_commands": self.flow.custom_commands,
            "trigger_event_data": self.trigger_event_data,
            "trigger_project_ids": [str(pid) for pid in self.flow.trigger_project_ids]
            if self.flow.trigger_project_ids
            else None,  # For git clone fallback
            # Singular form used by container.py for git clone and credential lookup
            "trigger_project_id": self._resolve_trigger_project_id(),
        }

        # Prepare git credentials if repositories are configured
        if self.flow.git_clone_config:
            repositories = self.flow.git_clone_config.get("repositories", [])
            if repositories:
                logger.info(
                    f"Preparing git credentials for {len(repositories)} configured repositories"
                )
                # Get unique tracker IDs from repositories
                tracker_ids = set(
                    repo.get("tracker_id")
                    for repo in repositories
                    if repo.get("tracker_id")
                )

                # Fetch credentials for each tracker
                credentials_map = {}
                for tracker_id in tracker_ids:
                    creds = await self._get_tracker_credentials_by_id(tracker_id)
                    if creds:
                        credentials_map[tracker_id] = creds

                if credentials_map:
                    execution_context["git_credentials_map"] = credentials_map
                    logger.info(
                        f"Prepared git credentials for {len(credentials_map)} tracker(s)"
                    )
                else:
                    logger.warning(
                        "Git clone enabled but could not get tracker credentials"
                    )

        # Add AI model details if available
        if self.ai_model:
            logger.info(
                f"AI model loaded: id={self.ai_model.id}, "
                f"identifier={self.ai_model.model_identifier}, "
                f"provider={self.ai_model.provider_name}"
            )
            resolved_model_runtime = self._resolve_execution_model_runtime()
            execution_context.update(
                resolved_model_runtime.to_execution_context(
                    gateway_token=account_api_token
                    if resolved_model_runtime.model_gateway_enabled
                    else None
                )
            )
        else:
            logger.warning(
                f"No AI model configured for flow {self.flow.id}, "
                f"ai_model_id={self.flow.ai_model_id if hasattr(self.flow, 'ai_model_id') else 'N/A'}, "
                "agent will need to use defaults"
            )

        logger.info("Execution context prepared successfully")
        return execution_context

    async def _stream_logs_to_nats(self, agent_executor, session_reference: str):
        """
        Background task to stream agent logs to NATS in real-time.

        Args:
            agent_executor: Agent executor instance
            session_reference: Container/Job reference
        """
        logger.info(f"Starting log streaming for {session_reference}")
        log_count = 0

        # Track previous line for token parsing (tokens used pattern spans 2 lines)
        previous_line = ""

        try:
            async for log_line in agent_executor.stream_logs(session_reference):
                log_count += 1
                logger.debug(f"Streamed log line #{log_count}: {log_line[:100]}")

                # Store the log line for later summary
                self.execution_logger.log_agent_output(log_line)

                # Track the agent exec start marker — sentinel detection is
                # suppressed until this marker is seen, preventing false
                # positives from the prompt echo that contains the sentinel
                # instruction text.
                if (
                    not self._agent_exec_started
                    and log_line.strip() == AGENT_EXEC_START_MARKER
                ):
                    self._agent_exec_started = True
                    logger.info(
                        f"Agent exec start marker seen at log line #{log_count}"
                    )

                # Detect success sentinel — but ONLY after the agent exec
                # start marker has been seen (to ignore prompt echo).
                stripped_line = log_line.strip()
                if stripped_line == FLOW_SUCCESS_SENTINEL:
                    if not self._agent_exec_started:
                        logger.warning(
                            f"[Sentinel] Ignoring sentinel match at line #{log_count} "
                            f"— agent exec start marker not yet seen (prompt echo?). "
                            f"Previous line: {previous_line[:120]!r}"
                        )
                    elif self._success_sentinel_seen.is_set():
                        logger.warning(
                            f"[Sentinel] Duplicate sentinel match at line #{log_count} "
                            f"— already triggered. Previous line: {previous_line[:120]!r}"
                        )
                    else:
                        logger.info(
                            f"[Sentinel] Success sentinel detected for {session_reference} "
                            f"at line #{log_count}. "
                            f"Previous line: {previous_line[:120]!r}"
                        )
                        self._success_sentinel_seen.set()

                previous_tool_calls_count = len(self.execution_logger.mcp_usage_logs)

                # Parse log line for structured data (includes tool call detection)
                self.execution_logger.parse_agent_logs([log_line])

                # Check for token usage pattern: "tokens used" followed by number on next line
                if "tokens used" in previous_line.lower():
                    # Try to extract token count from current line
                    # Pattern: number with optional commas (e.g., "1,234" or "1234")
                    token_match = re.search(r"(\d{1,3}(?:,\d{3})*)", log_line.strip())
                    if token_match:
                        tokens = int(token_match.group(1).replace(",", ""))
                        self.total_tokens += tokens

                        logger.info(
                            "Detected token usage: %s tokens (total: %s). "
                            "Live cost remains unset until provider pricing is known.",
                            tokens,
                            self.total_tokens,
                        )

                        # Emit token usage update
                        await self._publish_update(
                            "token_usage_update",
                            {
                                "total_tokens": self.total_tokens,
                                "pricing_available": False,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        await self._persist_live_metrics()

                # Check if this log line indicates a tool call was detected
                updated_tool_calls_count = len(self.execution_logger.mcp_usage_logs)
                if updated_tool_calls_count > self.tool_calls_count:
                    new_tool_entries = self.execution_logger.mcp_usage_logs[
                        previous_tool_calls_count:updated_tool_calls_count
                    ]
                    self.tool_calls_count = updated_tool_calls_count
                    logger.info(f"Tool call detected (total: {self.tool_calls_count})")

                    for tool_entry in new_tool_entries:
                        await self._publish_update(
                            "mcp_call",
                            {
                                **tool_entry,
                                "timestamp": tool_entry.get("timestamp")
                                or datetime.now(timezone.utc).isoformat(),
                            },
                        )

                    # Emit tool call count update
                    await self._publish_update(
                        "tool_calls_update",
                        {
                            "tool_calls": self.tool_calls_count,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    await self._persist_live_metrics()

                # Publish log line to NATS
                await self._publish_update(
                    "agent_log_line",
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "line": log_line,
                    },
                )

                # Update previous line for next iteration
                previous_line = log_line

            logger.info(
                f"Log streaming completed. Total logs streamed: {log_count}, tokens: {self.total_tokens}, tool calls: {self.tool_calls_count}"
            )

        except asyncio.CancelledError:
            logger.info(f"Log streaming cancelled for {session_reference}")
        except Exception as e:
            logger.error(
                f"Error streaming logs for {session_reference}: {e}", exc_info=True
            )
            await self._publish_update(
                "agent_log_error", {"error": f"Log streaming error: {str(e)}"}
            )

    async def _persist_live_metrics(self):
        """Persist live execution counters so reloads can rehydrate them."""
        if not self.execution_log:
            return

        self.execution_log.tool_calls_count = self.tool_calls_count
        self.execution_log.total_tokens = self.total_tokens
        self.execution_log.estimated_cost = self.estimated_cost
        self.execution_log.mcp_usage_logs = self.execution_logger.get_mcp_usage_logs()
        self.db.add(self.execution_log)
        self.db.commit()
        self.db.refresh(self.execution_log)

    def _get_runtime_tool_activity_count(self) -> int:
        """Return the persisted tool-call count for this execution."""
        if not self.execution_log:
            return self.tool_calls_count

        from preloop.models.crud import crud_runtime_session_activity

        return crud_runtime_session_activity.get_tool_call_count_by_flow_execution(
            self.db, flow_execution_id=self.execution_log.id
        )

    def _get_recent_runtime_tool_activity_signatures(
        self, limit: int = 12
    ) -> list[str]:
        """Return recent persisted tool-call signatures for loop detection."""
        if not self.execution_log:
            return []

        from preloop.models.crud import crud_runtime_session_activity

        activities = crud_runtime_session_activity.get_recent_successful_tool_calls_by_flow_execution(
            self.db, flow_execution_id=self.execution_log.id, limit=limit
        )

        signatures: list[str] = []
        timestamps: list[datetime] = []
        for activity in reversed(activities):
            metadata = activity.metadata_ or {}
            signatures.append(
                json.dumps(
                    {
                        "server_name": activity.server_name,
                        "tool_name": activity.tool_name,
                        "arguments": metadata.get("arguments"),
                    },
                    sort_keys=True,
                    default=str,
                )
            )
            timestamps.append(activity.timestamp)
        return self._dedupe_rapid_duplicate_signatures(signatures, timestamps)

    @staticmethod
    def _dedupe_rapid_duplicate_signatures(
        signatures: list[str],
        timestamps: list[datetime],
        *,
        max_delta_seconds: float = MCP_TOOL_LOOP_DUPLICATE_WINDOW_SECONDS,
    ) -> list[str]:
        """Drop paired duplicate signatures that arrive within a short window."""
        if not signatures:
            return []

        deduped_signatures: list[str] = [signatures[0]]
        last_kept_timestamp = timestamps[0] if timestamps else None
        for signature, timestamp in zip(signatures[1:], timestamps[1:], strict=False):
            if (
                signature == deduped_signatures[-1]
                and last_kept_timestamp is not None
                and timestamp is not None
                and abs((timestamp - last_kept_timestamp).total_seconds())
                <= max_delta_seconds
            ):
                continue
            deduped_signatures.append(signature)
            last_kept_timestamp = timestamp
        return deduped_signatures

    @staticmethod
    def _detect_repeated_tool_cycle(signatures: list[str]) -> Optional[Dict[str, Any]]:
        """Detect tight loops where the same tool+arguments repeat without progress.

        Legitimate flows (for example PR review) may call the same tool name several
        times with different arguments. Only identical consecutive signatures count
        toward a loop after rapid duplicate invocations are deduplicated.
        """
        if len(signatures) < MCP_TOOL_LOOP_MIN_REPETITIONS:
            return None

        for pattern_length in range(1, MCP_TOOL_LOOP_PATTERN_MAX_LENGTH + 1):
            repetitions = (
                MCP_TOOL_LOOP_SINGLE_CALL_REPETITIONS
                if pattern_length == 1
                else MCP_TOOL_LOOP_MIN_REPETITIONS
            )
            window_size = pattern_length * repetitions
            if len(signatures) < window_size:
                continue

            tail = signatures[-window_size:]
            pattern = tail[:pattern_length]
            if all(
                tail[index * pattern_length : (index + 1) * pattern_length] == pattern
                for index in range(repetitions)
            ):
                decoded_pattern = [json.loads(item) for item in pattern]
                return {
                    "pattern_length": pattern_length,
                    "repetitions": repetitions,
                    "pattern": decoded_pattern,
                }

        return None

    async def _sync_runtime_tool_activity_metrics(self) -> Optional[Dict[str, Any]]:
        """Sync persisted MCP activity into live metrics and detect tight loops."""
        persisted_tool_calls = self._get_runtime_tool_activity_count()
        if persisted_tool_calls > self.tool_calls_count:
            self.tool_calls_count = persisted_tool_calls
            logger.info(
                f"Persisted tool call count detected (total: {self.tool_calls_count})"
            )
            await self._publish_update(
                "tool_calls_update",
                {
                    "tool_calls": self.tool_calls_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await self._persist_live_metrics()

        recent_signatures = self._get_recent_runtime_tool_activity_signatures()
        return self._detect_repeated_tool_cycle(recent_signatures)

    async def _listen_for_commands(self):
        """
        Subscribe to NATS commands for user intervention.

        Listens on subject: flow-commands.{execution_id}
        """
        if not self.nats_client or not self.nats_client.is_connected:
            logger.warning("NATS not connected, cannot listen for commands")
            return

        command_subject = f"flow-commands.{self.execution_log.id}"

        try:

            async def command_handler(msg):
                try:
                    command_data = json.loads(msg.data.decode())
                    command_type = command_data.get("command")

                    logger.info(
                        f"Received command: {command_type} for execution {self.execution_log.id}"
                    )

                    if command_type == "stop":
                        logger.info("User requested stop")
                        self._stop_requested.set()
                    elif command_type == "send_message":
                        message = command_data.get("message", "")
                        logger.info(f"User sent message: {message}")
                        await self._user_messages.put(message)
                    elif command_type == "pause":
                        logger.info("User requested pause (not yet implemented)")
                        # TODO: Implement pause functionality
                    else:
                        logger.warning(f"Unknown command type: {command_type}")

                except Exception as e:
                    logger.error(f"Error handling command: {e}", exc_info=True)

            # Subscribe to commands
            self._command_subscription = await self.nats_client.subscribe(
                command_subject, cb=command_handler
            )
            logger.info(f"Listening for commands on {command_subject}")

        except Exception as e:
            logger.error(f"Failed to setup command subscription: {e}", exc_info=True)

    async def _cleanup_monitoring(self):
        """Cleanup monitoring resources (log streaming, command subscription)."""
        # Wait for log streaming task to complete naturally with a timeout
        # This ensures buffered logs are fully streamed before cleanup
        if self._log_streaming_task and not self._log_streaming_task.done():
            try:
                # Give the log streaming task time to finish naturally
                # (container may have buffered logs to yield)
                await asyncio.wait_for(self._log_streaming_task, timeout=30.0)
                logger.info("Log streaming task completed successfully")
            except asyncio.TimeoutError:
                logger.warning(
                    "Log streaming task did not complete within timeout, cancelling"
                )
                self._log_streaming_task.cancel()
                try:
                    await self._log_streaming_task
                except asyncio.CancelledError:
                    # Expected after cancelling the log streaming task on timeout.
                    pass
            except asyncio.CancelledError:
                # Task was cancelled while awaiting completion during cleanup.
                pass
            except Exception as e:
                logger.warning(f"Error waiting for log streaming task: {e}")

        # Unsubscribe from commands
        if self._command_subscription:
            try:
                await self._command_subscription.unsubscribe()
            except Exception as e:
                logger.error(f"Error unsubscribing from commands: {e}")

    async def _start_agent_session(
        self, execution_context: Dict[str, Any]
    ) -> tuple[str, Any]:
        """
        Launch an agent session via Agent Execution Infrastructure.

        Args:
            execution_context: Context for agent execution

        Returns:
            Tuple of (agent_session_reference, agent_executor)
            - agent_session_reference: Reference to the agent session (container ID, job ID, etc.)
            - agent_executor: The agent executor instance (caller must clean up)
        """
        agent_type = execution_context["agent_type"]
        agent_config = execution_context["agent_config"]

        logger.info(f"Starting {agent_type} agent session")

        agent_executor = None
        try:
            # Create agent executor using factory
            agent_executor = create_agent_executor(agent_type, agent_config)

            # Start the agent
            session_reference = await agent_executor.start(execution_context)

            logger.info(f"Agent session started: {session_reference}")
            # Return both session reference and executor (caller is responsible for cleanup)
            return session_reference, agent_executor

        except Exception as e:
            logger.error(f"Failed to start {agent_type} agent: {e}", exc_info=True)
            # Cleanup agent executor on failure
            if agent_executor:
                try:
                    await agent_executor.cleanup()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Error during agent cleanup after failure: {cleanup_error}"
                    )
            raise

    async def _monitor_agent_execution(
        self, session_reference: str, agent_executor: Any
    ) -> Dict[str, Any]:
        """
        Monitor agent execution until completion with real-time log streaming.

        Args:
            session_reference: Reference to the agent session
            agent_executor: Agent executor instance to use for monitoring

        Returns:
            Dict with execution results including status, output, errors
        """
        logger.info(f"Monitoring agent execution {session_reference}")
        self.execution_logger.log_milestone(
            "agent_monitoring_started", {"session_reference": session_reference}
        )

        try:
            from preloop.config import settings

            # Start listening for user commands
            await self._listen_for_commands()

            # Start background task for log streaming
            self._log_streaming_task = asyncio.create_task(
                self._stream_logs_to_nats(agent_executor, session_reference)
            )

            # Poll agent status until completion
            max_wait_time = max(30, int(settings.flow_execution_max_wait_seconds))
            poll_interval = 5  # Check status every 5 seconds
            elapsed = 0
            consecutive_failures = 0
            max_consecutive_failures = (
                3  # Fail after 3 consecutive status check failures
            )
            # Grace period after success sentinel is seen in logs.
            # The agent may have finished but post-exec commands (git push,
            # PR creation) keep the container alive.  Once the sentinel
            # appears we give at most this many extra seconds before
            # treating the execution as succeeded.
            post_sentinel_grace = 120  # seconds
            sentinel_seen_at: Optional[float] = None
            last_heartbeat_at = -30  # force first heartbeat near start
            heartbeat_interval = 30

            while elapsed < max_wait_time:
                if (
                    self._orchestrator_worker_id
                    and self.execution_log is not None
                    and elapsed - last_heartbeat_at >= heartbeat_interval
                ):
                    try:
                        crud_flow_execution.touch_heartbeat(
                            self.db,
                            execution_id=self.execution_log.id,
                            worker_id=self._orchestrator_worker_id,
                        )
                        last_heartbeat_at = elapsed
                    except Exception as heartbeat_error:
                        logger.warning(
                            "Failed to touch orchestrator heartbeat for %s: %s",
                            self.execution_log.id,
                            heartbeat_error,
                        )

                # Check if user requested stop
                if self._stop_requested.is_set():
                    logger.info(
                        f"User requested stop for execution {self.execution_log.id}"
                    )
                    await agent_executor.stop(session_reference)
                    await self._publish_update("user_stopped", {"elapsed": elapsed})
                    break

                # Get status with error handling
                try:
                    status = await agent_executor.get_status(session_reference)
                    logger.debug(f"Agent status at {elapsed}s: {status.value}")
                    consecutive_failures = 0  # Reset failure counter on success
                except Exception as status_error:
                    status_error_message = _exception_message(status_error)
                    logger.error(
                        f"Error getting agent status at {elapsed}s: {status_error_message}",
                        exc_info=True,
                    )
                    # Retry once after a short delay
                    await asyncio.sleep(2)
                    try:
                        status = await agent_executor.get_status(session_reference)
                        logger.info(f"Status check recovered: {status.value}")
                        consecutive_failures = 0  # Reset on successful retry
                    except Exception as retry_error:
                        retry_error_message = _exception_message(retry_error)
                        logger.error(
                            f"Status check retry failed: {retry_error_message}",
                            exc_info=True,
                        )
                        consecutive_failures += 1

                        # Fail execution if too many consecutive failures
                        if consecutive_failures >= max_consecutive_failures:
                            logger.error(
                                f"Agent monitoring failed after {consecutive_failures} consecutive failures"
                            )
                            self.execution_logger.log_milestone(
                                "agent_monitoring_failed",
                                {"consecutive_failures": consecutive_failures},
                            )
                            return {
                                "status": "FAILED",
                                "error_message": f"Monitoring error: {retry_error_message}",
                                "actions_taken": self.execution_logger.get_actions_taken(),
                                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                            }

                        # Continue polling for transient errors
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval
                        continue

                # Publish status update (best effort - don't fail if NATS is down)
                try:
                    await self._publish_update(
                        "agent_status", {"status": status.value, "elapsed": elapsed}
                    )
                except Exception as publish_error:
                    logger.warning(f"Failed to publish status update: {publish_error}")

                loop_detection = await self._sync_runtime_tool_activity_metrics()
                if loop_detection:
                    repeated_tools = ", ".join(
                        f"{item.get('server_name')}/{item.get('tool_name')}"
                        for item in loop_detection["pattern"]
                    )
                    error_message = (
                        "Execution stopped after detecting a repeated MCP tool loop: "
                        f"{repeated_tools} repeated "
                        f"{loop_detection['repetitions']} times with identical "
                        "arguments."
                    )
                    logger.warning(error_message)
                    self.execution_logger.log_milestone(
                        "mcp_tool_loop_detected",
                        {**loop_detection, "elapsed": elapsed},
                    )
                    await self._publish_update(
                        "agent_loop_detected",
                        {
                            "error": error_message,
                            "elapsed": elapsed,
                            **loop_detection,
                        },
                    )
                    await agent_executor.stop(session_reference)
                    return {
                        "status": "FAILED",
                        "error_message": error_message,
                        "actions_taken": self.execution_logger.get_actions_taken(),
                        "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                    }

                if status in (
                    AgentStatus.SUCCEEDED,
                    AgentStatus.FAILED,
                    AgentStatus.STOPPED,
                ):
                    # Agent finished, get final result
                    logger.info(
                        f"Agent finished with status {status.value} at {elapsed}s"
                    )
                    result = await agent_executor.get_result(session_reference)

                    self.execution_logger.log_milestone(
                        "agent_execution_completed",
                        {"status": status.value, "exit_code": result.exit_code},
                    )

                    # Sentinel-based status override:
                    # If the container reports SUCCEEDED (exit code 0) but the
                    # FLOW_EXECUTION_SUCCESS sentinel was NOT found in logs,
                    # treat it as FAILED.  This catches agents (e.g. OpenCode)
                    # that error out but still exit with code 0.
                    # Guard: only apply the override when the agent-exec-start
                    # marker was actually seen in logs.  If we never streamed
                    # real logs (e.g. mocks, or the log stream failed before
                    # any output), the sentinel's absence is not meaningful.
                    final_status = result.status.value
                    error_message = result.error_message

                    if (
                        result.status == AgentStatus.SUCCEEDED
                        and self._agent_exec_started
                        and not self._success_sentinel_seen.is_set()
                    ):
                        logger.warning(
                            f"Agent exited with SUCCEEDED status (exit_code={result.exit_code}) "
                            f"but success sentinel was NOT found in logs. "
                            f"Overriding status to FAILED."
                        )
                        self.execution_logger.log_milestone(
                            "sentinel_missing_override",
                            {
                                "original_status": result.status.value,
                                "exit_code": result.exit_code,
                            },
                        )
                        final_status = "FAILED"
                        error_message = (
                            result.error_message
                            or "Agent exited with code 0 but did not produce "
                            "the success sentinel — likely encountered an error."
                        )

                    return {
                        "status": final_status,
                        "output_summary": result.output_summary,
                        "error_message": error_message,
                        "actions_taken": self.execution_logger.get_actions_taken(),
                        "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                        "exit_code": result.exit_code,
                    }

                # Check if the success sentinel was seen in logs while
                # the container is still running (post-exec commands).
                # The sentinel is only armed after AGENT_EXEC_START_MARKER is
                # seen, so prompt echoes cannot trigger it.
                if self._success_sentinel_seen.is_set():
                    if sentinel_seen_at is None:
                        sentinel_seen_at = elapsed
                        logger.info(
                            f"Success sentinel seen at {elapsed}s, "
                            f"allowing {post_sentinel_grace}s grace period"
                        )
                    elif elapsed - sentinel_seen_at >= post_sentinel_grace:
                        logger.info(
                            f"Grace period expired ({post_sentinel_grace}s) "
                            f"after success sentinel — treating as SUCCEEDED"
                        )
                        self.execution_logger.log_milestone(
                            "sentinel_grace_period_expired",
                            {"sentinel_seen_at": sentinel_seen_at, "elapsed": elapsed},
                        )
                        return {
                            "status": "SUCCEEDED",
                            "output_summary": self.execution_logger.get_agent_output_summary(),
                            "error_message": None,
                            "actions_taken": self.execution_logger.get_actions_taken(),
                            "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
                        }

                # Wait before next poll
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            # Timeout reached
            logger.warning(
                f"Agent execution {session_reference} timed out after {max_wait_time}s"
            )
            self.execution_logger.log_milestone("agent_execution_timeout")
            await agent_executor.stop(session_reference)

            return {
                "status": "FAILED",
                "error_message": f"Execution timed out after {max_wait_time} seconds",
                "actions_taken": self.execution_logger.get_actions_taken(),
                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
            }

        except Exception as e:
            error_message = _exception_message(e)
            logger.error(
                f"Error monitoring agent execution {session_reference}: {error_message}",
                exc_info=True,
            )
            self.execution_logger.log_milestone(
                "agent_execution_error", {"error": error_message}
            )
            return {
                "status": "FAILED",
                "error_message": f"Monitoring error: {error_message}",
                "actions_taken": self.execution_logger.get_actions_taken(),
                "mcp_usage_logs": self.execution_logger.get_mcp_usage_logs(),
            }
        finally:
            # Always cleanup monitoring resources
            await self._cleanup_monitoring()
            # Cleanup agent executor resources (close Kubernetes/Docker clients)
            try:
                await agent_executor.cleanup()
            except Exception as cleanup_error:
                logger.warning(f"Error during agent cleanup: {cleanup_error}")

    def _create_execution_log(self):
        """Create an initial record in FlowExecutions.

        No-op when ``execution_log`` was pre-created (manual trigger / worker
        dispatch) so ``run()`` can be reused for both paths.
        """
        if self.execution_log is not None:
            logger.info("Using pre-created execution log: %s", self.execution_log.id)
            self._sync_runtime_session()
            return

        logger.info("Creating initial execution log")

        # Ensure trigger_event_data is JSON serializable (convert UUIDs, datetimes, etc.)
        serializable_event_data = _make_json_serializable(self.trigger_event_data)
        attach_trigger_subject(serializable_event_data)

        execution_create = schemas.FlowExecutionCreate(
            flow_id=self.flow_id,
            status="PENDING",
            trigger_event_details=serializable_event_data,
            trigger_event_id=self.trigger_event_data.get("event_id"),
        )

        db_execution_log = crud_flow_execution.create(self.db, obj_in=execution_create)
        self.db.commit()
        self.db.refresh(db_execution_log)
        self.execution_log = db_execution_log
        self._sync_runtime_session()

        logger.info(f"Execution log created with ID: {self.execution_log.id}")

    async def _update_execution_log(self, status: str, **kwargs):
        """Update the execution log and publish the update to NATS."""
        logger.info(f"Updating execution log to status: {status}")

        # Debug logging for metrics
        if "tool_calls_count" in kwargs or "total_tokens" in kwargs:
            logger.info(
                f"Updating execution metrics: tool_calls_count={kwargs.get('tool_calls_count')}, "
                f"total_tokens={kwargs.get('total_tokens')}, estimated_cost={kwargs.get('estimated_cost')}"
            )

        update_data = schemas.FlowExecutionUpdate(status=status, **kwargs)

        # Debug: Log what fields are actually in the update
        update_dict = update_data.model_dump(exclude_unset=True)
        logger.info(f"Update data fields: {list(update_dict.keys())}")
        if "tool_calls_count" in update_dict or "total_tokens" in update_dict:
            logger.info(
                f"Update dict metrics: tool_calls_count={update_dict.get('tool_calls_count')}, "
                f"total_tokens={update_dict.get('total_tokens')}, estimated_cost={update_dict.get('estimated_cost')}"
            )

        updated_log = crud_flow_execution.update(
            self.db, db_obj=self.execution_log, obj_in=update_data
        )
        self.db.commit()
        self.db.refresh(updated_log)
        self.execution_log = updated_log

        # Debug: Verify the values were actually set
        if "tool_calls_count" in kwargs or "total_tokens" in kwargs:
            logger.info(
                f"After update - DB values: tool_calls_count={updated_log.tool_calls_count}, "
                f"total_tokens={updated_log.total_tokens}, estimated_cost={updated_log.estimated_cost}"
            )

        # Publish update to NATS for real-time UI updates
        # Convert datetime objects to ISO format strings for JSON serialization
        serializable_kwargs = {}
        for key, value in kwargs.items():
            if isinstance(value, datetime):
                serializable_kwargs[key] = value.isoformat()
            else:
                serializable_kwargs[key] = value

        await self._publish_update(
            "status_update", {"status": status, **serializable_kwargs}
        )

        logger.debug(f"Execution log updated: status={status}")

    async def run(self):
        """
        Execute the flow through its full lifecycle.

        Lifecycle stages:
        1. PENDING: Execution log created
        2. INITIALIZING: Flow and AI model details retrieved
        3. RUNNING: Agent session started
        4. SUCCEEDED/FAILED: Execution completed
        """
        try:
            # Stage 1: Retrieve flow details first (needed for account_id in messages)
            self._get_flow_details()

            # Stage 2: Create execution log
            self._create_execution_log()

            # Publish execution_started event for UI notification
            # This allows the flow executions list to update automatically
            await self._publish_update(
                "execution_started",
                {
                    "status": "PENDING",
                    "flow_id": str(self.flow_id),
                    "flow_name": self.flow.name if self.flow else None,
                },
            )

            await self._publish_update("status_update", {"status": "PENDING"})
            logger.info(f"Flow execution started: {self.execution_log.id}")

            # Update commit status to pending (appears in GitHub/GitLab checks)
            await self._update_commit_status(
                state="pending",
                description=f"Preloop is reviewing: {self.flow.name}"
                if self.flow
                else "Preloop is reviewing",
            )

            # Stage 3: Mark as initializing
            await self._update_execution_log(status="INITIALIZING")

            # Stage 3: Prepare execution context
            execution_context = await self._prepare_execution_context()

            # Store resolved prompt for debugging/audit and mark as STARTING
            await self._update_execution_log(
                status="STARTING",
                resolved_input_prompt=execution_context["prompt"],
            )

            # Stage 4: Start agent session (returns both session reference and executor)
            session_reference, agent_executor = await self._start_agent_session(
                execution_context
            )

            # Agent started successfully - now mark as RUNNING with session reference
            await self._update_execution_log(
                status="RUNNING",
                agent_session_reference=session_reference,
            )
            self._sync_runtime_session(session_reference=session_reference)

            # Stage 5: Monitor agent execution and collect results
            # Pass the executor so we don't create a duplicate instance
            agent_result = await self._monitor_agent_execution(
                session_reference, agent_executor
            )

            # Update execution log with final results including detailed logs
            final_status = agent_result.get("status", "FAILED")

            # Use output_summary from agent result, or fallback to stored logs
            output_summary = agent_result.get("output_summary")
            if not output_summary:
                logger.warning(
                    "Agent result has no output_summary, using stored logs as fallback"
                )
                output_summary = self.execution_logger.get_agent_output_summary()
                if output_summary:
                    logger.info(
                        f"Using stored logs for output_summary ({len(output_summary)} chars)"
                    )

            # Sync metrics one last time before final status
            try:
                from preloop.services.execution_metrics import ExecutionMetricsService

                metrics_service = ExecutionMetricsService(self.db)
                final_metrics = metrics_service.get_execution_metrics(
                    str(self.execution_log.id)
                )
                self.tool_calls_count = final_metrics.get(
                    "tool_calls", self.tool_calls_count
                )
                self.total_tokens = final_metrics.get("token_usage", {}).get(
                    "total_tokens", self.total_tokens
                )
                self.estimated_cost = final_metrics.get(
                    "estimated_cost", self.estimated_cost
                )
            except Exception as e:
                logger.error(f"Failed to calculate final metrics for execution: {e}")

            await self._update_execution_log(
                status=final_status,
                model_output_summary=output_summary,
                error_message=agent_result.get("error_message"),
                actions_taken_summary=agent_result.get("actions_taken"),
                mcp_usage_logs=agent_result.get("mcp_usage_logs"),
                end_time=datetime.now(timezone.utc),
                tool_calls_count=self.tool_calls_count,
                total_tokens=self.total_tokens,
                estimated_cost=self.estimated_cost,
            )
            self._sync_runtime_session(ended_at=datetime.now(timezone.utc))

            # Update commit status to success/failure
            status_state = "success" if final_status == "SUCCEEDED" else "failure"
            status_description = (
                f"Preloop review completed: {self.flow.name}"
                if self.flow
                else "Preloop review completed"
            )
            if final_status != "SUCCEEDED":
                status_description = f"Preloop review failed: {agent_result.get('error_message', 'Unknown error')[:80]}"
            await self._update_commit_status(
                state=status_state,
                description=status_description,
            )

            logger.info(
                f"Flow execution completed with status {final_status}: {self.execution_log.id}"
            )

        except Exception as e:
            logger.error(
                f"Flow execution {self.execution_log.id if self.execution_log else 'unknown'} failed: {e}",
                exc_info=True,
            )

            # Update commit status to failure
            await self._update_commit_status(
                state="failure",
                description=f"Preloop execution failed: {str(e)[:80]}",
            )

            if self.execution_log:
                try:
                    # Sync metrics one last time before final status
                    try:
                        from preloop.services.execution_metrics import (
                            ExecutionMetricsService,
                        )

                        metrics_service = ExecutionMetricsService(self.db)
                        final_metrics = metrics_service.get_execution_metrics(
                            str(self.execution_log.id)
                        )
                        self.tool_calls_count = final_metrics.get(
                            "tool_calls", self.tool_calls_count
                        )
                        self.total_tokens = final_metrics.get("token_usage", {}).get(
                            "total_tokens", self.total_tokens
                        )
                        self.estimated_cost = final_metrics.get(
                            "estimated_cost", self.estimated_cost
                        )
                    except Exception as metrics_error:
                        logger.error(
                            f"Failed to calculate final metrics for failed execution: {metrics_error}"
                        )

                    await self._update_execution_log(
                        status="FAILED",
                        error_message=str(e),
                        end_time=datetime.now(timezone.utc),
                        tool_calls_count=self.tool_calls_count,
                        total_tokens=self.total_tokens,
                        estimated_cost=self.estimated_cost,
                    )
                    self._sync_runtime_session(ended_at=datetime.now(timezone.utc))
                except Exception as update_error:
                    logger.error(
                        f"Failed to update execution log after error: {update_error}",
                        exc_info=True,
                    )
            else:
                logger.error("Cannot update execution log - not created yet")
        finally:
            # Always cleanup the temporary API token
            self._cleanup_temporary_api_token()
