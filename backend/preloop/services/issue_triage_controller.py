"""Durable triage claims and authenticated assessment packets.

The existing issue lifecycle ledger owns identity and issue serialization. Triage
never authorizes implementation; readiness may consume its applicable evidence.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import json
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.api.common import get_tracker_client
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_issue_lifecycle
from preloop.schemas.issue_triage import IssueTriageApply, IssueTriageResult
from preloop.services.issue_triage import apply_triage, get_context, scope_revision
from preloop.services.issue_triage_provider import IssueTriageProvider
from preloop.sync.exceptions import TrackerError

VERSION = 1
MAX_PACKET_BYTES = 131072
TRIAGE_NAME = "Issue Triage Assistant"
ACTIVE_STATUSES = {
    "PENDING",
    "INITIALIZING",
    "STARTING",
    "RUNNING",
    "WAITING_FOR_HUMAN",
    "WAITING_FOR_CHILDREN",
    "PAUSED",
}
RETRYABLE_STATUSES = {"FAILED", "CANCELLED", "STOPPED", "TIMED_OUT", "ABORTED"}


class TriageControllerError(ValueError):
    """A rejected triage scope or revision, safe to report to its caller."""


@asynccontextmanager
async def _serialized(
    db: Session, account_id: UUID, issue_id: UUID
) -> AsyncIterator[None]:
    """Expose lock contention as a retryable controller outcome."""
    try:
        async with crud_issue_lifecycle.triage_locked(db, account_id, issue_id):
            yield
    except ValueError as exc:
        if str(exc) == "triage_operation_in_progress":
            raise TriageControllerError("triage_operation_in_progress") from exc
        raise


def require_scoped_triage_executor(flow: models.Flow) -> None:
    """Reject executors without an execution-bound triage credential.

    Persistent agents retain their own credential and tool policy. Sending
    them flow metadata cannot enforce this controller's scoped write boundary.
    Keep the same wrapped configuration interpretation as the agent factory.
    """
    from preloop.services.runner_service import unwrap_agent_config

    config = flow.agent_config
    sources = [config]
    if isinstance(config, dict):
        sources.append(config.get("agent_config"))
    for source in sources:
        unwrapped = unwrap_agent_config(source)
        if (
            isinstance(unwrapped, dict)
            and unwrapped.get("execution_path") == "persistent"
        ):
            raise TriageControllerError(
                "triage_persistent_executor_unsupported: use an ephemeral flow "
                "with an execution-bound credential"
            )


def fingerprint(value: Any) -> str:
    """Hash canonical JSON without timestamps or caller-provided identities."""
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def lifecycle_revision(title: str, body: str) -> str:
    """Match the existing readiness full-body revision without changing it."""
    return sha256(json.dumps([title, body], ensure_ascii=False).encode()).hexdigest()


def is_triage_flow(db: Session, flow: Any) -> bool:
    """Identify a saved triage flow; database errors fail closed."""
    if getattr(flow, "is_preset", False):
        return False
    source = getattr(flow, "source_preset_id", None)
    if source is None:
        return getattr(flow, "name", None) == TRIAGE_NAME
    preset = crud_flow.get_global_preset_by_name(db, name=TRIAGE_NAME)
    return preset is not None and str(source) == str(preset.id)


def is_triage_execution(
    db: Session, *, execution_id: str | UUID, account_id: str | UUID
) -> bool:
    """Classify authenticated execution credentials with durable provenance."""
    account_id, execution_id = UUID(str(account_id)), UUID(str(execution_id))
    execution = crud_flow_execution.get(db, id=execution_id, account_id=account_id)
    if execution is None:
        raise TriageControllerError("triage_execution_not_found")
    flow = crud_issue_lifecycle.triage_flow(
        db, flow_id=execution.flow_id, account_id=account_id
    )
    if flow is None:
        raise TriageControllerError("triage_flow_not_found")
    return crud_issue_lifecycle.triage_for_execution(
        db, account_id=account_id, execution_id=execution_id, include_attempts=True
    ) is not None or is_triage_flow(db, flow)


def policy_identity(project: models.Project) -> str:
    """Bind evidence to the existing authenticated project readiness policy."""
    return fingerprint(
        {
            "version": VERSION,
            "project_id": str(project.id),
            "policy": (project.settings or {}).get("issue_lifecycle") or {},
        }
    )


def context_identity(flow: models.Flow | None, project: models.Project) -> str:
    """Bind a packet to effective saved configuration, not only preset names."""
    config = (
        None
        if flow is None
        else {
            name: getattr(flow, name, None)
            for name in (
                "id",
                "prompt_template",
                "allowed_mcp_tools",
                "allowed_mcp_servers",
                "git_clone_config",
                "agent_config",
                "agent_type",
                "ai_model_id",
                "source_preset_id",
            )
        }
    )
    organization = project.organization
    tracker = organization.tracker
    scope = {
        "project_id": str(project.id),
        "name": project.name,
        "identifier": project.identifier,
        "tracker_settings": project.tracker_settings,
        "project_provider_id": (project.meta_data or {}).get("project_id"),
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "organization_identifier": organization.identifier,
        "tracker_id": str(tracker.id),
        "tracker_type": tracker.tracker_type,
        "tracker_url": tracker.url,
        "tracker_connection_details": tracker.connection_details,
    }
    return fingerprint(
        {
            "version": VERSION,
            "policy": policy_identity(project),
            "flow": config,
            "scope": scope,
        }
    )


async def authorized_provider(
    db: Session, *, issue: models.Issue, account_id: UUID, current_user: Any = None
) -> IssueTriageProvider:
    """Resolve provider authority from stored project/tracker ownership."""
    project = crud_issue_lifecycle.get_project(
        db, account_id=account_id, project_id=issue.project_id
    )
    if project is None:
        raise TriageControllerError("triage_project_not_found")
    actor = current_user or models.User(
        account_id=account_id, username="triage-controller"
    )
    client = await get_tracker_client(project.organization_id, project.id, db, actor)
    try:
        return IssueTriageProvider(
            client, str(issue.key or issue.external_id).rsplit("#", 1)[-1]
        )
    except ValueError as exc:
        raise TriageControllerError("triage_provider_scope_unsupported") from exc


async def reserve_triage_execution(
    db: Session,
    *,
    flow: models.Flow,
    event: dict[str, Any],
    retry_of_execution_id: UUID | None = None,
) -> tuple[models.FlowExecution, bool]:
    """Atomically reserve one execution per current issue/context revision."""
    require_scoped_triage_executor(flow)
    account_id = flow.account_id
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise TriageControllerError("triage_issue_payload_required")
    try:
        project_id = UUID(str(event.get("project_id") or payload.get("project_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TriageControllerError("triage_project_required") from exc
    project = crud_issue_lifecycle.get_project(
        db, account_id=account_id, project_id=project_id
    )
    if project is None:
        raise TriageControllerError("triage_project_not_found")
    subject = payload.get("issue") or payload.get("object_attributes") or {}
    if not isinstance(subject, dict):
        raise TriageControllerError("triage_issue_payload_required")
    issue = crud_issue_lifecycle.issue_target(
        db,
        account_id=account_id,
        project_id=project_id,
        external_id=str(subject["id"]) if subject.get("id") is not None else None,
        number=str(subject.get("number") or subject.get("iid") or "") or None,
    )
    if issue is None:
        raise TriageControllerError("triage_issue_not_synced")
    issue_id = issue.id
    async with _serialized(db, account_id, issue_id):
        flow = crud_issue_lifecycle.triage_flow(
            db, account_id=account_id, flow_id=flow.id
        )
        if flow is None or not flow.is_enabled or not is_triage_flow(db, flow):
            raise TriageControllerError("triage_flow_unavailable")
        require_scoped_triage_executor(flow)
        provider = await authorized_provider(db, issue=issue, account_id=account_id)
        try:
            async with asyncio.timeout(90):
                context = await get_context(provider)
        except (ValueError, TrackerError, TimeoutError) as exc:
            raise TriageControllerError("triage_context_unavailable") from exc
        identity = context_identity(flow, project)
        revision = fingerprint([context.expected_revision, identity])
        rows = crud_issue_lifecycle.list_for_issue(
            db, account_id=account_id, issue_id=issue_id
        )
        row = next(
            (
                item
                for item in reversed(rows)
                if item.kind == "triage"
                and item.data.get("context_identity") == identity
                and (
                    context.expected_revision
                    in {
                        item.data.get("source_revision"),
                        (item.data.get("packet") or {}).get("resulting_revision"),
                    }
                    or (
                        (
                            context.complexity_scheme.labels
                            if context.complexity_scheme
                            else []
                        )
                        == item.data.get("complexity_family", [])
                        and context.issue.revision
                        in (item.data.get("receipt") or {}).get(
                            "expected_revisions", []
                        )
                    )
                )
            ),
            None,
        )
        if row is not None:
            execution = crud_issue_lifecycle.pickup_execution(db, row=row)
            if execution is None or execution.flow_id != flow.id:
                raise TriageControllerError("triage_execution_missing")
            if retry_of_execution_id is None or row.state == "assessed":
                return execution, True
            if (
                str(execution.id) != str(retry_of_execution_id)
                or execution.status not in RETRYABLE_STATUSES
            ):
                raise TriageControllerError("triage_retry_requires_failed_execution")
            crud_issue_lifecycle.retry_triage(db, row=row)
            trusted = {
                **event,
                "triage_context": {
                    "issue_id": str(issue_id),
                    "revision": row.revision,
                    "recovery_request": row.data.get("pending_request"),
                    **{
                        key: value
                        for key, value in row.data.items()
                        if key not in {"receipt", "pending_request", "packet"}
                    },
                },
            }
            trusted.pop("lifecycle_pickup", None)
            trusted.pop("triage_packet", None)
            execution = crud_issue_lifecycle.create_execution(
                db,
                row=row,
                flow_id=flow.id,
                event=trusted,
                retry_of_execution_id=retry_of_execution_id,
            )
            crud_issue_lifecycle.commit(db)
            return execution, False
        data = {
            "version": VERSION,
            "project_id": str(project_id),
            "flow_id": str(flow.id),
            "source_revision": context.expected_revision,
            "source_provider_revision": context.issue.revision,
            "complexity_family": context.complexity_scheme.labels
            if context.complexity_scheme
            else [],
            "policy_identity": policy_identity(project),
            "context_identity": identity,
        }
        row = crud_issue_lifecycle.put(
            db,
            account_id=account_id,
            issue_id=issue_id,
            kind="triage",
            revision=revision,
            state="reserved",
            data=data,
        )
        trusted = dict(event)
        # Envelopes are never accepted from a browser, provider, or model.
        trusted.pop("lifecycle_pickup", None)
        trusted.pop("triage_packet", None)
        trusted["triage_context"] = {
            "issue_id": str(issue_id),
            "revision": revision,
            "recovery_request": None,
            **data,
        }
        execution = crud_issue_lifecycle.create_execution(
            db, row=row, flow_id=flow.id, event=trusted
        )
        crud_issue_lifecycle.commit(db)
        return execution, False


def applicable_triage_packet(
    db: Session, *, account_id: UUID, issue_id: UUID, lifecycle_revision: str
) -> dict[str, Any] | None:
    """Load only successful current evidence; caller retains dispatch authority."""
    issue = crud_issue_lifecycle.get_issue(db, account_id=account_id, issue_id=issue_id)
    if issue is None:
        return None
    project = crud_issue_lifecycle.get_project(
        db, account_id=account_id, project_id=issue.project_id
    )
    if project is None:
        return None
    rows = crud_issue_lifecycle.list_for_issue(
        db, account_id=account_id, issue_id=issue_id
    )
    for row in reversed(rows):
        packet = (
            row.data.get("packet")
            if row.kind == "triage" and row.state == "assessed"
            else None
        )
        if not isinstance(packet, dict) or packet.get("version") != VERSION:
            continue
        if any(
            packet.get(key) != value
            for key, value in {
                "account_id": str(account_id),
                "issue_id": str(issue_id),
                "project_id": str(project.id),
                "resulting_lifecycle_revision": lifecycle_revision,
                "policy_identity": policy_identity(project),
            }.items()
        ):
            continue
        flow = (
            crud_issue_lifecycle.triage_flow(
                db, flow_id=UUID(packet["flow_id"]), account_id=account_id
            )
            if packet.get("flow_id")
            else None
        )
        if (
            flow is None
            or not flow.is_enabled
            or not is_triage_flow(db, flow)
            or packet.get("context_identity") != context_identity(flow, project)
        ):
            continue
        if str(row.execution_id) != packet.get("execution_id"):
            continue
        return dict(packet)
    return None


async def apply_controlled_triage(
    db: Session,
    *,
    issue: models.Issue,
    provider: IssueTriageProvider,
    account_id: UUID,
    request: IssueTriageApply,
    execution_id: str | None,
    current_user: Any = None,
) -> IssueTriageResult:
    """Serialize applies and persist a packet only after verified provider/cache success."""
    issue_id = issue.id
    async with _serialized(db, account_id, issue_id):
        owned = crud_issue_lifecycle.get_issue(
            db, account_id=account_id, issue_id=issue_id
        )
        if owned is None:
            raise TriageControllerError("triage_issue_not_found")
        issue = owned
        project = crud_issue_lifecycle.get_project(
            db, account_id=account_id, project_id=issue.project_id
        )
        if project is None:
            raise TriageControllerError("triage_project_not_found")
        row = None
        if execution_id:
            row = crud_issue_lifecycle.triage_for_execution(
                db, account_id=account_id, execution_id=UUID(execution_id)
            )
            if row is None or row.issue_id != issue_id:
                raise TriageControllerError("triage_execution_issue_mismatch")
            flow = crud_issue_lifecycle.triage_flow(
                db, flow_id=UUID(row.data["flow_id"]), account_id=account_id
            )
            if (
                flow is None
                or not flow.is_enabled
                or context_identity(flow, project) != row.data.get("context_identity")
            ):
                raise TriageControllerError("triage_context_changed")
        provider = await authorized_provider(
            db, issue=issue, account_id=account_id, current_user=current_user
        )
        context = await get_context(provider)
        effective_request = request
        if row:
            if row.state == "assessed":
                prior = row.data.get("pending_request") or {}
                if request.expected_revision not in {
                    row.data["source_revision"],
                    context.expected_revision,
                } or any(
                    getattr(request, key) != prior.get(key)
                    for key in ("assessment", "title", "complexity_label")
                ):
                    raise TriageControllerError("triage_replay_request_mismatch")
                if (row.data.get("packet") or {}).get(
                    "resulting_revision"
                ) == context.expected_revision:
                    return IssueTriageResult(
                        status="unchanged", issue=context.issue, cache_updated=True
                    )
                raise TriageControllerError("triage_assessment_superseded")
            prior = row.data.get("pending_request")
            recovering = isinstance(prior, dict) and context.issue.revision in (
                row.data.get("receipt") or {}
            ).get("expected_revisions", [])
            if recovering:
                # A recorded partial provider effect can be completed, but the
                # caller cannot swap in different prose or classification.
                same = all(
                    getattr(request, key) == prior.get(key)
                    for key in ("assessment", "title", "complexity_label")
                )
                if not same or request.expected_revision not in {
                    row.data["source_revision"],
                    context.expected_revision,
                }:
                    raise TriageControllerError("triage_recovery_request_mismatch")
                effective_request = request.model_copy(
                    update={"expected_revision": context.expected_revision}
                )
            elif row.data.get("source_revision") != request.expected_revision:
                raise TriageControllerError("triage_execution_revision_mismatch")
            execution = crud_issue_lifecycle.pickup_execution(db, row=row)
            if execution is None or execution.status not in ACTIVE_STATUSES:
                raise TriageControllerError("triage_execution_not_active")
            row = crud_issue_lifecycle.put(
                db,
                account_id=account_id,
                issue_id=issue_id,
                kind="triage",
                revision=row.revision,
                state="applying",
                data={**row.data, "pending_request": request.model_dump()},
            )
            crud_issue_lifecycle.commit(db)

        def record(receipt: dict[str, Any]) -> None:
            crud_issue_lifecycle.triage_receipt(db, issue=issue, receipt=receipt)
            if row:
                crud_issue_lifecycle.put(
                    db,
                    account_id=account_id,
                    issue_id=issue_id,
                    kind="triage",
                    revision=row.revision,
                    state="applying",
                    data={**row.data, "receipt": receipt},
                )
            # The dedicated session lock survives this commit. Suppression intent
            # is durable before a provider response or process can be lost.
            crud_issue_lifecycle.commit(db)

        result = await apply_triage(provider, effective_request, record)
        if result.issue is not None:
            try:
                values: dict[str, Any] = {
                    "title": result.issue.title,
                    "description": result.issue.body,
                    "status": result.issue.state,
                    "labels": result.issue.labels,
                }
                if result.issue.updated_at:
                    try:
                        values["last_updated_external"] = datetime.fromisoformat(
                            result.issue.updated_at.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                crud_issue_lifecycle.triage_snapshot(db, issue=issue, values=values)
                result.cache_updated = True
            except SQLAlchemyError:
                crud_issue_lifecycle.rollback(db)
                result.status = "partial"
                result.reason = result.reason or "provider_result_cache_failed"
                result.next_action = "Refresh synchronization and inspect the durable receipt before retrying."
                return result
        if (
            row
            and result.status in {"updated", "unchanged"}
            and result.issue is not None
        ):
            packet = {
                "version": VERSION,
                "account_id": str(account_id),
                "project_id": str(project.id),
                "issue_id": str(issue_id),
                "flow_id": row.data["flow_id"],
                "execution_id": str(row.execution_id),
                "source_revision": row.data["source_revision"],
                "source_provider_revision": row.data["source_provider_revision"],
                "resulting_provider_revision": result.issue.revision,
                "resulting_revision": scope_revision(
                    result.issue, context.complexity_scheme
                ),
                "resulting_lifecycle_revision": lifecycle_revision(
                    result.issue.title, result.issue.body
                ),
                "policy_identity": row.data["policy_identity"],
                "context_identity": row.data["context_identity"],
                "assessment": request.assessment,
                "complexity_label": request.complexity_label,
                "complexity_family": context.complexity_scheme.labels
                if context.complexity_scheme
                else [],
                "evidence": {
                    "repository": "unknown",
                    "pull_requests": "unknown",
                    "label_catalogue": "complete",
                },
                "limitations": [
                    *context.limitations,
                    "Repository/source coverage is unknown to the controller.",
                    "Linked PR state and merged acceptance coverage are unknown to the controller.",
                ],
            }
            if len(json.dumps(packet, ensure_ascii=False).encode()) > MAX_PACKET_BYTES:
                result.status = "partial"
                result.reason = "triage_context_packet_too_large"
                result.next_action = "The issue was updated; reduce the project complexity catalogue before requesting a reusable packet."
            else:
                crud_issue_lifecycle.put(
                    db,
                    account_id=account_id,
                    issue_id=issue_id,
                    kind="triage",
                    revision=row.revision,
                    state="assessed",
                    data={**row.data, "packet": packet},
                )
        crud_issue_lifecycle.commit(db)
        return result
