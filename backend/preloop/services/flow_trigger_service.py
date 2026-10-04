import asyncio
from datetime import datetime, timezone
import functools
import logging
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from preloop.utils.bitbucket import (
    normalize_uuid as normalize_bitbucket_uuid,
    payload_commit_hash as bitbucket_payload_commit_hash,
)
from preloop.models.crud import crud_flow, crud_flow_execution, crud_issue
from preloop.models.models import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.services.model_routing import (
    ModelRoutingError,
    apply_no_progress_escalation,
    load_source_execution_for_flow,
    prepare_execution_routing,
)
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_RUNNER_ERROR,
    FAILURE_CATEGORY_UNKNOWN,
    derive_failure_category,
)
from preloop.services.flow_ci_feedback import (
    GITHUB_CI_EVENT_TYPES,
    bind_ci_failure_resume_or_skip,
    flow_requires_ci_failure_resume,
)
from .flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.kill_switch import FlowHaltActiveError, flows_halted
from preloop.sync.event_normalizer import (
    LABEL_CHANGE_ACTIONS,
    LABEL_CHANGE_EVENT_TYPES,
    PR_CLOSE_STOP_SOURCES,
    PR_HEAD_UPDATE_EVENT_TYPES,
    PR_STOP_SOURCE_MERGED,
    PR_STOP_SOURCE_SUPERSEDED,
    attach_trigger_subject,
    gitlab_label_delta,
    pr_close_stop_source,
)
from preloop.sync.services.event_bus import get_nats_client
from preloop.services.webhook_delivery_dedupe import (
    delivery_key_for_event,
    find_execution_for_delivery,
    is_delivery_key_conflict,
)
from preloop.utils.workspace_seed import attach_workspace_file_paths
from preloop.models.db.session import get_session_factory
from preloop.schemas.issue_triage import provider_revision
from preloop.services.issue_triage_trigger import (
    issue_update_touches_content,
    skip_triage_flow_for_event,
)

logger = logging.getLogger(__name__)


def _schedule_now() -> datetime:
    """Wall-clock time of a schedule tick (patched in tests)."""
    return datetime.now(timezone.utc)


def _previous_fire_iso(raw_schedule: Any, now: datetime) -> str:
    """Previous fire time of a stored schedule as of ``now``, ISO 8601 UTC.

    An unparseable stored config cannot say when it last fired; the run
    still gets a window, one minimum interval wide, rather than none.
    """
    from preloop.models.schemas.flow import (
        MIN_SCHEDULE_INTERVAL,
        parse_schedule_config,
    )

    try:
        _, previous = parse_schedule_config(raw_schedule).fire_window(now)
    except Exception:
        logger.warning("Could not compute previous fire time", exc_info=True)
        previous = now - MIN_SCHEDULE_INTERVAL
    return previous.isoformat()


# Maximum number of matrix cells a single trigger may fan out to. Keeps a
# runaway matrix from creating unbounded executions in one request; the
# design-partner use case is a 5x3 grid, so 25 leaves headroom.
MATRIX_MAX_ENTRIES = 25

# Resource-key kinds (third segment of ``_extract_resource_key``) that name a
# tracker object a human works on one at a time. Releases and generic webhook
# bodies are deliberately absent: they have their own dedup path.
TRACKER_OBJECT_KINDS: frozenset = frozenset({"issue", "pr", "merge_request"})

# An execution in any of these holds, or is about to hold, the object. A
# terminal run does not, and a parked run does: WAITING_FOR_HUMAN owns the
# issue until a human answers, WAITING_FOR_CHILDREN owns it until the flows
# it started finish, and in both cases the resume continues the same work.
TRACKER_OBJECT_ACTIVE_STATUSES = (
    "PENDING",
    "INITIALIZING",
    "STARTING",
    "RUNNING",
    "WAITING_FOR_HUMAN",
    "WAITING_FOR_CHILDREN",
)

# Event types whose whole purpose is to reach an execution that is already
# running on the object: PR-comment resumes and CI-failure resumes bind to
# the live run themselves (see flow_pr_binding / flow_ci_feedback), so the
# coalescing guard must not swallow them first.
COALESCE_EXEMPT_EVENT_TYPES: frozenset = frozenset(
    {"comment_created", "comment_updated", "comment_deleted"}
) | frozenset(GITHUB_CI_EVENT_TYPES)

# Resource-key kinds that name a pull or merge request, the objects whose
# executions are stopped when the request is merged, closed or gets a new
# head (#1032).
PR_OBJECT_KINDS: frozenset = frozenset({"pr", "merge_request"})


def flow_supersedes_on_update(flow: Any) -> bool:
    """Whether a new head on a pull request stops this flow's older run.

    Opt-in per flow through ``webhook_config.supersede_on_update`` (#1032).
    Off by default, so a flow that wants every head reviewed keeps today's
    behaviour; the Pull Request Reviewer preset turns it on.
    """
    config = getattr(flow, "webhook_config", None)
    return isinstance(config, dict) and config.get("supersede_on_update") is True


def flow_handles_pr_close(flow: Any) -> bool:
    """Whether the flow itself triggers on a merged or closed pull request.

    Such a flow runs *because* the request ended, so its executions on that
    request are never stopped for it.
    """
    types = getattr(flow, "trigger_event_types", None) or []
    return any(event_type in PR_CLOSE_STOP_SOURCES for event_type in types)


def _describe_pr_object(object_key: str) -> str:
    """``pull request owner/repo#12`` or ``merge request group/project!7``."""
    parts = object_key.split(":")
    repo = ":".join(parts[1:-2])
    kind, ident = parts[-2], parts[-1]
    if kind == "merge_request":
        return f"merge request {repo}!{ident}"
    return f"pull request {repo}#{ident}"


def _short(sha: str) -> str:
    return sha[:8]


def _label_name(item: Any) -> Optional[str]:
    """Return a label title from a string or GitHub/GitLab label object."""
    if isinstance(item, str) and item.strip():
        return item
    if isinstance(item, dict):
        name = item.get("name") or item.get("title")
        if isinstance(name, str) and name.strip():
            return name
    return None


def _label_names_from_payload(payload: Dict[str, Any]) -> List[str]:
    """Collect label names from a raw or enriched webhook payload.

    Production events merge ``extract_filter_fields`` (string lists) into the
    payload. This also unwraps GitHub ``issue.labels[].name`` and GitLab
    ``labels[].title`` so ``filter_conditions.labels`` matches those shapes
    even without enrichment.
    """
    names: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            name = _label_name(item)
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    _add(payload.get("labels"))
    issue = payload.get("issue")
    if isinstance(issue, dict):
        _add(issue.get("labels"))
    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict):
        _add(obj_attrs.get("labels"))
    _add(payload.get("label"))
    return names


def _object_label_names(payload: Dict[str, Any]) -> List[str]:
    """Label names currently on the issue or merge request.

    Unlike ``_label_names_from_payload`` this ignores the event's single
    ``label`` subject, so an ``unlabeled`` delivery does not count the label
    that just left. GitHub ``issue.labels`` / ``pull_request.labels`` and
    GitLab top-level ``labels`` already reflect the state after the change.
    """
    names: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            name = _label_name(item)
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    _add(payload.get("labels"))
    for key in ("issue", "pull_request", "object_attributes"):
        obj = payload.get(key)
        if isinstance(obj, dict):
            _add(obj.get("labels"))
            fields = obj.get("fields")  # Jira issue.fields.labels
            if isinstance(fields, dict):
                _add(fields.get("labels"))
    return names


def _is_label_change_event(event_data: Dict[str, Any]) -> bool:
    """True when this delivery is about one label being added or removed.

    Matches the normalized ``issue_labeled`` / ``issue_unlabeled`` names and
    the raw provider action, so GitHub's ``pull_request.labeled`` (which has
    no normalized name of its own yet) is treated the same way.
    """
    if (event_data.get("type") or "") in LABEL_CHANGE_EVENT_TYPES:
        return True
    payload = event_data.get("payload")
    if isinstance(payload, dict):
        return payload.get("action") in LABEL_CHANGE_ACTIONS
    return False


def _event_label_names(payload: Dict[str, Any]) -> List[str]:
    """Label names carried by a labeled/unlabeled event itself.

    Three shapes, in the order they are cheapest to read:

    * enriched payloads merge ``extract_filter_fields``, which exposes the
      delta as ``added_labels`` / ``removed_labels``
    * raw GitHub puts the one subject label under ``label``
    * raw GitLab sends no dedicated event; the delta is in ``changes.labels``

    Additions and removals are both returned. A GitLab edit that adds and
    removes in one hook normalizes to ``issue_labeled`` while still carrying
    the removed titles (see ``gitlab_label_delta``), and an ``unlabeled``
    flow filters on the label that left.
    """
    names: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            name = _label_name(item)
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    _add(payload.get("added_labels"))
    _add(payload.get("removed_labels"))
    _add(payload.get("label"))
    added, removed = gitlab_label_delta(payload)
    _add(added)
    _add(removed)
    return names


#: Local-dispatch tasks started by ``_start_flow_execution`` when no execution
#: worker is enabled. The event loop keeps only weak references to tasks, so an
#: unreferenced one can be garbage-collected mid-run; holding it here until it
#: finishes also guarantees its done-callback runs and sees the outcome.
_LOCAL_RUN_TASKS: Set["asyncio.Task[None]"] = set()

#: Worker-thread writes that record a crashed local dispatch as FAILED, held
#: until they finish for the same reason as ``_LOCAL_RUN_TASKS``.
_LOCAL_RUN_FAILURE_WRITES: Set["asyncio.Task[bool]"] = set()

#: Statuses a crashed local dispatch may overwrite with FAILED. Terminal rows
#: already say how the run ended, and parked or resuming rows belong to the
#: park/resume handshake, so neither is touched.
_LOCAL_RUN_FAILABLE_STATUSES = frozenset(
    {"PENDING", "INITIALIZING", "STARTING", "RUNNING"}
)


