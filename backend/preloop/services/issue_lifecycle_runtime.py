"""Production adapters and flow entry/terminal hooks for issue lifecycles."""

import logging
from functools import partial
from typing import Any, cast
from uuid import UUID

import anyio
from sqlalchemy.orm import Session

from preloop.api.common import get_tracker_client
from preloop.models import models
from preloop.models.crud import (
    crud_delivery_report_policy,
    crud_flow,
    crud_issue,
    crud_issue_lifecycle,
)
from preloop.schemas.issue_lifecycle import ReadinessContract
from preloop.services.issue_lifecycle import IssueLifecycleService
from preloop.services.delivery_report import resolve_policy
from preloop.services.issue_lifecycle_provider import GitHubLifecycleProvider
from preloop.services.issue_lifecycle_worker import (
    dispatch_lifecycle_execution,
    lifecycle_entry_decision,
    lifecycle_worker_db,
    lifecycle_worker_hook,
)

logger = logging.getLogger(__name__)


async def sweep_deferred_merge_audits() -> None:
    """Retry durable CI waits from the application monitor's minute tick."""
    await anyio.to_thread.run_sync(_sweep_deferred_merge_audits_worker)


def _sweep_deferred_merge_audits_worker() -> None:
    """Keep provider I/O and lifecycle CRUD on one worker thread."""
    from preloop.models.db.session import get_session_factory

    factory = get_session_factory()
    with factory() as db:
        waiting = [
            (row.account_id, row.issue_id, row.revision)
            for row in crud_issue_lifecycle.waiting_merge_audits(db)
        ]
    for account_id, issue_id, merge_sha in waiting:
        try:
            anyio.run(_retry_deferred_merge_audit, account_id, issue_id, merge_sha)
        except Exception:
            logger.exception(
                "Could not retry deferred merge audit for issue %s", issue_id
            )


async def _retry_deferred_merge_audit(
    account_id: UUID, issue_id: UUID, merge_sha: str
) -> None:
    """Refresh evidence and dispatch the existing audit operation if ready."""
    from preloop.models.db.session import get_session_factory
    from preloop.services.flow_trigger_service import FlowTriggerService
    from preloop.services.kill_switch import flows_halted

    with get_session_factory()() as db:
        service = await build_lifecycle_service(
            db, issue_id=issue_id, account_id=account_id
        )
        flow_id = service.policy.get("audit_flow_id")
        flow = (
            crud_flow.get(db, id=UUID(flow_id), account_id=account_id)
            if flow_id
            else None
        )
        if flow is None or not flow.is_enabled or flows_halted(db, account_id):
            return
        trigger = FlowTriggerService(db)

        async def dispatch(execution: models.FlowExecution) -> None:
            _ = flow.id, execution.id
            await dispatch_lifecycle_execution(
                cast(UUID, execution.id),
                partial(
                    trigger._start_flow_execution,
                    flow,
                    cast(dict[str, Any], execution.trigger_event_details),
                    None,
                    precreated_execution=execution,
                ),
            )

        await service.schedule_audit(flow, dispatch, expected_merge_sha=merge_sha)


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
    policy = dict((project.settings or {}).get("issue_lifecycle") or {})
    report_policy = resolve_policy(
        crud_delivery_report_policy.get_overrides(db, account_id=account_id),
        crud_delivery_report_policy.get_overrides(
            db, account_id=account_id, project_id=project.id
        ),
    )
    policy["audit_ci_max_wait_minutes"] = report_policy.audit_ci_max_wait_minutes
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
    fallback = getattr(trigger_service, "db", None)
    with lifecycle_worker_db(trigger_service, fallback) as db:
        return await _lifecycle_flow_entry(
            db, flow, event, nats_client, trigger_service
        )


async def _lifecycle_flow_entry(
    db: Any,
    flow: models.Flow,
    event: dict[str, Any],
    nats_client: Any,
    trigger_service: Any,
) -> tuple[bool, models.FlowExecution | None]:
    """CRUD for ``lifecycle_flow_entry`` on the worker-owned session."""
    entry = lifecycle_entry_decision(db, flow, event)
    if not entry.engaged:
        return False, None
    project = entry.project
    payload = event.get("payload") or {}
    raw_issue = payload.get("issue") or payload.get("object_attributes") or {}
    external_id = raw_issue.get("id")
    issue = crud_issue.get_by_external_id(
        db,
        project_id=str(project.id),
        external_id=str(external_id),
        account_id=str(flow.account_id),
    )
    if issue is None:
        raise ValueError("lifecycle_issue_not_synced")
    service = await build_lifecycle_service(
        db, issue_id=issue.id, account_id=flow.account_id
    )

    async def dispatch(execution: models.FlowExecution) -> None:
        # Resolve expired ORM attributes before crossing to the application loop.
        _ = flow.id, execution.id
        await dispatch_lifecycle_execution(
            execution.id,
            partial(
                trigger_service._start_flow_execution,
                flow,
                execution.trigger_event_details,
                nats_client,
                precreated_execution=execution,
            ),
        )

    if entry.kind == "merge_audit":
        return True, await service.schedule_audit(flow, dispatch)
    if entry.pickup:
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
    with lifecycle_worker_db(fallback=db) as worker_db:
        envelope = (event.get("payload") or {}).get("lifecycle") or event.get(
            "lifecycle_refinement"
        )
        if not envelope or not flow.account_id:
            return
        service = await build_lifecycle_service(
            worker_db, issue_id=UUID(envelope["issue_id"]), account_id=flow.account_id
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
