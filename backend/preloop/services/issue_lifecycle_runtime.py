"""Production adapters and flow entry/terminal hooks for issue lifecycles."""

import logging
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.api.common import get_tracker_client
from preloop.models import models
from preloop.models.crud import crud_flow, crud_issue, crud_issue_lifecycle
from preloop.schemas.issue_lifecycle import ReadinessContract
from preloop.services.issue_lifecycle import IssueLifecycleService
from preloop.services.issue_lifecycle_provider import GitHubLifecycleProvider
from preloop.services.issue_lifecycle_worker import (
    lifecycle_worker_hook,
    on_application_loop,
)

logger = logging.getLogger(__name__)


class FlowEnvironmentCapabilities:
    """Resolve the selected implementation flow through environment service."""

    def __init__(self, db: Session, account_id: UUID, policy: dict[str, Any]) -> None:
        self.db, self.account_id, self.policy = db, account_id, policy

    async def blockers(self, profile: str, commands: list[str]) -> list[str]:
        """Reject missing harness/image/service command capabilities explicitly."""
        flow_id = self.policy.get("implementation_flow_id")
        if not flow_id:
            return ["implementation_flow_not_configured"]
        flow = crud_flow.get(self.db, id=UUID(flow_id), account_id=self.account_id)
        if flow is None or not flow.is_enabled:
            return ["implementation_flow_unavailable"]
        if (flow.agent_config or {}).get("environment_profile") != profile:
            return ["implementation_environment_profile_mismatch"]
        from preloop.services.flow_environment import verification_profile_readiness
        from preloop.services.runner_service import resolve_runner_pool

        pool = resolve_runner_pool(flow, {}, db=self.db)
        readiness = verification_profile_readiness(
            flow.agent_config or {},
            flow.git_clone_config or {},
            agent_type=flow.agent_type,
            runner="server" if pool is None else "private",
            required_command_ids=commands,
        )
        return readiness["blockers"]


async def build_lifecycle_service(
    db: Session,
    *,
    issue_id: UUID,
    account_id: UUID,
    current_user: models.User | None = None,
) -> IssueLifecycleService:
    """Resolve tenant access and existing tracker include/exclude policy."""
    issue = crud_issue_lifecycle.get_issue(db, issue_id=issue_id, account_id=account_id)
    if issue is None:
        raise ValueError("lifecycle_issue_not_found")
    project = crud_issue_lifecycle.get_project(
        db, project_id=issue.project_id, account_id=account_id
    )
    if project is None:
        raise ValueError("lifecycle_project_not_found")
    # A flow is already tenant-authorized by the trigger service; use the same
    # scope resolver as HTTP callers, never bypass tracker exclusions.
    actor = current_user or models.User(
        account_id=account_id, username="lifecycle-worker"
    )
    client = await get_tracker_client(project.organization_id, project.id, db, actor)
    if client.tracker_type != "github":
        raise ValueError("lifecycle_provider_unsupported")
    policy = (project.settings or {}).get("issue_lifecycle") or {}
    provider = GitHubLifecycleProvider(client, issue.key.rsplit("#", 1)[0])
    return IssueLifecycleService(
        db,
        account_id=account_id,
        issue=issue,
        provider=provider,
        capabilities=FlowEnvironmentCapabilities(db, account_id, policy),
        policy=policy,
    )


@lifecycle_worker_hook
async def lifecycle_flow_entry(
    trigger_service: Any, flow: models.Flow, event: dict[str, Any], nats_client: Any
) -> tuple[bool, models.FlowExecution | None]:
    """Intercept explicitly configured lifecycle flows before ordinary dispatch."""
    kind = (flow.agent_config or {}).get("lifecycle_kind")
    if kind not in {"merge_audit", "refinement"} and event.get("type") not in {
        "issue_labeled",
        "issue_updated",
        "issue_created",
    }:
        return False, None
    project_id = event.get("project_id")
    if not project_id or not flow.account_id:
        return False, None
    project = crud_issue_lifecycle.get_project(
        trigger_service.db, project_id=UUID(str(project_id)), account_id=flow.account_id
    )
    if not project:
        return False, None
    policy = (project.settings or {}).get("issue_lifecycle") or {}
    pickup = (
        policy.get("ready_enabled") is True
        and str(policy.get("implementation_flow_id")) == str(flow.id)
        and event.get("type") in {"issue_labeled", "issue_updated", "issue_created"}
    )
    if kind not in {"merge_audit", "refinement"} and not pickup:
        return False, None
    payload = event.get("payload") or {}
    raw_issue = payload.get("issue") or payload.get("object_attributes") or {}
    external_id = raw_issue.get("id")
    issue = crud_issue.get_by_external_id(
        trigger_service.db,
        project_id=str(project.id),
        external_id=str(external_id),
        account_id=str(flow.account_id),
    )
    if issue is None:
        raise ValueError("lifecycle_issue_not_synced")
    service = await build_lifecycle_service(
        trigger_service.db, issue_id=issue.id, account_id=flow.account_id
    )

    async def dispatch(execution: models.FlowExecution) -> None:
        # Resolve expired ORM attributes before crossing to the application loop.
        _ = flow.id, execution.id
        await on_application_loop(
            partial(
                trigger_service._start_flow_execution,
                flow,
                execution.trigger_event_details,
                nats_client,
                precreated_execution=execution,
            )
        )

    if kind == "merge_audit":
        return True, await service.schedule_audit(flow, dispatch)
    if pickup:
        return True, await service.schedule_pickup(flow, event, dispatch)
    # Refinement runs normally, but receives a trusted source revision. Its
    # result is a proposal and cannot itself add the ready label.
    current = await service.provider.issue(service.number)
    event["lifecycle_refinement"] = {
        "issue_id": str(issue.id),
        "issue_revision": current.revision,
    }
    return False, None


@lifecycle_worker_hook
async def lifecycle_execution_finished(
    db: Session, execution: models.FlowExecution, flow: models.Flow
) -> None:
    """Consume persisted structured output; failures remain retryable by API."""
    event = execution.trigger_event_details or {}
    envelope = (event.get("payload") or {}).get("lifecycle") or event.get(
        "lifecycle_refinement"
    )
    if not envelope or not flow.account_id:
        return
    service = await build_lifecycle_service(
        db, issue_id=UUID(envelope["issue_id"]), account_id=flow.account_id
    )
    if (flow.agent_config or {}).get("lifecycle_kind") in {
        "merge_audit",
        "deployment_audit",
    }:
        await service.finish_audit(execution)
    elif execution.status == "SUCCEEDED":
        contract = ReadinessContract.model_validate(
            (execution.result or {}).get("readiness_contract")
        )
        if contract.issue_revision != envelope["issue_revision"]:
            raise ValueError("refinement_revision_mismatch")
        await service.refine(contract)