def _record_local_run_failure(
    session_factory: Callable[[], Session],
    execution_id: uuid.UUID,
    exc: BaseException,
) -> bool:
    """Mark a locally dispatched execution FAILED after its task raised.

    The row is only updated while it is still in a pre-terminal status and no
    agent runtime was recorded for it. A row with an agent session reference
    has a live container that execution recovery can still adopt, so it is
    left for recovery instead of being failed underneath the container.

    Args:
        session_factory: Zero-argument callable returning a fresh Session. The
            callback runs after the dispatching request finished, so it cannot
            reuse that request's session.
        execution_id: The execution the failed task was running.
        exc: The exception the task raised.

    Returns:
        True when the execution was marked FAILED, False when it was left
        unchanged (missing, already finished, parked, has a runtime, or the
        update itself failed).
    """
    db = session_factory()
    try:
        execution = crud_flow_execution.get(db, id=execution_id)
        if execution is None:
            return False
        if execution.status not in _LOCAL_RUN_FAILABLE_STATUSES:
            return False
        if execution.agent_session_reference:
            return False
        message = f"Local flow dispatch failed: {type(exc).__name__}: {exc}"
        category = derive_failure_category(
            status="FAILED", error_message=message, exception=exc
        )
        if category in (None, FAILURE_CATEGORY_UNKNOWN):
            # The run never reached an agent: the in-process dispatcher lost
            # it, which is a runner failure rather than an agent one.
            category = FAILURE_CATEGORY_RUNNER_ERROR
        crud_flow_execution.update(
            db,
            db_obj=execution,
            obj_in=FlowExecutionUpdate(
                status="FAILED",
                error_message=message,
                failure_category=category,
                end_time=datetime.now(timezone.utc),
            ),
        )
        db.commit()
        # The dispatch died before the orchestrator's terminal hook, so record
        # the issue cost fact here. Never raises; a failure is picked up by
        # the scheduled rebuild.
        from preloop.services.issue_cost_rollup import (
            record_execution_finished_safely,
        )

        record_execution_finished_safely(db, execution_id)
        return True
    except Exception:
        logger.exception(
            "Could not record the local dispatch failure of execution %s",
            execution_id,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass
        return False
    finally:
        db.close()


def _supervise_local_run(
    task: "asyncio.Task[None]",
    *,
    execution_id: uuid.UUID,
    session_factory: Callable[[], Session],
) -> None:
    """Done-callback for a local-dispatch task: log and fail on an exception.

    Without it, an exception raised by the in-process run is never retrieved:
    nothing is logged and the execution stays PENDING until the next process
    restart runs execution recovery. The error is logged here; the FAILED
    write is handed to a worker thread so it never blocks the event loop.

    Args:
        task: The finished local-dispatch task.
        execution_id: The execution the task was running.
        session_factory: Zero-argument callable returning a fresh Session.
    """
    _LOCAL_RUN_TASKS.discard(task)
    if task.cancelled():
        # Cancellation is a shutdown, not a failure: the row stays active and
        # execution recovery re-dispatches it when the process comes back.
        logger.warning(
            "Local flow run for execution %s was cancelled; leaving it for "
            "execution recovery",
            execution_id,
        )
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error(
        "Local flow run for execution %s failed: %s",
        execution_id,
        exc,
        exc_info=exc,
    )
    # This callback runs on the event-loop thread. The status write is
    # synchronous SQLAlchemy work that can block on a slow or exhausted pool
    # (plausibly the very reason the run failed), so it runs in a worker
    # thread instead of stalling every other request on the loop.
    write = asyncio.to_thread(
        _record_local_run_failure_and_log, session_factory, execution_id, exc
    )
    try:
        write_task = task.get_loop().create_task(write)
    except RuntimeError:
        # The loop is closing: nothing scheduled now would run. The row stays
        # active and execution recovery picks it up on the next start.
        write.close()
        logger.warning(
            "Could not schedule the failure write for execution %s; leaving "
            "it for execution recovery",
            execution_id,
        )
        return
    _LOCAL_RUN_FAILURE_WRITES.add(write_task)
    write_task.add_done_callback(
        functools.partial(_log_failure_write_outcome, execution_id=execution_id)
    )


def _record_local_run_failure_and_log(
    session_factory: Callable[[], Session],
    execution_id: uuid.UUID,
    exc: BaseException,
) -> bool:
    """Record a crashed local dispatch and log when the row was marked.

    Runs in a worker thread (see ``_supervise_local_run``).

    Args:
        session_factory: Zero-argument callable returning a fresh Session.
        execution_id: The execution the failed task was running.
        exc: The exception the task raised.

    Returns:
        True when the execution was marked FAILED.
    """
    marked = _record_local_run_failure(session_factory, execution_id, exc)
    if marked:
        logger.info(
            "Execution %s marked FAILED after its local dispatch raised",
            execution_id,
        )
    return marked


def _log_failure_write_outcome(
    write_task: "asyncio.Task[bool]", *, execution_id: uuid.UUID
) -> None:
    """Done-callback for the failure write: release it and log a crash.

    ``_record_local_run_failure`` already logs its own database errors, so
    this only reports a write that was cancelled or failed outside it.

    Args:
        write_task: The finished failure-write task.
        execution_id: The execution whose failure was being recorded.
    """
    _LOCAL_RUN_FAILURE_WRITES.discard(write_task)
    if write_task.cancelled():
        logger.warning(
            "Failure write for execution %s was cancelled; leaving it for "
            "execution recovery",
            execution_id,
        )
        return
    write_exc = write_task.exception()
    if write_exc is not None:
        logger.error(
            "Could not record the local dispatch failure of execution %s",
            execution_id,
            exc_info=write_exc,
        )


def _triage_timestamp(value: Any) -> Optional[datetime]:
    """Parse provider timestamps without treating a timezone-less value as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value.replace(" UTC", "+00:00").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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
        self._session_factory = session_factory
        self._db_thread = threading.get_ident()

    def _create_orchestrator_session(self) -> Session:
        factory = self._session_factory or get_session_factory()
        return factory()

    def _flows_halted(self, account_id: Any) -> bool:
        """Read the account halt flag on a Session owned by this thread.

        Lifecycle workers may marshal ``_start_flow_execution`` onto the
        application loop. SQLAlchemy sync Sessions are not safe there.
        """
        db = self.db
        if (
            isinstance(db, Session)
            and threading.get_ident() != self._db_thread
            and not isinstance(db.get_bind(), Connection)
        ):
            session = self._create_orchestrator_session()
            try:
                return flows_halted(session, account_id)
            finally:
                session.close()
        return flows_halted(db, account_id)

    @staticmethod
    def _extract_resource_key(event_data: Dict[str, Any]) -> Optional[str]:
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

            # GitHub release events
            release = payload.get("release") or {}
            tag = release.get("tag_name")
            if tag:
                repo = payload.get("repository", {})
                repo_full_name = repo.get("full_name", "")
                if repo_full_name:
                    return f"github:{repo_full_name}:release:{tag}"

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

            # GitLab release events (object_kind == "release"). GitLab's
            # Release Hook puts the tag at the top level; the nested
            # release.tag shape is GitHub's, not GitLab's.
            if payload.get("object_kind") == "release":
                tag = payload.get("tag")
                if project_path and tag:
                    return f"gitlab:{project_path}:release:{tag}"

        elif source == "bitbucket":
            pr = payload.get("pullrequest") or {}
            repo_full_name = (payload.get("repository") or {}).get("full_name", "")
            if isinstance(pr, dict) and pr.get("id") and repo_full_name:
                return f"bitbucket:{repo_full_name}:pr:{pr['id']}"

        elif source == "bitbucket_dc":
            # Keyed on the immutable repository id, never the mutable slug.
            pr = payload.get("pull_request") or {}
            repo_id = (payload.get("repository") or {}).get("id")
            if isinstance(pr, dict) and pr.get("number") and repo_id:
                return f"bitbucket_dc:{repo_id}:pr:{pr['number']}"

        return None

    @staticmethod
    def _resolve_json_path(payload: Any, path: str) -> Any:
        """Resolve a dotted JSON path like ``attachments.0.title_link``.

        Numeric segments index into lists; other segments index into dicts.
        Returns None when any step is missing or the shape does not match.
        """
        current: Any = payload
        for part in path.split("."):
            if isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    def _extract_webhook_resource_key(
        self, flow: Flow, payload: Dict[str, Any]
    ) -> Optional[str]:
        """
        Extract a deduplication key from a generic webhook payload.

        Uses ``flow.webhook_config["dedupe_path"]`` (a dotted JSON path into
        the webhook body) when configured. Without configuration, safe
        defaults cover GlitchTip/Slack-style alert payloads
        (``attachments[0].title_link``) and Sentry-style payloads
        (``data.issue.id``).

        Returns:
            A stable key like ``webhook:attachments.0.title_link=<url>``,
            or None when no configured/default path yields a value.
        """
        webhook_config = flow.webhook_config or {}
        configured = (
            webhook_config.get("dedupe_path")
            if isinstance(webhook_config, dict)
            else None
        )
        if configured:
            paths = [configured]
        else:
            paths = ["attachments.0.title_link", "data.issue.id"]

        for path in paths:
            value = self._resolve_json_path(payload, path)
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, ""):
                return f"webhook:{path}={value}"
        return None

    def _extract_dedupe_resource_key(
        self, flow: Flow, event_data: Dict[str, Any]
    ) -> Optional[str]:
        """Deduplication key for ``event_data`` under ``flow``'s trigger type."""
        if (event_data.get("source") or "").lower() == "webhook":
            payload = event_data.get("payload")
            if isinstance(payload, dict):
                return self._extract_webhook_resource_key(flow, payload)
            return None
        return self._extract_resource_key(event_data)

    def _fallback_dedupe_resource_key(
        self, flow: Flow, event_data: Dict[str, Any]
    ) -> Optional[str]:
        """Resource key restricted to sources where commit-SHA dedup cannot apply.

        Generic webhook deliveries (GlitchTip alerts, custom integrations) and
        GitHub/GitLab release events carry no commit SHA, so the existing
        commit-based dedup never fires for them. Issue/PR events are excluded:
        they may legitimately lack a SHA (issue comments, label changes) and
        their dedup behavior must stay unchanged (see issue #241).
        """
        source = (event_data.get("source") or "").lower()
        if source != "webhook":
            payload = event_data.get("payload")
            is_release_event = isinstance(payload, dict) and (
                bool(payload.get("release")) or payload.get("object_kind") == "release"
            )
            if not is_release_event:
                return None
        return self._extract_dedupe_resource_key(flow, event_data)

    def _find_running_execution_for_resource_key(
        self,
        flow: Flow,
        resource_key: str,
        account_id: str,
    ) -> Optional[FlowExecution]:
        """
        Return a running execution of ``flow`` for the same resource key.

        Mirrors ``_find_running_execution_for_commit`` but keyed on the
        resource identifier (issue/PR/release/webhook body identity) instead
        of a commit SHA, so commit-less deliveries (GlitchTip alerts, release
        notifications) can still be coalesced across retries.
        """
        executions = crud_flow_execution.get_running_by_flow(
            self.db,
            flow_id=flow.id,
            account_id=uuid.UUID(account_id)
            if isinstance(account_id, str)
            else account_id,
        )

        for execution in executions:
            trigger_details = execution.trigger_event_details or {}
            exec_payload = trigger_details.get("payload", {})
            exec_event_data = {
                "source": trigger_details.get("source", ""),
                "payload": exec_payload if isinstance(exec_payload, dict) else {},
            }
            if self._extract_dedupe_resource_key(flow, exec_event_data) == (
                resource_key
            ):
                logger.info(
                    f"Found running execution {execution.id} for flow "
                    f"{flow.id} and resource {resource_key} "
                    f"(status: {execution.status})"
                )
                return execution

        return None

    def _extract_tracker_object_key(self, event_data: Dict[str, Any]) -> Optional[str]:
        """Resource key for the issue/PR/MR this event is about, or None.

        Reuses ``_extract_resource_key`` and keeps only the kinds a human
        works on one at a time, so the coalescing guard cannot accidentally
        serialize release or webhook-body deliveries that already have their
        own dedup path.
        """
        key = self._extract_resource_key(event_data)
        if not key:
            return None
        parts = key.split(":")
        if len(parts) < 4:
            return None
        return key if parts[2] in TRACKER_OBJECT_KINDS else None

    def _find_active_execution_for_tracker_object(
        self,
        flow: Flow,
        object_key: str,
        account_id: str,
    ) -> Optional[FlowExecution]:
        """Return this flow's active execution for the same issue/PR, if any.

        Wider than the commit and resource-key dedup above in two ways: it
        covers issue and pull-request events (which those deliberately skip,
        see issue #241) and it counts a parked run as active. Narrower in
        one: it only answers for tracker objects.
        """
        executions = crud_flow_execution.get_running_by_flow(
            self.db,
            flow_id=flow.id,
            account_id=uuid.UUID(account_id)
            if isinstance(account_id, str)
            else account_id,
            running_statuses=list(TRACKER_OBJECT_ACTIVE_STATUSES),
            tracker_object_key=object_key,
        )

        for execution in executions:
            trigger_details = execution.trigger_event_details or {}
            exec_payload = trigger_details.get("payload", {})
            exec_event_data = {
                "source": trigger_details.get("source", ""),
                "payload": exec_payload if isinstance(exec_payload, dict) else {},
            }
            if self._extract_tracker_object_key(exec_event_data) == object_key:
                return execution

        return None

    def find_active_execution_for_event_object(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        account_id: Any,
    ) -> Optional[Tuple[str, FlowExecution]]:
        """Object key and this flow's active run for it, or None.

        Public entry point for callers outside the webhook path (the manual
        ``POST /flows/run-preset`` runs) so one active execution per (flow,
        tracker object) means the same thing however the run was started.
        """
        object_key = self._extract_tracker_object_key(event_data)
        if not object_key:
            return None
        active = self._find_active_execution_for_tracker_object(
            flow, object_key, str(account_id)
        )
        return (object_key, active) if active is not None else None

    def record_coalesced_trigger(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        object_key: str,
        active: FlowExecution,
        *,
        reason: str = "active_execution_for_tracker_object",
    ) -> None:
        """Make a skipped trigger visible instead of silently dropping it."""
        logger.info(
            "Skipping flow '%s' (%s) for %s: execution %s (status %s) is "
            "already active on %s. One active execution per flow and tracker "
            "object; the event is not queued.",
            flow.name,
            flow.id,
            event_data.get("type"),
            active.id,
            active.status,
            object_key,
        )
        try:
            from preloop.models.crud import crud_event

            crud_event.log_event(
                self.db,
                event_type="flow_trigger_skipped_active_object",
                account_id=flow.account_id,
                event_data={
                    "flow_id": str(flow.id),
                    "flow_name": flow.name,
                    "trigger_source": event_data.get("source"),
                    "trigger_type": event_data.get("type"),
                    "object_key": object_key,
                    "active_execution_id": str(active.id),
                    "active_status": active.status,
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001 - audit must never block triggering
            logger.warning(
                "Could not record the skipped trigger for flow %s on %s",
                flow.id,
                object_key,
                exc_info=True,
            )

    @staticmethod
    def _execution_event_data(execution: FlowExecution) -> Dict[str, Any]:
        """The ``source``/``type``/``payload`` an execution was started from."""
        trigger_details = execution.trigger_event_details or {}
        payload = trigger_details.get("payload", {})
        return {
            "source": trigger_details.get("source", ""),
            "type": trigger_details.get("type"),
            "payload": payload if isinstance(payload, dict) else {},
        }

    def _extract_pr_object_key(self, event_data: Dict[str, Any]) -> Optional[str]:
        """Resource key of the pull or merge request an event is about.

        Includes comments on the request: a GitHub ``issue_comment`` names
        the PR as an ``issue`` with a ``pull_request`` link, a GitLab note
        as ``merge_request``. Mirrors
        :func:`preloop.models.crud.flow_execution.pull_request_payload_match`.
        """
        key = self._extract_tracker_object_key(event_data)
        if key and key.split(":")[-2] in PR_OBJECT_KINDS:
            return key
        source = str(event_data.get("source") or "").lower()
        payload = event_data.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if source == "github":
            issue = payload.get("issue") or {}
            repo = (payload.get("repository") or {}).get("full_name")
            if isinstance(issue, dict) and issue.get("pull_request") and repo:
                number = issue.get("number")
                if number:
                    return f"github:{repo}:pr:{number}"
        elif source == "gitlab":
            merge_request = payload.get("merge_request") or {}
            path = (payload.get("project") or {}).get("path_with_namespace")
            if isinstance(merge_request, dict) and merge_request.get("iid") and path:
                return f"gitlab:{path}:merge_request:{merge_request['iid']}"
        return None

    async def _stop_for_pull_request(
        self,
        execution: FlowExecution,
        *,
        flow: Any,
        object_key: str,
        stop_source: str,
        reason: str,
        event_data: Dict[str, Any],
        nats_client: Any,
    ) -> bool:
        """Stop one execution through the shared stop path and audit it."""
        from preloop.services.flow_execution_stop import stop_execution

        outcome = await stop_execution(
            self.db,
            execution,
            account_id=flow.account_id,
            nats_client=nats_client,
            error_message=reason,
            stop_reason=reason,
            stop_source=stop_source,
        )
        if not outcome.stopped:
            return False
        logger.info(
            "Stopped execution %s of flow '%s' (%s): %s",
            execution.id,
            flow.name,
            flow.id,
            reason,
        )
        try:
            from preloop.models.crud import crud_event

            crud_event.log_event(
                self.db,
                event_type="flow_execution_stopped_for_pull_request",
                account_id=flow.account_id,
                event_data={
                    "flow_id": str(flow.id),
                    "flow_name": flow.name,
                    "execution_id": str(execution.id),
                    "object_key": object_key,
                    "stop_source": stop_source,
                    "trigger_source": event_data.get("source"),
                    "trigger_type": event_data.get("type"),
                },
            )
        except Exception:  # noqa: BLE001 - audit must never block the stop
            logger.warning(
                "Could not record the pull request stop of execution %s",
                execution.id,
                exc_info=True,
            )
        return True

    async def stop_executions_for_ended_pull_request(
        self,
        event_data: Dict[str, Any],
        *,
        nats_client: Any = None,
    ) -> List[str]:
        """Stop every execution still working on a merged or closed PR (#1032).

        Runs for every normalized ``pull_request_merged``/``_closed`` and
        ``merge_request_merged``/``_closed`` delivery, whether or not a flow
        subscribes to it. Covers every flow in the account and every status
        that still holds the request (queued, running, parked). Left alone:
        executions not bound to this request, and executions of flows that
        trigger on the merge or close themselves.

        Args:
            event_data: The normalized event.
            nats_client: Connected NATS client, fetched on demand when None.

        Returns:
            Ids of the executions this call stopped. Empty, and nothing
            written, when no execution is bound to the request.
        """
        stop_source = pr_close_stop_source(event_data.get("type"))
        account_id = event_data.get("account_id")
        if not stop_source or not account_id:
            return []
        object_key = self._extract_pr_object_key(event_data)
        if not object_key:
            return []
        candidates = crud_flow_execution.get_active_for_pull_request(
            self.db,
            account_id=uuid.UUID(str(account_id)),
            tracker_object_key=object_key,
            statuses=TRACKER_OBJECT_ACTIVE_STATUSES,
        )
        described = _describe_pr_object(object_key)
        outcome_text = (
            "was merged"
            if stop_source == PR_STOP_SOURCE_MERGED
            else "was closed without merging"
        )
        reason = f"Stopped because {described} {outcome_text}"
        stopped: List[str] = []
        for execution in candidates:
            exec_event = self._execution_event_data(execution)
            if self._extract_pr_object_key(exec_event) != object_key:
                continue
            flow = execution.flow
            if flow is None or flow_handles_pr_close(flow):
                continue
            if exec_event.get("type") in PR_CLOSE_STOP_SOURCES:
                continue
            if nats_client is None:
                try:
                    nats_client = await get_nats_client()
                except Exception:  # noqa: BLE001 - the stop is durable without it
                    logger.warning("No NATS client for the pull request stop")
            try:
                if await self._stop_for_pull_request(
                    execution,
                    flow=flow,
                    object_key=object_key,
                    stop_source=stop_source,
                    reason=reason,
                    event_data=event_data,
                    nats_client=nats_client,
                ):
                    stopped.append(str(execution.id))
            except Exception:
                logger.exception(
                    "Could not stop execution %s for %s", execution.id, object_key
                )
                self._rollback_quietly()
        return stopped

    @staticmethod
    def _pull_request_version(event_data: Dict[str, Any]) -> Optional[int]:
        """Data Center pull request ``version`` carried by an event, if any."""
        if str(event_data.get("source") or "").lower() != "bitbucket_dc":
            return None
        pr = (event_data.get("payload") or {}).get("pull_request")
        version = pr.get("version") if isinstance(pr, dict) else None
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        return version

    def _is_out_of_order_head(self, flow: Flow, event_data: Dict[str, Any]) -> bool:
        """True when a Data Center PR update is older than one already seen.

        Data Center increments the pull request ``version`` on every change,
        and deliveries are not ordered. A late delivery for an older state
        must neither supersede the run on the newer head nor start a review
        of the stale one, whether the newer run is still active or has
        already finished.
        """
        version = self._pull_request_version(event_data)
        if version is None:
            return False
        object_key = self._extract_pr_object_key(event_data)
        if not object_key:
            return False
        recent = crud_flow_execution.get_recent_for_pull_request(
            self.db,
            flow_id=flow.id,
            account_id=flow.account_id,
            tracker_object_key=object_key,
        )
        for execution in recent:
            exec_event = self._execution_event_data(execution)
            if self._extract_pr_object_key(exec_event) != object_key:
                continue
            active_version = self._pull_request_version(exec_event)
            if active_version is not None and active_version > version:
                return True
        return False

    async def supersede_older_heads(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        *,
        commit_sha: str,
        nats_client: Any = None,
    ) -> List[str]:
        """Stop this flow's runs on an older head of the same PR (#1032).

        Called for a ``pull_request_updated``/``merge_request_updated``
        delivery on a flow with ``webhook_config.supersede_on_update`` set,
        before the execution for the new head is created. A run whose own
        head is unknown or equals ``commit_sha`` is left alone, and so is a
        comment or CI resume: those feed the run more input, they do not
        review a head.

        Returns:
            Ids of the executions this call stopped.
        """
        object_key = self._extract_pr_object_key(event_data)
        if not object_key or not commit_sha:
            return []
        actives = crud_flow_execution.get_running_by_flow(
            self.db,
            flow_id=flow.id,
            account_id=flow.account_id,
            running_statuses=list(TRACKER_OBJECT_ACTIVE_STATUSES),
            tracker_object_key=object_key,
        )
        described = _describe_pr_object(object_key)
        stopped: List[str] = []
        for execution in actives:
            exec_event = self._execution_event_data(execution)
            if self._extract_pr_object_key(exec_event) != object_key:
                continue
            if exec_event.get("type") in COALESCE_EXEMPT_EVENT_TYPES:
                continue
            old_sha = self._extract_commit_sha(exec_event)
            if not old_sha or old_sha == commit_sha:
                continue
            reason = (
                f"Stopped because {described} got a new head "
                f"{_short(commit_sha)}; this run on {_short(old_sha)} was superseded"
            )
            try:
                if await self._stop_for_pull_request(
                    execution,
                    flow=flow,
                    object_key=object_key,
                    stop_source=PR_STOP_SOURCE_SUPERSEDED,
                    reason=reason,
                    event_data=event_data,
                    nats_client=nats_client,
                ):
                    stopped.append(str(execution.id))
            except Exception:
                logger.exception(
                    "Could not stop superseded execution %s for %s",
                    execution.id,
                    object_key,
                )
                self._rollback_quietly()
        return stopped

    def _rollback_quietly(self) -> None:
        """Leave the session usable after a failed stop; triggering goes on."""
        try:
            self.db.rollback()
        except Exception:  # noqa: BLE001 - nothing more to undo
            logger.debug("Rollback after a failed pull request stop failed")

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

        elif source == "bitbucket":
            repo = payload.get("repository") or {}
            repo_full_name = repo.get("full_name", "") if isinstance(repo, dict) else ""
            if repo_full_name:
                return f"bitbucket:{repo_full_name}"

        elif source == "bitbucket_dc":
            repo = payload.get("repository") or {}
            if isinstance(repo, dict) and repo.get("id"):
                return f"bitbucket_dc:{repo['id']}"

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

        elif source == "bitbucket":
            repo = payload.get("repository", {})
            # Bitbucket uses full_name like "workspace/repo"; projects store
            # the repository UUID without braces as the identifier.
            repo_identifier = repo.get("full_name") or repo.get("name")
            repo_external_id = normalize_bitbucket_uuid(repo.get("uuid")) or None

        elif source == "jira":
            return self._extract_jira_project_id(payload, tracker_id)

        elif source == "bitbucket_dc":
            # Data Center projects store the immutable repository id as the
            # identifier; a slug or display name never selects the project.
            repo = payload.get("repository") or {}
            repo_id = repo.get("id") if isinstance(repo, dict) else None
            if not repo_id:
                return None
            project = crud_project.get_for_tracker_by_identifier(
                self.db, tracker_id=tracker_id, identifier=str(repo_id)
            )
            return str(project.id) if project else None

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

    def _extract_jira_project_id(
        self, payload: Dict[str, Any], tracker_id: str
    ) -> Optional[str]:
        """Resolve the synced Jira project an issue webhook belongs to.

        Jira sync stores the project key as the slug (older rows used it as
        the identifier). Only the key and the numeric project id are
        compared: matching a Jira key against project display names could
        pick an unrelated project.

        Args:
            payload: Jira webhook payload.
            tracker_id: Jira tracker the webhook arrived on.

        Returns:
            Internal project UUID as a string, or None.
        """
        from preloop.models.crud import crud_project

        issue = payload.get("issue") if isinstance(payload, dict) else None
        fields = issue.get("fields") if isinstance(issue, dict) else None
        project = fields.get("project") if isinstance(fields, dict) else None
        if not isinstance(project, dict):
            return None
        key = str(project.get("key") or "").strip()
        external_id = str(project.get("id") or "").strip()
        if not key and not external_id:
            return None
        found = crud_project.get_for_tracker_by_key(
            self.db, tracker_id=tracker_id, key=key, external_id=external_id
        )
        if found is not None:
            return str(found.id)
        logger.debug(
            "Could not match Jira project %s (id=%s) for tracker %s",
            key,
            external_id,
            tracker_id,
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

        # Bitbucket Cloud PR or repo:push
        sha = bitbucket_payload_commit_hash(payload)
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
        """Return a running execution of ``flow`` for the same identity as
        ``event_data``, or None.

        Used by direct-trigger callers (e.g. the webhook endpoint) to preserve
        the commit-SHA deduplication that generic event matching
        (``process_event``) enforces, without silently dropping the event:
        callers can return the existing execution to the caller instead.

        Scope: dedup applies when the payload carries a recognizable commit
        SHA (see ``_extract_commit_sha``), or — as a fallback for
        commit-less deliveries such as GlitchTip alerts and release events —
        when a resource key can be extracted (see
        ``_fallback_dedupe_resource_key``). Payloads with neither identity
        are never deduplicated. The check-then-insert is also not atomic — a
        DB-level guard would be needed to close the race for concurrent
        identical deliveries.
        """
        commit_sha = self._extract_commit_sha(event_data)
        if commit_sha:
            repo_key = self._extract_repo_key(event_data)
            return self._find_running_execution_for_commit(
                flow.id,
                commit_sha,
                str(flow.account_id),
                repo_key=repo_key,
            )
        resource_key = self._fallback_dedupe_resource_key(flow, event_data)
        if resource_key:
            return self._find_running_execution_for_resource_key(
                flow, resource_key, str(flow.account_id)
            )
        return None

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
        source_execution: Optional[FlowExecution] = None,
    ) -> Any:
        """Create (or reuse) a PENDING execution and hand it to a worker or local task.

        When ``FLOW_EXECUTION_WORKER_ENABLED`` is true, publishes ``execute_flow``.
        Otherwise falls back to ``asyncio.create_task`` in-process.

        Raises:
            FlowHaltActiveError: The account kill switch currently halts new
                flow executions (#157). No execution row is created and
                nothing is dispatched.
        """
        from preloop.services.flow_execution_dispatcher import (
            dispatch_execute,
            flow_execution_worker_enabled,
        )
        from preloop.services.flow_execution_runner import run_existing_execution
        from preloop.services.flow_orchestrator import _make_json_serializable
        from preloop.services.issue_triage_controller import (
            is_triage_flow,
            reserve_triage_execution,
        )

        if self._flows_halted(flow.account_id):
            if precreated_execution is None:
                raise FlowHaltActiveError(
                    f"Flow '{flow.name}' ({flow.id}) not started: the account "
                    "kill switch currently halts new flow executions"
                )
            # A pre-created execution stays PENDING: workers refuse to claim
            # it while the halt is active, and the recovery loop re-dispatches
            # it once the scope is lifted.
            logger.info(
                "Execution %s left PENDING: account kill switch halts new "
                "flow executions",
                precreated_execution.id,
            )
            return precreated_execution

        if precreated_execution is None:
            event_data = dict(event_data or {})
            for key in ("lifecycle_pickup", "triage_context", "triage_packet"):
                event_data.pop(key, None)
            if is_triage_flow(self.db, flow):
                event_data = prepare_execution_routing(
                    self.db,
                    flow,
                    _make_json_serializable(event_data),
                    source_execution=source_execution,
                    pin_kind="continuation" if source_execution is not None else None,
                )
                if test_mode:
                    event_data["test_mode"] = True
                from preloop.services.flow_feedback import feedback_policy

                event_data.pop("_session_thread_id", None)
                event_data.pop("_thread_id", None)
                if feedback_policy(flow):
                    event_data["_session_thread_id"] = str(uuid.uuid4())
                attach_trigger_subject(event_data)
                attach_workspace_file_paths(event_data)
                precreated_execution, _ = await reserve_triage_execution(
                    self.db,
                    flow=flow,
                    event=event_data,
                    retry_of_execution_id=retry_of_execution_id,
                )
                event_data = precreated_execution.trigger_event_details
                if precreated_execution.status != "PENDING":
                    return precreated_execution

        if precreated_execution is None:
            from preloop.services.issue_lifecycle_runtime import lifecycle_flow_entry
            from preloop.services.issue_lifecycle_worker import lifecycle_entry_decision

            event = event_data if isinstance(event_data, dict) else {}
            skip_entry = (
                isinstance(self.db, Session)
                and not lifecycle_entry_decision(self.db, flow, event).engaged
            )
            if not skip_entry:
                handled, lifecycle_execution = await lifecycle_flow_entry(
                    self, flow, event_data, nats_client
                )
                if handled:
                    return lifecycle_execution

        if precreated_execution is not None:
            execution = precreated_execution
            execution_id = execution.id
        else:
            trigger_details = _make_json_serializable(
                dict(event_data) if event_data else {}
            )
            trigger_details = prepare_execution_routing(
                self.db,
                flow,
                trigger_details,
                source_execution=source_execution,
                pin_kind="continuation" if source_execution is not None else None,
            )
            if test_mode:
                trigger_details["test_mode"] = True
            from preloop.services.flow_feedback import feedback_policy

            trigger_details.pop("_session_thread_id", None)
            trigger_details.pop("_thread_id", None)
            if feedback_policy(flow):
                trigger_details["_session_thread_id"] = str(uuid.uuid4())
            event_data = trigger_details
            attach_trigger_subject(trigger_details)
            attach_workspace_file_paths(trigger_details)
            execution_data = FlowExecutionCreate(
                flow_id=flow.id
                if isinstance(flow.id, uuid.UUID)
                else uuid.UUID(str(flow.id)),
                status="PENDING",
                trigger_event_details=trigger_details,
                retry_of_execution_id=retry_of_execution_id,
            )
            # A retry is a deliberate second run of the same delivery, so it
            # must not claim the delivery key (nor collide with the original).
            delivery_key = (
                None
                if retry_of_execution_id is not None
                else delivery_key_for_event(event_data)
            )
            try:
                execution = crud_flow_execution.create(self.db, obj_in=execution_data)
                if delivery_key:
                    execution.webhook_delivery_key = delivery_key
                self.db.commit()
            except IntegrityError as e:
                # Lost a race with another worker holding the same redelivered
                # message: the partial unique index on
                # (flow_id, webhook_delivery_key) refused the second row.
                # Return the row that won; dispatching again is what created
                # duplicate pull requests in the first place. Other integrity
                # failures (NOT NULL, FK, a different unique index) re-raise.
                self.db.rollback()
                if not delivery_key or not is_delivery_key_conflict(e):
                    raise
                existing = find_execution_for_delivery(
                    self.db, flow_id=flow.id, delivery_key=delivery_key
                )
                if existing is None:
                    raise
                logger.warning(
                    "Flow %s: delivery %s already has execution %s (unique "
                    "index); not creating or dispatching a second one",
                    flow.id,
                    delivery_key,
                    existing.id,
                )
                return existing
            self.db.refresh(execution)
            execution_id = execution.id
            logger.info("Created flow execution: %s", execution_id)

        async def _local_run() -> None:
            from preloop.models.crud import crud_issue_lifecycle
            from preloop.services.flow_execution_runner import claim_and_run_execution

            orchestrator_db = self._create_orchestrator_session()
            try:
                if crud_issue_lifecycle.has_triage_execution(
                    orchestrator_db, execution_id=execution_id
                ):
                    # A repeated PENDING delivery may queue this callback again.
                    # Use the worker's exclusive durable claim before starting
                    # any local triage orchestration, just as broker delivery does.
                    orchestrator_db.close()
                    await claim_and_run_execution(str(execution_id))
                    return
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
            local_task = asyncio.create_task(_local_run())
            _LOCAL_RUN_TASKS.add(local_task)
            local_task.add_done_callback(
                functools.partial(
                    _supervise_local_run,
                    execution_id=execution_id,
                    session_factory=self._create_orchestrator_session,
                )
            )

        return execution

    def _labels_to_match(
        self,
        flow: Flow,
        event_data: Dict[str, Any],
        payload: Dict[str, Any],
        key: str,
    ) -> Any:
        """The label names a ``labels`` condition is tested against.

        For a labeled/unlabeled delivery this is the label the event carries,
        so a filter reads "this event added ``agent-ready``" instead of "the
        issue has ``agent-ready``". Every other event type keeps the object's
        label list, which is the right reading for issue-opened and
        merge-request conditions.

        A label event that carries no label name at all falls back to the
        list. Dropping it instead would silently stop a working flow on any
        payload shape this does not know about; the per-object coalescing
        guard bounds what that fallback can cost.
        """
        if _is_label_change_event(event_data):
            carried = _event_label_names(payload)
            if carried:
                return carried
            logger.info(
                "Flow %s: %s event carries no label name; falling back to the "
                "object's label list for the 'labels' condition",
                flow.id,
                event_data.get("type"),
            )
        return _label_names_from_payload(payload) or payload.get(key)

    def _matches_trigger_config(self, flow: Flow, event_data: Dict[str, Any]) -> bool:
        """
        Check if the event matches the flow's trigger_config (if specified).

        Two label conditions combine with AND:

        * ``labels`` (any-of): "this event added one of". On a label-change
          delivery it reads the label the event carries; otherwise the
          object's label list.
        * ``labels_all`` (all-of): "and the issue carries all of". Always read
          from the object's current label list (after the change), so
          ``{"labels": ["agent-ready"], "labels_all": ["complexity:low"]}``
          routes one ``agent-ready`` delivery to the complexity:low flow only.
          An event with no label list never satisfies ``labels_all``.

        A bound implementation comment (its PR need not repeat intake
        labels) skips both label conditions.

        Args:
            flow: The flow definition
            event_data: The event data containing payload and metadata

        Returns:
            True if the event matches the trigger config, False otherwise
        """
        # A backport flow (issue #961) starts only for a merge into its
        # configured source branch; a merge anywhere else never starts it.
        from preloop.services.backport import backport_event_matches

        if not backport_event_matches(
            getattr(flow, "git_clone_config", None), event_data
        ):
            logger.info(
                "Flow %s: backport gate did not match (not a merge into the "
                "source branch, or an invalid backport block)",
                flow.id,
            )
            return False

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

        bound_cache: List[bool] = []

        def _bound_comment() -> bool:
            # Shared by both label conditions so the bound-execution lookup
            # runs at most once per event.
            if not bound_cache:
                from preloop.services.flow_pr_binding import (
                    is_bound_implementation_comment,
                )

                bound_cache.append(
                    bool(is_bound_implementation_comment(self.db, flow, event_data))
                )
            return bound_cache[0]

        for key, expected_value in flattened_config.items():
            if key == "labels_all":
                required = (
                    expected_value
                    if isinstance(expected_value, list)
                    else [expected_value]
                )
                required = [r for r in required if isinstance(r, str) and r]
                if not required:
                    continue
                if _bound_comment():
                    continue
                present = _object_label_names(payload)
                missing = [r for r in required if r not in present]
                if missing:
                    logger.debug(
                        f"Flow {flow.id} trigger_config mismatch: "
                        f"labels_all missing {missing} (issue has {present})"
                    )
                    return False
                continue
            if key == "labels":
                if _bound_comment():
                    # The issue qualified at intake; its PR need not duplicate
                    # that label. Every other configured condition still applies.
                    continue
                actual_value = self._labels_to_match(flow, event_data, payload, key)
            else:
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

    # Event types that are safe to accept even when the sender is a Preloop
    # bot account.  These represent *intentional actions* (opening a PR,
    # reopening one) rather than side-effects of a prior flow execution
    # (posting a comment, changing a status, editing a body).  A reviewer
    # flow triggered by a bot-opened PR will post comments or statuses --
    # those downstream events ARE guarded, so recursion cannot happen.
    _LOOP_GUARD_EXEMPT_EVENT_TYPES: frozenset = frozenset(
        {
            # Canonical names from normalize_event_type (sync/tasks.py).
            "pull_request_opened",
            "pull_request_reopened",
            "merge_request_opened",
            "merge_request_reopened",
        }
    )

    # Exact bot identities that Preloop controls.  We intentionally avoid
    # prefix matching ("preloop*") because that would false-positive on
    # legitimate human usernames like "preloop-fan".
    _KNOWN_BOT_IDENTITIES: frozenset = frozenset(
        {
            "preloop",
            "preloop-bot",
            "preloop-staging",
            "preloop-dev",
            "preloop[bot]",  # GitHub App format
            "preloop-app",
        }
    )

    def _is_preloop_triggered_event(self, event_data: Dict[str, Any]) -> bool:
        """Check if an event was triggered by Preloop's own actions.

        This prevents infinite loops where:
        1. Flow runs and updates a PR (adds comment, modifies body, etc.)
        2. Update triggers a new webhook event (pull_request_updated,
           comment_created)
        3. Event matches another flow and triggers another execution
        4. Repeat forever

        Two categories of events are allowed through even when the sender
        is a known Preloop bot:

        * **Label events** (issue_labeled / issue_unlabeled) -- these are
          the hand-off hop from an intake/dispatch flow onto an
          implementation flow.
        * **PR/MR opened/reopened events** -- creating or reopening a PR
          is an intentional action (e.g. Preloop opens a PR on a human's
          behalf).  The resulting review flow will post comments or
          statuses, which are *reaction-type* events that remain guarded,
          so unbounded recursion cannot occur.
        """
        payload = event_data.get("payload", {})
        source = event_data.get("source", "").lower()
        event_type = event_data.get("type") or ""

        # Label events are the hop from intake/dispatch onto an
        # implementation flow.  If we skip Preloop-bot label events, then
        # update_issue(labels=["agent-ready"]) can never start a fixer.
        if event_type in {"issue_labeled", "issue_unlabeled"}:
            return False

        # PR/MR opened/reopened events carry real code changes and are
        # legitimate triggers even when the PR was opened by the App on
        # a human's behalf.  See class docstring for loop-safety argument.
        if event_type in self._LOOP_GUARD_EXEMPT_EVENT_TYPES:
            return False

        # Reviewer comments are the hand-off hop back onto the implementer
        # flow, and the reviewer posts as the Preloop bot.  Only comments
        # that carry the reviewer's own marker
        # (``<!-- preloop-review:flow-id:... -->``) are let through; every
        # other bot comment stays dropped, so a chatty bot cannot restart a
        # flow.  Self-loops and runaway resumes are guarded separately in
        # bind_resume_or_skip (marker flow id, max_resumes_per_pr).
        if event_type == "comment_created":
            from preloop.services.flow_pr_binding import (
                extract_comment_body,
                parse_review_marker,
            )

            marker_flow_id = parse_review_marker(extract_comment_body(event_data))
            if marker_flow_id:
                logger.info(
                    "Allowing marked review comment from flow id %s past the "
                    "bot filter",
                    marker_flow_id,
                )
                return False

        # Get the sender/actor who triggered the event.
        # Note: payload may be enriched with filter_fields which can
        # overwrite dict values (e.g. sender) with strings, so handle
        # both types.
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
        elif source == "bitbucket_dc":
            # The intake compares the actor with the user Preloop acts as on
            # that instance; the configured user slug is not a fixed bot name.
            dc = payload.get("bitbucket_dc") or {}
            if isinstance(dc, dict) and dc.get("self_generated") is True:
                logger.info("Ignoring Bitbucket Data Center event sent by Preloop")
                return True
            sender_obj = payload.get("sender")
            if isinstance(sender_obj, str):
                sender = sender_obj.lower()
        elif source == "bitbucket":
            # Bitbucket names the acting user "actor"; filter_fields adds the
            # nickname as "sender".
            actor = payload.get("actor")
            sender_obj = payload.get("sender")
            if isinstance(sender_obj, str) and sender_obj:
                sender = sender_obj.lower()
            elif isinstance(actor, dict):
                sender = str(
                    actor.get("nickname") or actor.get("display_name") or ""
                ).lower()

        if not sender:
            return False

        # Match against exact known bot identities only -- never use a
        # prefix/startswith check, which would incorrectly drop events
        # from legitimate users whose names happen to start with
        # "preloop" (e.g. "preloop-fan").
        if sender in self._KNOWN_BOT_IDENTITIES:
            logger.info(f"Ignoring event triggered by Preloop bot account: {sender}")
            return True

        return False

    def _is_triage_self_update(self, event_data: Dict[str, Any]) -> bool:
        """Match a complete issue snapshot to trusted triage write receipts.

        The decision keys on the server-written receipt, not on which tools a
        flow selected: the receipt is the only evidence that Preloop itself
        produced this exact issue content. PAT-backed writes may have a human
        sender, and marker text alone never establishes a self-update. Pending
        exact snapshots expire; a verified final snapshot may persist.
        """
        if event_data.get("type") != "issue_updated":
            return False
        account_id = event_data.get("account_id")
        tracker_id = event_data.get("tracker_id")
        if not account_id or not tracker_id:
            return False
        payload = event_data.get("payload")
        if not isinstance(payload, dict):
            return False
        source = event_data.get("source")
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            return False
        if source == "github":
            if payload.get("action") != "edited" or not set(changes) <= {
                "title",
                "body",
            }:
                return False
            subject = payload.get("issue")
            body_key, url_key = "body", "html_url"
        elif source == "gitlab":
            if payload.get("object_kind") != "issue":
                return False
            subject = payload.get("object_attributes")
            body_key, url_key = "description", "url"
            substantive = {"title", "description", "labels"}
            if (
                not isinstance(subject, dict)
                or subject.get("action") != "update"
                or not set(changes) & substantive
                or not set(changes) <= substantive | {"updated_at", "updated_by_id"}
            ):
                return False
        else:
            return False
        if not isinstance(subject, dict) or body_key not in subject:
            return False
        title, body = subject.get("title"), subject.get(body_key)
        url, state = subject.get(url_key), subject.get("state")
        if body is None:
            body = ""
        if not all(isinstance(value, str) for value in (title, body, url, state)):
            return False
        labels = subject.get("labels", payload.get("labels"))
        if not isinstance(labels, list):
            return False
        names: List[str] = []
        for label in labels:
            name = _label_name(label)
            if name is None:
                return False
            names.append(name)
        issue = crud_issue.get_by_external_url(
            self.db, external_url=url, account_id=str(account_id)
        )
        if issue is None or str(issue.tracker_id) != str(tracker_id):
            return False
        if event_data.get("project_id") and str(issue.project_id) != str(
            event_data["project_id"]
        ):
            return False
        receipt = (issue.meta_data or {}).get("preloop_triage")
        if not isinstance(receipt, dict):
            return False
        revision = provider_revision(
            title, body, names, "open" if state == "opened" else state
        )
        observed_time = _triage_timestamp(subject.get("updated_at"))
        recorded_time = _triage_timestamp(receipt.get("provider_updated_at"))
        if revision == receipt.get("provider_revision"):
            # A later human event can return to identical content. Do not
            # fall back to pending intent when its verified time differs.
            return observed_time is not None and observed_time == recorded_time
        expected = receipt.get("expected_revisions")
        expires_at = receipt.get("expires_at")
        if (
            not isinstance(expected, list)
            or len(expected) > 8
            or revision not in expected
            or not isinstance(expires_at, str)
        ):
            return False
        expiry = _triage_timestamp(expires_at)
        return expiry is not None and expiry > datetime.now(timezone.utc)

    def _add_secondary_event_flows(
        self,
        event_data: Dict[str, Any],
        matching_flows: List[Flow],
        *,
        query_source: Any,
        project_id: Optional[str],
        account_id: Any,
    ) -> Tuple[List[Flow], Dict[Any, str]]:
        """Append flows subscribed to a secondary type of this delivery.

        See ``secondary_event_types``: one Jira edit can add a label, change
        the status and remove a label, and it is still an issue update.
        Flows subscribed to any of those types are considered. Each flow
        appears once, under the first type it matched (primary first).

        A secondary type the loop guard would drop is not expanded: a label
        edit by the bot passes the guard as ``issue_labeled``, but the same
        edit must not start ``issue_updated`` or status flows.

        Args:
            event_data: The event being processed.
            matching_flows: Flows matched on the primary event type.
            query_source: Tracker id or source used for the primary lookup.
            project_id: Project used for the primary lookup.
            account_id: Account scope.

        Returns:
            The primary flows followed by any additional ones, and the event
            type each secondary flow matched on (keyed by flow id).
        """
        from preloop.sync.event_normalizer import secondary_event_types

        extra_types = secondary_event_types(
            event_data.get("source"),
            event_data.get("type"),
            event_data.get("payload"),
        )
        flows = list(matching_flows)
        matched_types: Dict[Any, str] = {}
        if not extra_types:
            return flows, matched_types
        seen = {flow.id for flow in flows}
        for extra_type in extra_types:
            if self._is_preloop_triggered_event({**event_data, "type": extra_type}):
                logger.info(
                    "Not expanding %s delivery to %s: sent by the Preloop bot",
                    event_data.get("type"),
                    extra_type,
                )
                continue
            for flow in crud_flow.get_by_trigger(
                self.db,
                event_source=query_source,
                event_type=extra_type,
                project_id=project_id,
                account_id=account_id,
            ):
                if flow.id not in seen:
                    seen.add(flow.id)
                    flows.append(flow)
                    matched_types[flow.id] = extra_type
        return flows, matched_types

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

        # Durable feedback is routed by an existing account/repository binding,
        # independently from issue-intake labels and bot comment markers.
        from preloop.services.flow_feedback import (
            FEEDBACK_TYPES,
            feedback_policy,
            ingest_feedback,
        )

        ingest_feedback(self.db, event_data)

        # A merged or closed pull request ends every run still working on it
        # (#1032), before anything else: the flow lookup below returns early
        # when no flow subscribes to the merge, which is the common case, and
        # a merge done by the Preloop bot is still a merge.
        if pr_close_stop_source(event_type):
            try:
                await self.stop_executions_for_ended_pull_request(event_data)
            except Exception:
                logger.exception(
                    "Could not stop executions for %s event from %s",
                    event_type,
                    event_source,
                )
                self._rollback_quietly()

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
            matching_flows, secondary_types = self._add_secondary_event_flows(
                event_data,
                matching_flows,
                query_source=query_source,
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

            # The receipt describes the event, not a flow, so evaluate it once.
            triage_self_update = self._is_triage_self_update(event_data)
            if triage_self_update:
                logger.info("Event matches a recorded triage write receipt")

            # Relevance is a property of the delivery, so evaluate it once.
            # Only triage flows are held back by it (see issue_triage_trigger).
            triage_content_change = issue_update_touches_content(event_data)

            # Filter flows by trigger_config and enabled status
            flows_to_trigger = []
            for flow in matching_flows:
                # A flow found through a secondary type (Jira) is filtered as
                # that type, so a "labels" condition on an issue_updated flow
                # reads the issue's labels, not the delta.
                flow_event = event_data
                if flow.id in secondary_types:
                    flow_event = {**event_data, "type": secondary_types[flow.id]}
                flow_event_type = flow_event.get("type")
                if feedback_policy(flow) and flow_event_type in FEEDBACK_TYPES:
                    # This flow's durable subscription owns follow-up routing.
                    # Independent reviewer and ordinary event flows still run.
                    continue
                if not flow.is_enabled:
                    logger.warning(
                        f"Skipping disabled flow '{flow.name}' ({flow.id}). "
                        f"To enable this flow, set is_enabled=true via the API or UI."
                    )
                    continue

                if triage_self_update:
                    logger.info(
                        "Skipping flow %s for a recorded triage issue update",
                        flow.id,
                    )
                    continue

                if skip_triage_flow_for_event(
                    self.db,
                    flow,
                    flow_event,
                    event_touches_content=triage_content_change,
                ):
                    logger.info(
                        "Skipping triage flow %s: this issue update changed "
                        "neither the title nor the description, so triage has "
                        "nothing new to assess",
                        flow.id,
                    )
                    continue

                if not self._matches_trigger_config(flow, flow_event):
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

            # Delivery-level idempotency key for this event, independent of
            # any resource key. See preloop.services.webhook_delivery_dedupe.
            delivery_key = delivery_key_for_event(event_data)

            # Trigger each matching flow
            for flow in flows_to_trigger:
                try:
                    employee_binding = (flow.trigger_config or {}).get(
                        "employee_events"
                    )
                    if isinstance(employee_binding, dict):
                        from preloop.services.employee_events import (
                            ingest_employee_event,
                        )

                        subject = self._extract_resource_key(event_data)
                        if not subject:
                            raise ValueError(
                                "Employee tracker event is missing an owned subject"
                            )
                        # Created/merged objects have one lifecycle event. Providers
                        # lacking delivery IDs still get a stable replay identity.
                        event_id = (
                            event_data.get("delivery_id") or f"{event_type}:{subject}"
                        )
                        await ingest_employee_event(
                            self.db,
                            account_id=account_id,
                            flow_id=flow.id,
                            source=event_source,
                            connection_id=str(tracker_id or event_source),
                            event_id=str(event_id),
                            kind=event_type,
                            subject=subject,
                            payload=event_data.get("payload") or {},
                        )
                        continue
                    # One provider delivery, one execution per flow. At-least-
                    # once message delivery (a drained pod naks its in-flight
                    # message, ack_wait expires, a pod dies before acking)
                    # would otherwise replay a webhook that already produced
                    # an execution, which is how one `agent-ready` label
                    # produced two pull requests in production.
                    if delivery_key:
                        already = find_execution_for_delivery(
                            self.db, flow_id=flow.id, delivery_key=delivery_key
                        )
                        if already is not None:
                            logger.info(
                                "Skipping flow '%s' (%s): webhook delivery %s "
                                "already created execution %s (status %s). "
                                "Redelivery of the same message never creates "
                                "a second execution.",
                                flow.name,
                                flow.id,
                                delivery_key,
                                already.id,
                                already.status,
                            )
                            continue

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

                    # A new head on a pull request supersedes this flow's
                    # run on the older head when the flow opts in (#1032).
                    # Stopped before the new execution is created, and before
                    # the one-active-run guard below, which would otherwise
                    # keep the stale run and drop the new head.
                    if (
                        account_id
                        and event_type in PR_HEAD_UPDATE_EVENT_TYPES
                        and self._is_out_of_order_head(flow, event_data)
                    ):
                        logger.info(
                            "Skipping flow '%s' (%s): the pull request update "
                            "is older than the head an active run works on",
                            flow.name,
                            flow.id,
                        )
                        continue

                    if (
                        commit_sha
                        and account_id
                        and event_type in PR_HEAD_UPDATE_EVENT_TYPES
                        and flow_supersedes_on_update(flow)
                    ):
                        await self.supersede_older_heads(
                            flow,
                            event_data,
                            commit_sha=commit_sha,
                            nats_client=nats_client,
                        )

                    # Fallback dedup for commit-less deliveries (generic
                    # webhooks, release events): coalesce on a resource key.
                    # Issue/PR events are excluded so their existing behavior
                    # is unchanged (see issue #241).
                    if not commit_sha and account_id:
                        resource_key = self._fallback_dedupe_resource_key(
                            flow, event_data
                        )
                        if (
                            resource_key
                            and self._find_running_execution_for_resource_key(
                                flow, resource_key, account_id
                            )
                            is not None
                        ):
                            logger.info(
                                f"Skipping flow '{flow.name}' ({flow.id}) - "
                                f"already has a running execution for "
                                f"resource {resource_key}. This prevents "
                                f"duplicate executions when delivery retries "
                                f"re-send the same event."
                            )
                            continue

                    # One active execution per (flow, tracker object). Four
                    # issues produced 21 executions and three duplicate pull
                    # requests in production because nothing bounded the
                    # number of runs a single issue could start. Comment and
                    # CI deliveries are exempt: they are how a live run is
                    # fed more input, and they bind to it below.
                    from preloop.services.issue_triage_controller import is_triage_flow

                    if (
                        account_id
                        and event_type not in COALESCE_EXEMPT_EVENT_TYPES
                        and not is_triage_flow(self.db, flow)
                    ):
                        object_key = self._extract_tracker_object_key(event_data)
                        if object_key:
                            active = self._find_active_execution_for_tracker_object(
                                flow, object_key, account_id
                            )
                            if active is not None:
                                self.record_coalesced_trigger(
                                    flow, event_data, object_key, active
                                )
                                continue

                    logger.info(
                        f"Triggering flow '{flow.name}' ({flow.id}) for event {event_type}"
                    )
                    event_copy = dict(event_data)
                    source_execution = None
                    if event_type == "comment_created":
                        from preloop.services.flow_pr_binding import (
                            bind_resume_or_skip,
                            flow_requires_pr_comment_resume,
                        )

                        if flow_requires_pr_comment_resume(flow):
                            resume = bind_resume_or_skip(self.db, flow, event_copy)
                            if resume is None:
                                # No opened PR for this comment, the comment
                                # carries this flow's own review marker, the
                                # PR hit max_resumes_per_pr, or a run for it
                                # is still going and took the comment as its
                                # single queued follow-up. bind_resume_or_skip
                                # logs which one it was.
                                logger.info(
                                    "Skipping comment_created on flow '%s' (%s): "
                                    "no resume started for this comment",
                                    flow.name,
                                    flow.id,
                                )
                                continue
                            source_execution = load_source_execution_for_flow(
                                self.db, flow, resume.get("execution_id")
                            )
                            if source_execution is None:
                                logger.error(
                                    "Skipping comment_created on flow '%s' (%s): "
                                    "bound execution %s failed account/lineage checks",
                                    flow.name,
                                    flow.id,
                                    resume.get("execution_id"),
                                )
                                continue
                            logger.info(
                                "Resuming flow '%s' from execution %s on %s",
                                flow.name,
                                resume.get("execution_id"),
                                resume.get("pr_url"),
                            )
                    if (
                        event_type in GITHUB_CI_EVENT_TYPES
                        and flow_requires_ci_failure_resume(flow)
                    ):
                        ci_resume = bind_ci_failure_resume_or_skip(
                            self.db, flow, event_type, event_copy
                        )
                        if ci_resume is None:
                            logger.info(
                                "Skipping %s on flow '%s' (%s): no failing CI run "
                                "bound to a PR this flow opened, or the resume "
                                "cap was reached",
                                event_type,
                                flow.name,
                                flow.id,
                            )
                            continue
                        source_execution = load_source_execution_for_flow(
                            self.db, flow, ci_resume.get("execution_id")
                        )
                        if source_execution is None:
                            logger.error(
                                "Skipping %s on flow '%s' (%s): bound execution "
                                "%s failed account/lineage checks",
                                event_type,
                                flow.name,
                                flow.id,
                                ci_resume.get("execution_id"),
                            )
                            continue
                        logger.info(
                            "Resuming flow '%s' from execution %s after CI failure on %s",
                            flow.name,
                            ci_resume.get("execution_id"),
                            ci_resume.get("pr_url"),
                        )
                    await self._start_flow_execution(
                        flow=flow,
                        event_data=event_copy,
                        nats_client=nats_client,
                        source_execution=source_execution,
                    )
                    logger.info(f"Flow '{flow.name}' ({flow.id}) execution initiated")
                except ModelRoutingError:
                    if source_execution is not None:
                        crud_flow_execution.append_log(
                            self.db,
                            str(source_execution.id),
                            {
                                "type": "warning",
                                "message": "PR feedback continuation blocked: the original model/harness identity cannot be preserved. Start a new execution explicitly.",
                                "metadata": {
                                    "reason": "model_identity_unavailable",
                                    "event_type": event_type,
                                },
                            },
                        )
                    logger.warning(
                        "Model routing blocked event %s for flow %s",
                        event_type,
                        flow.id,
                        exc_info=True,
                    )
                except FlowHaltActiveError:
                    # The kill switch is not an error: record that the trigger
                    # was deliberately dropped (#157) and keep processing.
                    from preloop.models.crud import crud_event

                    logger.info(
                        "Trigger for flow '%s' (%s) dropped: account kill "
                        "switch halts new flow executions",
                        flow.name,
                        flow.id,
                    )
                    crud_event.log_event(
                        self.db,
                        event_type="flow_trigger_blocked_kill_switch",
                        account_id=flow.account_id,
                        event_data={
                            "flow_id": str(flow.id),
                            "flow_name": flow.name,
                            "trigger_source": event_data.get("source"),
                            "trigger_type": event_data.get("type"),
                        },
                    )
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

    async def run_scheduled_tick(self, flow_id: uuid.UUID | str) -> str:
        """
        Handle one tick of a schedule (cron) trigger for a flow.

        Overlap policy is skip-if-previous-running: if the flow already has
        an execution in a running state, the tick is skipped and recorded as
        a ``flow_schedule_tick_skipped`` audit event. Disabled (paused)
        flows never fire.

        Args:
            flow_id: The ID of the schedule-triggered flow.

        Returns:
            One of "triggered", "skipped_overlap", "suppressed_disabled",
            "suppressed_halted", or "not_scheduled".
        """
        from preloop.models.crud import crud_event

        flow = crud_flow.get(self.db, id=str(flow_id))
        if not flow or flow.trigger_event_source != "schedule":
            logger.warning(
                f"Scheduled tick for flow {flow_id} ignored - flow missing or "
                f"no longer schedule-triggered"
            )
            return "not_scheduled"

        if not flow.is_enabled:
            logger.info(
                f"Scheduled tick suppressed for disabled flow '{flow.name}' ({flow.id})"
            )
            return "suppressed_disabled"

        if flows_halted(self.db, flow.account_id):
            logger.info(
                f"Scheduled tick suppressed for flow '{flow.name}' ({flow.id}): "
                f"account kill switch halts new flow executions"
            )
            crud_event.log_event(
                self.db,
                event_type="flow_trigger_blocked_kill_switch",
                account_id=flow.account_id,
                event_data={
                    "flow_id": str(flow.id),
                    "flow_name": flow.name,
                    "trigger_source": "schedule",
                },
            )
            return "suppressed_halted"

        from preloop.models.schemas.flow import parse_schedule_config

        raw_schedule = flow.schedule_config or {}
        try:
            # Normalize legacy {"cron": ...} shapes into the typed union form
            schedule_config = parse_schedule_config(raw_schedule).model_dump()
        except Exception:
            schedule_config = raw_schedule
        now = _schedule_now()
        scheduled_at = now.isoformat()
        previous_scheduled_at = _previous_fire_iso(raw_schedule, now)

        running = crud_flow_execution.get_running_by_flow(self.db, flow_id=flow.id)
        if running:
            logger.info(
                f"Skipping scheduled tick for flow '{flow.name}' ({flow.id}) - "
                f"{len(running)} execution(s) still running (overlap policy: skip)"
            )
            crud_event.log_event(
                self.db,
                event_type="flow_schedule_tick_skipped",
                account_id=flow.account_id,
                event_data={
                    "flow_id": str(flow.id),
                    "flow_name": flow.name,
                    "reason": "previous_execution_running",
                    "running_execution_ids": [str(e.id) for e in running[:10]],
                    "schedule": schedule_config,
                    "timezone": schedule_config.get("timezone", "UTC"),
                    "scheduled_at": scheduled_at,
                    "previous_scheduled_at": previous_scheduled_at,
                },
            )
            return "skipped_overlap"

        # A schedule may carry a static payload (schedule_config.payload) for
        # options it has no other way to state, such as the
        # previous_result_execution_id a review subscription diffs against.
        # The schedule's own fields are written last: a stored config does
        # not get to rewrite when it fired.
        payload: Dict[str, Any] = dict(schedule_config.get("payload") or {})
        described_schedule = {
            key: value for key, value in schedule_config.items() if key != "payload"
        }
        payload.update(
            {
                "schedule": described_schedule,
                "timezone": schedule_config.get("timezone", "UTC"),
                "scheduled_at": scheduled_at,
                # The window since the previous fire of this schedule, from
                # its definition: a skipped or failed run still moves it, so
                # consecutive windows tile time. Catch-up is the run's call,
                # using last_successful_scheduled_at from history.
                "previous_scheduled_at": previous_scheduled_at,
                "window": {"from": previous_scheduled_at, "to": scheduled_at},
                "last_successful_scheduled_at": (
                    crud_flow_execution.last_successful_scheduled_at(
                        self.db, flow_id=flow.id
                    )
                ),
            }
        )
        event_data = {
            "source": "schedule",
            "type": "schedule",
            "account_id": str(flow.account_id) if flow.account_id else None,
            "payload": payload,
        }
        nats_client = await get_nats_client()
        await self._start_flow_execution(
            flow=flow,
            event_data=event_data,
            nats_client=nats_client,
        )
        logger.info(f"Scheduled execution initiated for flow '{flow.name}' ({flow.id})")
        return "triggered"

    async def trigger_flow(
        self,
        flow_id: uuid.UUID,
        test_mode: bool = False,
        trigger_event_data: Optional[Dict[str, Any]] = None,
        retry_of_execution_id: Optional[uuid.UUID] = None,
        triggered_by: Optional[str] = None,
        source_execution_id: Optional[uuid.UUID] = None,
        *,
        parent_execution_id: Optional[uuid.UUID] = None,
        root_execution_id: Optional[uuid.UUID] = None,
        delegation_depth: int = 0,
        batch_id: Optional[uuid.UUID] = None,
        no_progress_escalation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Manually trigger a flow execution for testing purposes or as a retry.

        Args:
            flow_id: The ID of the flow to trigger
            test_mode: Whether this is a test execution
            trigger_event_data: Optional custom trigger event data for testing
            retry_of_execution_id: If this is a retry, the ID of the original execution
            triggered_by: Who started this run, for the execution subject. A
                manual run has no repo and no reference, so the person is the
                only thing that tells two of them apart in the console list.
            source_execution_id: Controller-owned continuation of a persisted
                execution on this flow. Never read from the trigger body.
            parent_execution_id: Execution that started this one, for a
                delegated child (#630). Controller owned: resolved from the
                calling execution's own identity, never from a payload.
            root_execution_id: First execution of the delegation tree, NULL on
                a root run. Controller owned, as above.
            delegation_depth: Distance from the root of the tree, 0 for a run
                nobody delegated. Controller owned, as above.
            no_progress_escalation: ``{"ai_model_id", "reasoning_effort"}``
                for the one retry the no-progress guard creates (#851).
                Controller owned: the orchestrator reads it from the flow's
                own ``agent_config``, never from a payload. Only meaningful
                together with ``retry_of_execution_id``.
            batch_id: Group this execution belongs to, shared by the children
                of one delegated fan out (#631) exactly as a matrix trigger
                shares one across its cells, so the batch rollup endpoint
                reports the fan out as a unit. Controller owned, as above.

        Returns:
            Dict with execution_id and status
        """
        # Get the flow
        flow_id_str = str(flow_id)
        # Use CRUD layer without account filtering for test mode
        flow = crud_flow.get(self.db, id=flow_id_str)

        if not flow:
            raise ValueError(f"Flow {flow_id} not found")

        if flows_halted(self.db, flow.account_id):
            raise FlowHaltActiveError(
                f"Flow '{flow.name}' ({flow.id}) not started: the account "
                "kill switch currently halts new flow executions"
            )

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
        for key in ("lifecycle_pickup", "triage_context", "triage_packet"):
            trigger_details.pop(key, None)
        trigger_details["test_mode"] = test_mode
        if triggered_by:
            # Set after the copy so a retry is attributed to whoever retried,
            # not to whoever started the original run.
            trigger_details["triggered_by"] = triggered_by

        from preloop.services.flow_orchestrator import _make_json_serializable

        trigger_details = _make_json_serializable(trigger_details)
        from preloop.services.flow_feedback import feedback_policy

        trigger_details.pop("_session_thread_id", None)
        trigger_details.pop("_thread_id", None)
        if feedback_policy(flow):
            trigger_details["_session_thread_id"] = str(uuid.uuid4())
        pin_source_id = retry_of_execution_id or source_execution_id
        source_execution = None
        pin_kind = None
        if pin_source_id is not None:
            source_execution = load_source_execution_for_flow(
                self.db, flow, pin_source_id
            )
            if source_execution is None:
                raise ModelRoutingError("source execution not found for this flow")
            pin_kind = "retry" if retry_of_execution_id is not None else "continuation"
        trigger_details = prepare_execution_routing(
            self.db,
            flow,
            trigger_details,
            source_execution=source_execution,
            pin_kind=pin_kind,
        )
        if no_progress_escalation and retry_of_execution_id is not None:
            trigger_details = apply_no_progress_escalation(
                self.db,
                flow,
                trigger_details,
                escalation=no_progress_escalation,
                retry_of_execution_id=retry_of_execution_id,
            )
        attach_trigger_subject(trigger_details)
        attach_workspace_file_paths(trigger_details)

        execution_data = FlowExecutionCreate(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details=trigger_details,
            retry_of_execution_id=retry_of_execution_id,
            parent_execution_id=parent_execution_id,
            root_execution_id=root_execution_id,
            delegation_depth=delegation_depth,
            batch_id=batch_id,
        )

        from preloop.services.issue_triage_controller import (
            is_triage_flow,
            reserve_triage_execution,
        )

        coalesced = False
        triage_flow = is_triage_flow(self.db, flow)
        if triage_flow:
            if (
                parent_execution_id is not None
                or root_execution_id is not None
                or delegation_depth != 0
                or batch_id is not None
            ):
                from preloop.services.issue_triage_controller import (
                    TriageControllerError,
                )

                raise TriageControllerError(
                    "Triage revision claims cannot be reparented; use issue single or batch runs"
                )
            execution, coalesced = await reserve_triage_execution(
                self.db,
                flow=flow,
                event=trigger_details,
                retry_of_execution_id=retry_of_execution_id,
            )
            trigger_details = execution.trigger_event_details
        else:
            execution = crud_flow_execution.create(self.db, obj_in=execution_data)
            self.db.commit()
            self.db.refresh(execution)

        execution_id = execution.id
        execution_status = execution.status

        logger.info(f"Created flow execution: {execution_id}")

        if coalesced and execution_status != "PENDING":
            return {
                "id": str(execution_id),
                "status": execution_status,
                "flow_id": flow_id_str,
                "coalesced": True,
            }

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
            **({"coalesced": coalesced} if triage_flow else {}),
        }

    async def trigger_flow_matrix(
        self,
        flow_id: uuid.UUID,
        matrix: List[Dict[str, Any]],
        test_mode: bool = False,
        trigger_event_data: Optional[Dict[str, Any]] = None,
        triggered_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fan a single trigger out to one execution per matrix entry.

        Each entry may override ``agent_type`` and/or ``ai_model_id`` for its
        cell (an empty entry runs the flow defaults). All executions share a
        freshly minted ``batch_id``. All rows are created and committed before
        any cell is dispatched, so a dispatch failure mid-batch leaves visible
        PENDING rows rather than silently missing cells.

        Args:
            flow_id: The flow definition shared by all cells
            matrix: List of ``{"agent_type"?, "ai_model_id"?}`` overrides
            test_mode: Whether this is a test/manual trigger
            trigger_event_data: Optional trigger event data shared by all cells
            triggered_by: Who started the batch, for the execution subject

        Returns:
            Dict with batch_id, flow_id and per-cell execution references.

        Raises:
            ValueError: If the flow does not exist or the matrix is empty or
                exceeds MATRIX_MAX_ENTRIES. (Entry contents are validated at
                the API layer, where account scoping is known.)
        """
        if not matrix:
            raise ValueError("matrix must contain at least one entry")
        if len(matrix) > MATRIX_MAX_ENTRIES:
            raise ValueError(
                f"matrix supports at most {MATRIX_MAX_ENTRIES} entries, "
                f"got {len(matrix)}"
            )

        flow = crud_flow.get(self.db, id=str(flow_id))
        if not flow:
            raise ValueError(f"Flow {flow_id} not found")

        from preloop.services.issue_triage_controller import (
            TriageControllerError,
            is_triage_flow,
        )

        if is_triage_flow(self.db, flow):
            raise TriageControllerError(
                "Triage uses one revision claim per issue; use issue batches"
            )

        if flows_halted(self.db, flow.account_id):
            raise FlowHaltActiveError(
                f"Flow '{flow.name}' ({flow.id}) not started: the account "
                "kill switch currently halts new flow executions"
            )

        from preloop.services.flow_orchestrator import _make_json_serializable

        batch_id = uuid.uuid4()
        logger.info(
            "Triggering matrix batch %s for flow '%s' (%s) with %d cells",
            batch_id,
            flow.name,
            flow.id,
            len(matrix),
        )

        executions = []
        cells = []
        for index, entry in enumerate(matrix):
            trigger_details: Dict[str, Any] = {}
            if trigger_event_data:
                trigger_details.update(trigger_event_data)
            for key in ("lifecycle_pickup", "triage_context", "triage_packet"):
                trigger_details.pop(key, None)
            trigger_details["test_mode"] = test_mode
            if triggered_by:
                trigger_details["triggered_by"] = triggered_by
            trigger_details = _make_json_serializable(trigger_details)
            from preloop.services.flow_feedback import feedback_policy

            trigger_details.pop("_session_thread_id", None)
            trigger_details.pop("_thread_id", None)
            if feedback_policy(flow):
                trigger_details["_session_thread_id"] = str(uuid.uuid4())
            attach_trigger_subject(trigger_details)

            cell: Dict[str, Any] = {"batch_id": str(batch_id), "index": index}
            if entry.get("agent_type"):
                cell["agent_type"] = str(entry["agent_type"])
            if entry.get("ai_model_id"):
                cell["ai_model_id"] = str(entry["ai_model_id"])
            trigger_details = prepare_execution_routing(
                self.db,
                flow,
                trigger_details,
                authorized_matrix=cell,
            )
            cells.append(cell)

            execution_data = FlowExecutionCreate(
                flow_id=flow_id,
                status="PENDING",
                trigger_event_details=trigger_details,
                batch_id=batch_id,
            )
            executions.append(
                crud_flow_execution.create(self.db, obj_in=execution_data)
            )

        self.db.commit()
        for execution in executions:
            self.db.refresh(execution)

        nats_client = await get_nats_client()
        for execution in executions:
            await self._start_flow_execution(
                flow=flow,
                event_data=execution.trigger_event_details,
                nats_client=nats_client,
                precreated_execution=execution,
            )

        return {
            "batch_id": str(batch_id),
            "flow_id": str(flow_id),
            "executions": [
                {
                    "index": cell["index"],
                    "id": str(execution.id),
                    "execution_id": str(execution.id),
                    "status": execution.status,
                    "agent_type": cell.get("agent_type"),
                    "ai_model_id": cell.get("ai_model_id"),
                }
                for execution, cell in zip(executions, cells, strict=True)
            ],
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
