import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, sessionmaker

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from .flow_orchestrator import FlowExecutionOrchestrator
from preloop.sync.event_normalizer import attach_trigger_subject
from preloop.sync.services.event_bus import get_nats_client
from preloop.models.db.session import get_session_factory

logger = logging.getLogger(__name__)


class FlowDispatchError(Exception):
    """A flow execution row was durably committed, but the subsequent
    dispatch (NATS acquisition / worker hand-off) failed.

    Callers that surface HTTP responses should NOT report this as
    "no execution was created": the execution exists (typically PENDING)
    and blindly retrying the trigger would create a duplicate.
    """

    def __init__(
        self, execution_id: str, execution_status: str, original: Exception
    ) -> None:
        self.execution_id = execution_id
        self.execution_status = execution_status
        self.original = original
        super().__init__(
            f"Execution {execution_id} was created but dispatch failed: {original}"
        )


class FlowTriggerService:
    """
    Matches incoming tracker events against active Flow definitions and
    initiates the corresponding Flow Executions if needed.
    """

    def __init__(self, db: Session, session_factory: sessionmaker | None = None):
        self.db = db
        self.session_factory = session_factory or get_session_factory()

    def _create_orchestrator_session(self) -> Session:
        return self.session_factory()

    def _extract_resource_key(self, event_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract a unique resource identifier from the event payload.

        This is used for deduplication - events about the same resource
        (e.g., the same PR/MR) can be coalesced or skipped if an execution
        is already running.

        Returns:
            A unique key like "github:owner/repo:pr:123" or None if not extractable.
        """
        source = event_data.get("source", "").lower()
        payload = event_data.get("payload", {})

        if source == "github":
            # GitHub PR events
            pr = payload.get("pull_request", {})
            if pr:
                repo = payload.get("repository", {})
                repo_full_name = repo.get("full_name", "")
                pr_number = pr.get("number")
                if repo_full_name and pr_number:
                    return f"github:{repo_full_name}:pr:{pr_number}"

            # GitHub issue events
            issue = payload.get("issue", {})
            if issue:
                repo = payload.get("repository", {})
                repo_full_name = repo.get("full_name", "")
                issue_number = issue.get("number")
                if repo_full_name and issue_number:
                    return f"github:{repo_full_name}:issue:{issue_number}"

        elif source == "gitlab":
            # GitLab MR/issue events
            obj_attrs = payload.get("object_attributes", {})
            project = payload.get("project", {})
            project_path = project.get("path_with_namespace", "")

            if obj_attrs:
                iid = obj_attrs.get("iid")
                obj_kind = payload.get("object_kind", "")
                if project_path and iid:
                    return f"gitlab:{project_path}:{obj_kind}:{iid}"

        return None

    def _extract_repo_key(self, event_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract a repository identifier from the event payload.

        Used together with commit SHA for deduplication: an execution is
        considered a duplicate only when both the repo AND the commit SHA
        match a running execution.

        Returns:
            A key like "github:owner/repo" or "gitlab:group/project", or None.
        """
        source = event_data.get("source", "").lower()
        payload = event_data.get("payload", {})

        if source == "github":
            repo = payload.get("repository", {})
            repo_full_name = repo.get("full_name", "")
            if repo_full_name:
                return f"github:{repo_full_name}"

        elif source == "gitlab":
            project = payload.get("project", {})
            project_path = project.get("path_with_namespace", "")
            if project_path:
                return f"gitlab:{project_path}"

        return None

    def _extract_project_id(self, event_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract the internal project_id from webhook event data.

        This looks up the project in our database based on the repository
        information from the webhook payload.

        Args:
            event_data: The event data containing source and payload

        Returns:
            Our internal project UUID as string, or None if not found
        """
        from preloop.models.crud import crud_project

        source = event_data.get("source", "").lower()
        payload = event_data.get("payload", {})
        account_id = event_data.get("account_id")

        if not account_id:
            return None

        # Get tracker_id from dedicated field (UUID for project lookup)
        tracker_id = event_data.get("tracker_id")
        if not tracker_id:
            logger.debug(
                f"No tracker_id in event_data, skipping project extraction "
                f"(source={source})"
            )
            return None

        repo_identifier = None
        repo_external_id = None  # Numeric ID from the tracker platform

        if source == "github":
            repo = payload.get("repository", {})
            # GitHub uses full_name like "owner/repo"
            repo_identifier = repo.get("full_name") or repo.get("name")
            repo_external_id = str(repo.get("id", "")) if repo.get("id") else None

        elif source == "gitlab":
            project = payload.get("project", {})
            # GitLab uses path_with_namespace like "group/project"
            repo_identifier = project.get("path_with_namespace") or project.get("name")
            repo_external_id = str(project.get("id", "")) if project.get("id") else None

        if not repo_identifier:
            return None

        # Derive the short repo name (last segment of path)
        repo_name = (
            repo_identifier.split("/")[-1]
            if "/" in repo_identifier
            else repo_identifier
        )

        # Look up project by name/identifier/slug within the tracker.
        # GitLab projects store: identifier=numeric_id, slug=path_with_namespace, name=display_name
        # GitHub projects store: identifier=numeric_id, slug=full_name, name=repo_name
        projects = crud_project.get_for_tracker(
            self.db, tracker_id=tracker_id, limit=1000
        )

        for proj in projects:
            # Match by slug (path_with_namespace / full_name) - case-insensitive
            if proj.slug and proj.slug.lower() == repo_identifier.lower():
                return str(proj.id)

            # Match by identifier (numeric platform ID stored as string)
            if repo_external_id and proj.identifier == repo_external_id:
                return str(proj.id)

            # Match by name - case-insensitive
            if proj.name and proj.name.lower() == repo_identifier.lower():
                return str(proj.id)

            # Match short repo name against project name - case-insensitive
            if proj.name and proj.name.lower() == repo_name.lower():
                return str(proj.id)

        logger.debug(
            f"Could not match repo '{repo_identifier}' (external_id={repo_external_id}) "
            f"to any of {len(projects)} projects for tracker {tracker_id}"
        )
        return None

    def _has_running_execution(
        self, flow_id: uuid.UUID, resource_key: str, account_id: str
    ) -> bool:
        """
        Check if there's already a running execution for the same flow and resource.

        Args:
            flow_id: The flow to check
            resource_key: The resource identifier (e.g., "github:owner/repo:pr:123")
            account_id: Account ID for scoping

        Returns:
            True if there's already a running execution for this flow+resource.
        """
        # Query specifically for running executions (no limit - we need all of them)
        # This ensures we don't miss long-running executions that might have
        # fallen outside a limit window
        executions = crud_flow_execution.get_running_by_flow(
            self.db,
            flow_id=flow_id,
            account_id=uuid.UUID(account_id)
            if isinstance(account_id, str)
            else account_id,
        )

        for execution in executions:
            # Check if the trigger_event_details contain the same resource
            trigger_details = execution.trigger_event_details or {}
            exec_payload = trigger_details.get("payload", {})

            # Extract resource key from the execution's trigger event
            exec_event_data = {
                "source": trigger_details.get("source", ""),
                "payload": exec_payload,
            }
            exec_resource_key = self._extract_resource_key(exec_event_data)

            if exec_resource_key == resource_key:
                logger.info(
                    f"Found running execution {execution.id} for flow {flow_id} "
                    f"and resource {resource_key} (status: {execution.status})"
                )
                return True

        return False

    def _extract_commit_sha(self, event_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract the commit SHA from the event data.

        Looks for commit SHA in common locations for different event types.
        """
        payload = event_data.get("payload", {})

        # Ensure payload is a dict
        if not isinstance(payload, dict):
            return None

        # GitHub push event
        # Note: head_commit can be None for branch deletions
        head_commit = payload.get("head_commit")
        if head_commit and isinstance(head_commit, dict):
            sha = head_commit.get("id")
            if sha:
                return sha

        # GitHub/GitLab pull request / merge request events
        object_attrs = payload.get("object_attributes", {})
        if object_attrs and isinstance(object_attrs, dict):
            # GitLab MR - last_commit (can be None)
            last_commit = object_attrs.get("last_commit")
            if last_commit and isinstance(last_commit, dict):
                sha = last_commit.get("id")
                if sha:
                    return sha
            # GitLab MR - sha
            if "sha" in object_attrs:
                sha = object_attrs["sha"]
                if sha:
                    return sha

        # GitHub PR event
        pr = payload.get("pull_request")
        if pr and isinstance(pr, dict):
            head = pr.get("head")
            if head and isinstance(head, dict):
                sha = head.get("sha")
                if sha:
                    return sha

        # Direct commit reference
        if "commit" in payload:
            commit = payload["commit"]
            if isinstance(commit, dict):
                sha = commit.get("sha") or commit.get("id")
                if sha:
                    return sha

        # Top-level sha
        if "sha" in payload:
            return payload["sha"]

        # Push events - after field
        if "after" in payload:
            return payload["after"]

        return None

    def find_duplicate_execution(
        self, flow: Flow, event_data: Dict[str, Any]
    ) -> Optional[FlowExecution]:
        """Return a running execution of ``flow`` for the same repo + commit
        as ``event_data``, or None.

        Used by direct-trigger callers (e.g. the webhook endpoint) to preserve
        the commit-SHA deduplication that generic event matching
        (``process_event``) enforces, without silently dropping the event:
        callers can return the existing execution to the caller instead.

        Scope (parity with ``process_event``): dedup applies only when the
        payload carries a recognizable commit SHA (see
        ``_extract_commit_sha``); commit-less payloads are never deduplicated.
        The check-then-insert is also not atomic — a DB-level guard would be
        needed to close the race for concurrent identical deliveries.
        """
        commit_sha = self._extract_commit_sha(event_data)
        if not commit_sha:
            return None
        repo_key = self._extract_repo_key(event_data)
        return self._find_running_execution_for_commit(
            flow.id,
            commit_sha,
            str(flow.account_id),
            repo_key=repo_key,
        )

    def matches_trigger_config(self, flow: Flow, event_data: Dict[str, Any]) -> bool:
        """Public wrapper: does ``event_data`` satisfy ``flow.trigger_config``?"""
        return self._matches_trigger_config(flow, event_data)

    def _find_running_execution_for_commit(
        self,
        flow_id: uuid.UUID,
        commit_sha: str,
        account_id: str,
        repo_key: Optional[str] = None,
    ) -> Optional[FlowExecution]:
        """
        Return a running execution for this repo + commit, if one exists.

        Deduplication is scoped to (repo, commit_sha) so that:
        - Same repo + same commit SHA  → blocked (duplicate)
        - Same repo + different commit SHA → allowed
        - Different repo + same commit SHA → allowed

        Args:
            flow_id: The flow to check
            commit_sha: The commit SHA to check
            account_id: Account ID for scoping
            repo_key: Repository identifier (e.g. "github:owner/repo")
                      for repo-level scoping.  When provided, only
                      executions for the same repo are considered
                      duplicates.

        Returns:
            The already-running execution for this repo + commit
            combination, or None.
        """
        executions = crud_flow_execution.get_running_by_flow(
            self.db,
            flow_id=flow_id,
            account_id=uuid.UUID(account_id)
            if isinstance(account_id, str)
            else account_id,
        )

        for execution in executions:
            trigger_details = execution.trigger_event_details or {}
            exec_sha = self._extract_commit_sha(trigger_details)

            if not exec_sha or exec_sha != commit_sha:
                continue

            # Commit SHA matches — now check repo scoping
            if repo_key:
                exec_event_data = {
                    "source": trigger_details.get("source", ""),
                    "payload": trigger_details.get("payload", {}),
                }
                exec_repo_key = self._extract_repo_key(exec_event_data)
                if exec_repo_key != repo_key:
                    # Same commit in a different repo — allow it
                    continue

            logger.info(
                f"Found running execution {execution.id} for flow {flow_id}, "
                f"repo {repo_key or '(any)'}, and commit {commit_sha[:8]} "
                f"(status: {execution.status})"
            )
            return execution

        return None

    async def _run_orchestrator_with_session(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        nats_client,
    ) -> None:
        orchestrator_db = self._create_orchestrator_session()
        try:
            orchestrator = FlowExecutionOrchestrator(
                orchestrator_db,
                flow_id=flow.id,
                trigger_event_data=event_data,
                nats_client=nats_client,
            )
            await orchestrator.run()
        finally:
            orchestrator_db.close()

    async def _start_flow_execution(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        nats_client,
        *,
        retry_of_execution_id: Optional[uuid.UUID] = None,
        test_mode: bool = False,
        precreated_execution: Any = None,
    ) -> Any:
        """Create (or reuse) a PENDING execution and hand it to a worker or local task.

        When ``FLOW_EXECUTION_WORKER_ENABLED`` is true, publishes ``execute_flow``.
        Otherwise falls back to ``asyncio.create_task`` in-process.
        """
        from preloop.services.flow_execution_dispatcher import (
            dispatch_execute,
            flow_execution_worker_enabled,
        )
        from preloop.services.flow_execution_runner import run_existing_execution
        from preloop.services.flow_orchestrator import _make_json_serializable

        if precreated_execution is not None:
            execution = precreated_execution
            execution_id = execution.id
        else:
            trigger_details = _make_json_serializable(
                dict(event_data) if event_data else {}
            )
            if test_mode:
                trigger_details["test_mode"] = True
            attach_trigger_subject(trigger_details)
            execution_data = FlowExecutionCreate(
                flow_id=flow.id
                if isinstance(flow.id, uuid.UUID)
                else uuid.UUID(str(flow.id)),
                status="PENDING",
                trigger_event_details=trigger_details,
                retry_of_execution_id=retry_of_execution_id,
            )
            execution = crud_flow_execution.create(self.db, obj_in=execution_data)
            self.db.commit()
            self.db.refresh(execution)
            execution_id = execution.id
            logger.info("Created flow execution: %s", execution_id)

        async def _local_run() -> None:
            orchestrator_db = self._create_orchestrator_session()
            try:
                exec_row = crud_flow_execution.get(orchestrator_db, id=execution_id)
                if not exec_row:
                    raise ValueError(f"Failed to load execution {execution_id}")
                flow_id = flow.id
                if isinstance(flow_id, str):
                    flow_id = uuid.UUID(flow_id)
                orchestrator = FlowExecutionOrchestrator(
                    orchestrator_db,
                    flow_id=flow_id,
                    trigger_event_data=exec_row.trigger_event_details or event_data,
                    nats_client=nats_client,
                )
                orchestrator.execution_log = exec_row
                await run_existing_execution(orchestrator)
            finally:
                orchestrator_db.close()

        if flow_execution_worker_enabled():
            await dispatch_execute(execution_id)
        else:
            asyncio.create_task(_local_run())

        return execution

    def _matches_trigger_config(self, flow: Flow, event_data: Dict[str, Any]) -> bool:
        """
        Check if the event matches the flow's trigger_config (if specified).

        Args:
            flow: The flow definition
            event_data: The event data containing payload and metadata

        Returns:
            True if the event matches the trigger config, False otherwise
        """
        if not flow.trigger_config:
            # No additional conditions, event matches
            return True

        # trigger_config can contain conditions like:
        # {"branch": "main"} - for commit events
        # {"labels": ["bug", "critical"]} - for issue events
        # {"status": "opened"} - for PR events
        # {"assignee": "username"} - for assignee filter
        # {"reviewer": "username"} - for reviewer filter
        #
        # For backward compatibility, also support nested filter_conditions:
        # {"assignee": "user", "filter_conditions": {"labels": [...]}}

        payload = event_data.get("payload", {})

        # Flatten trigger_config if it has filter_conditions wrapper
        flattened_config = {}
        for key, value in flow.trigger_config.items():
            if key == "filter_conditions" and isinstance(value, dict):
                # Unpack filter_conditions into top-level
                flattened_config.update(value)
            else:
                flattened_config[key] = value

        logger.info(
            f"Flow {flow.id} ({flow.name}): Checking trigger_config. "
            f"Original: {flow.trigger_config}, Flattened: {flattened_config}, "
            f"Payload keys: {list(payload.keys())}"
        )

        for key, expected_value in flattened_config.items():
            actual_value = payload.get(key)

            # Handle None/missing values
            if actual_value is None:
                logger.debug(
                    f"Flow {flow.id} trigger_config mismatch: "
                    f"{key} not present in payload"
                )
                return False

            if isinstance(expected_value, list):
                # Expected value is a list - check if any expected value matches actual value(s)
                if isinstance(actual_value, list):
                    # Both are lists - check if any expected value is in actual values
                    if not any(item in actual_value for item in expected_value):
                        logger.debug(
                            f"Flow {flow.id} trigger_config mismatch: "
                            f"none of {expected_value} found in {actual_value}"
                        )
                        return False
                else:
                    # Expected is list, actual is single value - check if actual is in expected
                    if actual_value not in expected_value:
                        logger.debug(
                            f"Flow {flow.id} trigger_config mismatch: "
                            f"{key}={actual_value} not in {expected_value}"
                        )
                        return False
            else:
                # Expected value is a single value
                if isinstance(actual_value, list):
                    # Actual is a list - check if expected value is in the list
                    if expected_value not in actual_value:
                        logger.debug(
                            f"Flow {flow.id} trigger_config mismatch: "
                            f"{key}: '{expected_value}' not in {actual_value}"
                        )
                        return False
                else:
                    # Both are single values - exact match required
                    if actual_value != expected_value:
                        logger.debug(
                            f"Flow {flow.id} trigger_config mismatch: "
                            f"{key}={actual_value} != {expected_value}"
                        )
                        return False

        return True

    def _is_preloop_triggered_event(self, event_data: Dict[str, Any]) -> bool:
        """
        Check if an event was triggered by Preloop's own actions.

        This prevents infinite loops where:
        1. Flow runs and updates a PR (adds comment, modifies body, etc.)
        2. Update triggers a new webhook event (pull_request_updated, comment_created)
        3. Event matches another flow -> triggers another execution
        4. Repeat forever

        We detect Preloop-triggered events by checking the sender/actor field
        in the webhook payload for known Preloop bot usernames.
        """
        payload = event_data.get("payload", {})
        source = event_data.get("source", "").lower()

        # Get the sender/actor who triggered the event
        # Note: payload may be enriched with filter_fields which can overwrite
        # dict values (e.g. sender) with strings, so handle both types.
        sender = None
        if source == "github":
            sender_obj = payload.get("sender", {})
            if isinstance(sender_obj, str):
                sender = sender_obj.lower()
            elif isinstance(sender_obj, dict):
                sender = sender_obj.get("login", "").lower()
            else:
                sender = ""
        elif source == "gitlab":
            # GitLab uses "user" for the actor in most events
            user_obj = payload.get("user", {})
            if isinstance(user_obj, str):
                sender = user_obj.lower()
            elif isinstance(user_obj, dict):
                sender = user_obj.get("username", "").lower()
            else:
                sender = ""
            # Some events have object_attributes.author
            if not sender:
                obj_attrs = payload.get("object_attributes", {})
                author = obj_attrs.get("author", {})
                if isinstance(author, dict):
                    sender = author.get("username", "").lower()

        if not sender:
            return False

        # Known Preloop bot username patterns
        # These are typically the usernames of GitHub Apps or GitLab service accounts
        # that Preloop uses to interact with trackers
        preloop_patterns = [
            "preloop",
            "preloop-bot",
            "preloop-staging",
            "preloop-dev",
            "preloop[bot]",  # GitHub App format
            "preloop-app",
        ]

        for pattern in preloop_patterns:
            if sender == pattern or sender.startswith("preloop"):
                logger.info(
                    f"Ignoring event triggered by Preloop bot account: {sender}"
                )
                return True

        return False

    async def process_event(self, event_data: Dict[str, Any]):
        """
        Process an incoming event and trigger any matching flows.

        Args:
            event_data: Dictionary containing:
                - source: Tracker type (e.g., 'github', 'gitlab', 'jira', 'webhook')
                - tracker_id: Tracker UUID for project lookup (optional, used for filtering)
                - type: Event type (e.g., 'push', 'issue_created')
                - payload: Event payload from the tracker
                - account_id: Account ID for scoping
        """
        event_source = event_data.get("source")
        event_type = event_data.get("type")
        account_id = event_data.get("account_id")
        # tracker_id is the UUID stored in flow.trigger_event_source
        tracker_id = event_data.get("tracker_id")

        if not event_source or not event_type:
            logger.warning(
                f"Event data is missing required fields: source={event_source}, type={event_type}"
            )
            return

        # Check if this event was triggered by Preloop itself to prevent infinite loops
        if self._is_preloop_triggered_event(event_data):
            logger.info(
                f"Skipping event triggered by Preloop bot to prevent infinite loop: "
                f"source='{event_source}', type='{event_type}'"
            )
            return

        logger.info(
            f"Processing event from source='{event_source}', type='{event_type}', "
            f"account_id={account_id}"
        )

        try:
            # Extract project_id from the event payload for project-based filtering
            project_id = self._extract_project_id(event_data)
            if project_id:
                event_data["project_id"] = project_id
                logger.info(f"Extracted project_id for filtering: {project_id}")

            # Query for flows that match the event source and type
            # trigger_event_source stores the tracker UUID, not the tracker type
            query_source = tracker_id or event_source
            matching_flows: List[Flow] = crud_flow.get_by_trigger(
                self.db,
                event_source=query_source,
                event_type=event_type,
                project_id=project_id,
                account_id=account_id,
            )

            if not matching_flows:
                logger.warning(
                    f"No flows found matching source='{query_source}', type='{event_type}', "
                    f"account_id={account_id}, project_id={project_id}. "
                    f"Check that flows are configured with the correct tracker ID as trigger_event_source."
                )
                return

            logger.info(f"Found {len(matching_flows)} potential matching flow(s)")

            # Filter flows by trigger_config and enabled status
            flows_to_trigger = []
            for flow in matching_flows:
                if not flow.is_enabled:
                    logger.warning(
                        f"Skipping disabled flow '{flow.name}' ({flow.id}). "
                        f"To enable this flow, set is_enabled=true via the API or UI."
                    )
                    continue

                if not self._matches_trigger_config(flow, event_data):
                    logger.info(
                        f"Skipping flow '{flow.name}' ({flow.id}) - trigger_config does not match. "
                        f"Config: {flow.trigger_config}"
                    )
                    continue

                flows_to_trigger.append(flow)

            if not flows_to_trigger:
                logger.info("No enabled flows with matching trigger_config found")
                return

            # Get NATS client for publishing updates
            nats_client = await get_nats_client()

            # Extract repo key and commit SHA for deduplication.
            # Dedup is scoped to (repo, commit_sha) so that different repos
            # or different commits for the same repo can run in parallel.
            repo_key = self._extract_repo_key(event_data)
            commit_sha = self._extract_commit_sha(event_data)
            if repo_key:
                logger.info(f"Extracted repo key for deduplication: {repo_key}")
            if commit_sha:
                logger.info(f"Extracted commit SHA for deduplication: {commit_sha[:8]}")

            # Trigger each matching flow
            for flow in flows_to_trigger:
                try:
                    # Check for a running execution with the same repo + commit SHA.
                    # This catches duplicate events for the same commit
                    # (e.g., push + PR update when description is edited).
                    if commit_sha and account_id:
                        if (
                            self._find_running_execution_for_commit(
                                flow.id, commit_sha, account_id, repo_key=repo_key
                            )
                            is not None
                        ):
                            logger.info(
                                f"Skipping flow '{flow.name}' ({flow.id}) - "
                                f"already has a running execution for "
                                f"repo {repo_key or '(any)'} commit {commit_sha[:8]}. "
                                f"This prevents duplicate executions when multiple events "
                                f"are triggered for the same commit."
                            )
                            continue

                    logger.info(
                        f"Triggering flow '{flow.name}' ({flow.id}) for event {event_type}"
                    )
                    event_copy = dict(event_data)
                    await self._start_flow_execution(
                        flow=flow,
                        event_data=event_copy,
                        nats_client=nats_client,
                    )
                    logger.info(f"Flow '{flow.name}' ({flow.id}) execution initiated")
                except Exception as e:
                    logger.error(
                        f"Error initiating flow '{flow.name}' ({flow.id}): {e}",
                        exc_info=True,
                    )

        except Exception as e:
            logger.error(
                f"Error processing event source='{event_source}', type='{event_type}': {e}",
                exc_info=True,
            )

    async def trigger_flow(
        self,
        flow_id: uuid.UUID,
        test_mode: bool = False,
        trigger_event_data: Optional[Dict[str, Any]] = None,
        retry_of_execution_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Manually trigger a flow execution for testing purposes or as a retry.

        Args:
            flow_id: The ID of the flow to trigger
            test_mode: Whether this is a test execution
            trigger_event_data: Optional custom trigger event data for testing
            retry_of_execution_id: If this is a retry, the ID of the original execution

        Returns:
            Dict with execution_id and status
        """
        # Get the flow
        flow_id_str = str(flow_id)
        # Use CRUD layer without account filtering for test mode
        flow = crud_flow.get(self.db, id=flow_id_str)

        if not flow:
            raise ValueError(f"Flow {flow_id} not found")

        if retry_of_execution_id:
            logger.info(
                f"Triggering retry execution for flow '{flow.name}' ({flow.id}), "
                f"retrying execution {retry_of_execution_id}"
            )
        else:
            logger.info(f"Triggering test execution for flow '{flow.name}' ({flow.id})")

        # Pre-create the execution record so we can return its ID immediately
        # Merge custom trigger_event_data with test_mode flag
        # Important: Set test_mode AFTER updating from trigger_event_data to ensure
        # retries don't inherit test_mode=True from the original test execution
        trigger_details = {}
        if trigger_event_data:
            trigger_details.update(trigger_event_data)
        trigger_details["test_mode"] = test_mode

        from preloop.services.flow_orchestrator import _make_json_serializable

        trigger_details = _make_json_serializable(trigger_details)
        attach_trigger_subject(trigger_details)

        execution_data = FlowExecutionCreate(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details=trigger_details,
            retry_of_execution_id=retry_of_execution_id,
        )

        execution = crud_flow_execution.create(self.db, obj_in=execution_data)
        self.db.commit()
        self.db.refresh(execution)

        execution_id = execution.id
        execution_status = execution.status

        logger.info(f"Created flow execution: {execution_id}")

        # The execution row is durably committed at this point. Any failure
        # below (NATS acquisition, worker hand-off) must not be reported as
        # "no execution created" — wrap it so callers can distinguish.
        try:
            # Get NATS client (needed for local fallback path)
            nats_client = await get_nats_client()

            await self._start_flow_execution(
                flow=flow,
                event_data=trigger_details,
                nats_client=nats_client,
                precreated_execution=execution,
            )
        except Exception as e:
            raise FlowDispatchError(str(execution_id), execution_status, e) from e

        return {
            "id": str(execution_id),
            "status": execution_status,
            "flow_id": flow_id_str,
        }

    async def _run_orchestrator_without_creation(self, orchestrator):
        """Deprecated local path; prefer ``run_existing_execution``."""
        from preloop.services.flow_execution_runner import run_existing_execution

        try:
            await run_existing_execution(orchestrator)
        finally:
            orchestrator._cleanup_temporary_api_token()
            if orchestrator.db:
                try:
                    orchestrator.db.close()
                    logger.debug("Closed orchestrator database session")
                except Exception as close_error:
                    logger.warning(
                        f"Error closing orchestrator database session: {close_error}"
                    )
