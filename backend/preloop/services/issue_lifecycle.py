"""Readiness transitions and merge-linked independent completion auditing."""

from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_issue_lifecycle
from preloop.schemas.issue_lifecycle import AuditResult, ReadinessContract
from preloop.services.issue_lifecycle_provider import (
    LifecycleProvider,
    publication_budget,
)


class EnvironmentCapabilities(Protocol):
    """The environment service owns image approval and runnable command names."""

    async def blockers(self, profile: str, commands: list[str]) -> list[str]: ...


class IssueLifecycleService:
    """A single controller shared by HTTP, tracker triggers and terminal hooks."""

    def __init__(
        self,
        db: Session,
        *,
        account_id: UUID,
        issue: models.Issue,
        provider: LifecycleProvider,
        capabilities: EnvironmentCapabilities,
        policy: dict[str, Any],
    ) -> None:
        self.db, self.account_id, self.issue = db, account_id, issue
        self.provider, self.capabilities, self.policy = provider, capabilities, policy
        self.number = int(issue.key.rsplit("#", 1)[-1])

    def _get(self, kind: str, revision: str) -> models.IssueLifecycle | None:
        return crud_issue_lifecycle.get(
            self.db,
            account_id=self.account_id,
            issue_id=self.issue.id,
            kind=kind,
            revision=revision,
        )

    def _put(
        self, kind: str, revision: str, state: str, data: dict[str, Any]
    ) -> models.IssueLifecycle:
        return crud_issue_lifecycle.put(
            self.db,
            account_id=self.account_id,
            issue_id=self.issue.id,
            kind=kind,
            revision=revision,
            state=state,
            data=data,
        )

    async def refine(self, contract: ReadinessContract) -> dict[str, Any]:
        """Persist a reviewable proposal and explain material blockers."""
        async with crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id):
            current = await self.provider.issue(self.number)
            blockers = await self._blockers(contract)
            if current.revision != contract.issue_revision:
                blockers.append("issue_scope_changed")
            pickup = self._get("pickup", "once")
            if pickup and pickup.data["issue_revision"] != current.revision:
                blockers.append("implementation_scope_requires_reconciliation")
                self._put("pickup", "once", "needs_reconciliation", pickup.data)
            data = {
                "contract": contract.model_dump(),
                "blockers": blockers,
                "issue_url": current.url,
            }
            self._put(
                "readiness",
                contract.issue_revision,
                "blocked" if blockers else "reviewable",
                data,
            )
            return data

    async def _blockers(self, contract: ReadinessContract) -> list[str]:
        missing = [
            name
            for name in (
                "problem",
                "user_outcome",
                "criteria",
                "code_entry_points",
                "test_entry_points",
                "environment_profile",
                "test_commands",
            )
            if not getattr(contract, name)
        ]
        blockers = [f"missing_{name}" for name in missing]
        blockers.extend(f"decision:{item}" for item in contract.blocking_decisions)
        blockers.extend(f"conflict:{item}" for item in contract.conflicts)
        ids = [criterion.id for criterion in contract.criteria]
        if len(ids) != len(set(ids)):
            blockers.append("duplicate_acceptance_id")
        for number in contract.dependencies:
            if number <= 0 or number == self.number:
                blockers.append(f"invalid_dependency:{number}")
            elif (await self.provider.issue(number)).state != "closed":
                blockers.append(f"blocked_dependency:{number}")
        if contract.environment_profile and contract.test_commands:
            blockers.extend(
                await self.capabilities.blockers(
                    contract.environment_profile, contract.test_commands
                )
            )
        return blockers

    async def reconcile_pickup(
        self, revision: str, previous_execution_id: UUID, reason: str
    ) -> dict[str, Any]:
        """Explicitly authorize changed scope only after the previous run ends."""
        return await self._ready(revision, previous_execution_id, reason)

    async def ready(self, revision: str) -> dict[str, Any]:
        """Authorize initial or unlaunched pickup, preserving existing executions."""
        return await self._ready(revision)

    async def _ready(
        self, revision: str, previous_execution_id: UUID | None = None, reason: str = ""
    ) -> dict[str, Any]:
        """Authorize exactly one additive label transition for an issue.

        GitHub has no conditional-label-write API. The provider rereads scope
        immediately before the effect and this controller checks again after it;
        concurrent edits are recorded for reconciliation, never silently adopted.
        """
        if self.policy.get("ready_enabled") is not True:
            raise ValueError("readiness_transition_not_authorized")
        async with crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id):
            existing = self._get("pickup", "once")
            current = await self.provider.issue(self.number)
            if current.revision != revision or current.state != "open":
                return {"state": "blocked", "blockers": ["issue_scope_changed"]}
            replacement = previous_execution_id is not None
            if replacement:
                if (
                    existing
                    and existing.data.get("replaces_execution_id")
                    == str(previous_execution_id)
                    and existing.data["issue_revision"] == revision
                ):
                    if existing.state != "label_pending":
                        return {"state": existing.state, **existing.data}
                    replacement = (
                        False  # Retry a provider effect without resetting intent.
                    )
                else:
                    if (
                        existing is None
                        or existing.execution_id != previous_execution_id
                    ):
                        raise ValueError("pickup_execution_mismatch")
                    prior = crud_issue_lifecycle.pickup_execution(self.db, row=existing)
                    if prior is None or prior.status not in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "STOPPED",
                        "TIMED_OUT",
                        "ABORTED",
                    }:
                        raise ValueError("pickup_execution_not_terminal")
                    if existing.data["issue_revision"] == revision:
                        raise ValueError("pickup_scope_unchanged_use_execution_retry")
                    if not reason.strip():
                        raise ValueError("pickup_reconciliation_reason_required")
            if existing:
                if existing.data["issue_revision"] != current.revision:
                    self._put("pickup", "once", "needs_reconciliation", existing.data)
                if existing.state == "needs_reconciliation":
                    if existing.execution_id is not None and not replacement:
                        return {
                            "state": existing.state,
                            **existing.data,
                            "blockers": ["pickup_reconciliation_required"],
                        }
                    # This explicit /ready request can replace an authorization
                    # that never launched. It still validates the current proposal
                    # and scope below; an existing execution is never replaced.
                elif existing.state != "label_pending" and not replacement:
                    return {"state": existing.state, **existing.data}
            row = self._get("readiness", revision)
            if row is None:
                raise ValueError("readiness_contract_missing")
            contract = ReadinessContract.model_validate(row.data["contract"])
            blockers = await self._blockers(contract)
            if current.revision != revision or current.state != "open":
                blockers.append("issue_scope_changed")
            if blockers:
                return {"state": "blocked", "blockers": blockers}
            label = self.policy.get("ready_label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError("ready_label_not_configured")
            await self.provider.require_ready_label(label)
            # The marker is stable even if provider success precedes rollback.
            data = {
                "issue_revision": revision,
                "contract": contract.model_dump(),
                "label": label,
            }
            if replacement:
                data.update(
                    {
                        "authorization_id": str(uuid4()),
                        "replaces_execution_id": str(previous_execution_id),
                        "reconciliation_reason": reason,
                    }
                )
            elif existing and existing.state == "label_pending":
                data = dict(existing.data)
            if existing and (replacement or existing.state == "needs_reconciliation"):
                crud_issue_lifecycle.archive_pickup(self.db, row=existing)
            self._put("pickup", "once", "label_pending", data)
        # Persist intent before the provider effect. If its successful response
        # is lost, the real label webhook can still resolve this authorization.
        async with crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id):
            pending = self._get("pickup", "once")
            if pending.state != "label_pending" or pending.data != data:
                return {"state": pending.state, **pending.data}
            await self.provider.add_ready_label(self.number, label, revision)
            after = await self.provider.issue(self.number)
            state = "ready" if after.revision == revision else "needs_reconciliation"
            self._put("pickup", "once", state, data)
            return {"state": state, **data}

    async def schedule_pickup(
        self,
        flow: models.Flow,
        event: dict[str, Any],
        dispatch: Callable[[models.FlowExecution], Awaitable[None]],
    ) -> models.FlowExecution | None:
        """Atomically bind the authorized pickup to one execution."""
        from preloop.services.flow_trigger_service import FlowDispatchError

        async with crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id):
            row = self._get("pickup", "once")
            current = await self.provider.issue(self.number)
            if row is None or row.data.get("issue_revision") != current.revision:
                if row:
                    self._put("pickup", "once", "needs_reconciliation", row.data)
                return None
            if str(flow.id) != str(self.policy.get("implementation_flow_id")):
                return None
            if row.state not in {
                "label_pending",
                "ready",
                "dispatch_pending",
                "dispatched",
            }:
                return None
            if current.state != "open" or row.data["label"] not in current.labels:
                return None
            if await self._blockers(
                ReadinessContract.model_validate(row.data["contract"])
            ):
                return None
            event = {
                **event,
                "lifecycle_pickup": {"issue_id": str(self.issue.id), **row.data},
            }
            execution = crud_issue_lifecycle.create_execution(
                self.db, row=row, flow_id=flow.id, event=event
            )
            state = (
                "dispatch_pending" if execution.status == "PENDING" else "dispatched"
            )
            self._put("pickup", "once", state, row.data)
        if execution.status == "PENDING":
            try:
                await dispatch(execution)
            except FlowDispatchError:
                # The committed execution is recoverable; do not claim delivery
                # or replace it when the broker did not acknowledge publication.
                return execution
            async with crud_issue_lifecycle.locked(
                self.db, self.account_id, self.issue.id
            ):
                current = self._get("pickup", "once")
                if (
                    current.execution_id == execution.id
                    and current.state == "dispatch_pending"
                ):
                    self._put("pickup", "once", "dispatched", current.data)
        return execution

    async def schedule_audit(
        self,
        flow: models.Flow,
        dispatch: Callable[[models.FlowExecution], Awaitable[None]],
    ) -> models.FlowExecution | None:
        """Revalidate closure, create one independent run and dispatch durably."""
        if str(flow.id) != str(self.policy.get("audit_flow_id")):
            return None
        async with (
            crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id),
            publication_budget(),
        ):
            current = await self.provider.issue(self.number)
            links = await self.provider.merged_links(self.number)
            if current.state != "closed" or not links:
                return None
            merge = links[-1]
            if (
                flow.account_id != self.account_id
                or (flow.agent_config or {}).get("lifecycle_kind") != "merge_audit"
            ):
                raise ValueError("invalid_audit_flow")
            git = flow.git_clone_config or {}
            repos = git.get("repositories") or []
            if (
                not git.get("enabled")
                or git.get("create_pull_request")
                or len(repos) != 1
                or str(repos[0].get("project_id")) != str(self.issue.project_id)
            ):
                raise ValueError(
                    "audit_requires_exact_project_checkout_without_publication"
                )
            if flow.allowed_mcp_servers or flow.allowed_mcp_tools:
                raise ValueError("audit_requires_independent_read_only_tool_scope")
            row = self._get("merge_audit", merge.sha)
            if row and row.state == "published":
                return None
            if row is None:
                ready = self._get("readiness", current.revision)
                contract = (
                    ready.data["contract"]
                    if ready
                    else ReadinessContract(issue_revision=current.revision).model_dump()
                )
                pickup = self._get("pickup", "once")
                data = {
                    "issue_revision": current.revision,
                    "pickup_revision": pickup.data.get("issue_revision")
                    if pickup
                    else None,
                    "contract": contract,
                    "issue_url": current.url,
                    "issue_title": current.title,
                    "issue_body": current.body,
                    "merge_sha": merge.sha,
                    "merges": [
                        {"number": link.number, "sha": link.sha, "url": link.url}
                        for link in links
                    ],
                }
                row = self._put("merge_audit", merge.sha, "queued", data)
            event = {
                "source": str(self.issue.tracker_id),
                "type": "issue_closed",
                "project_id": str(self.issue.project_id),
                "payload": {
                    "sha": merge.sha,
                    "repository": {"full_name": self.issue.key.rsplit("#", 1)[0]},
                    "lifecycle": {"issue_id": str(self.issue.id), **row.data},
                },
            }
            execution = crud_issue_lifecycle.create_execution(
                self.db, row=row, flow_id=flow.id, event=event
            )
        # The durable PENDING execution is recoverable if dispatch fails. The
        # normal worker's execution claim also tolerates repeated delivery.
        if execution.status == "PENDING":
            await dispatch(execution)
        return execution

    async def schedule_deployment_audit(
        self,
        *,
        merge_sha: str,
        target: str,
        deployed_revision: str,
        deployment_evidence: str,
        flow: models.Flow,
        dispatch: Callable[[models.FlowExecution], Awaitable[None]],
    ) -> models.FlowExecution:
        """Trigger separate verification only after an authorized deployment record.

        Target and test scope come from project policy, never issue text. The
        authenticated CD/operator caller records the actual deployed revision
        and its deployment evidence; closing an issue cannot call this method.
        """
        target_policy = (self.policy.get("deployment_targets") or {}).get(target)
        if (
            not target_policy
            or str(target_policy.get("flow_id")) != str(flow.id)
            or not target_policy.get("approved_scope")
        ):
            raise ValueError("deployment_scope_not_authorized")
        if (
            flow.account_id != self.account_id
            or not flow.is_enabled
            or (flow.agent_config or {}).get("lifecycle_kind") != "deployment_audit"
        ):
            raise ValueError("invalid_deployment_audit_flow")
        if (flow.git_clone_config or {}).get("create_pull_request"):
            raise ValueError("deployment_audit_cannot_publish_code")
        operation = sha256(
            f"{merge_sha}:{target}:{deployed_revision}".encode()
        ).hexdigest()
        async with crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id):
            merged = self._get("merge_audit", merge_sha)
            if (
                not merged
                or merged.state != "published"
                or not merged.data.get("deployment_verification")
            ):
                raise ValueError("merge_audit_not_waiting_for_deployment")
            row = self._get("deployment_audit", operation)
            if row is None:
                contract = ReadinessContract.model_validate(merged.data["contract"])
                contract.criteria = [
                    item for item in contract.criteria if item.deployment_required
                ]
                data = {
                    **merged.data,
                    "contract": contract.model_dump(),
                    "audit_kind": "deployment_audit",
                    "operation_revision": operation,
                    "deployed_revision": deployed_revision,
                    "deployment_record": {
                        "target": target,
                        "evidence": deployment_evidence,
                        "approved_scope": target_policy["approved_scope"],
                    },
                    "follow_ups": {},
                    "deployment_verification": None,
                }
                row = self._put("deployment_audit", operation, "queued", data)
            event = {
                "source": "authorized_deployment",
                "type": "deployment_verification",
                "project_id": str(self.issue.project_id),
                "payload": {
                    "sha": deployed_revision,
                    "lifecycle": {"issue_id": str(self.issue.id), **row.data},
                },
            }
            execution = crud_issue_lifecycle.create_execution(
                self.db, row=row, flow_id=flow.id, event=event
            )
        if execution.status == "PENDING":
            await dispatch(execution)
        return execution

    async def finish_audit(self, execution: models.FlowExecution) -> dict[str, Any]:
        """Publish one verdict and at most three confirmed, deduplicated gaps."""
        envelope = (execution.trigger_event_details or {}).get("payload", {}).get(
            "lifecycle"
        ) or {}
        sha = envelope.get("merge_sha", "")
        async with (
            crud_issue_lifecycle.locked(self.db, self.account_id, self.issue.id),
            publication_budget(),
        ):
            kind = envelope.get("audit_kind", "merge_audit")
            operation = envelope.get("operation_revision", sha)
            row = self._get(kind, operation)
            if not row or row.execution_id != execution.id:
                raise ValueError("audit_execution_not_bound")
            if row.state == "published":
                return row.data
            if execution.status != "SUCCEEDED":
                if execution.status not in {
                    "FAILED",
                    "CANCELLED",
                    "TIMED_OUT",
                    "ABORTED",
                }:
                    return {"state": "waiting_for_successful_audit"}
                contract = ReadinessContract.model_validate(row.data["contract"])
                data = {
                    **row.data,
                    "verdict": "unknown",
                    "follow_ups": {},
                    "deployment_verification": None,
                    "evidence": {
                        "status": "error",
                        "checked_out_sha": None,
                        "criteria": [
                            {
                                "criterion_id": item.id,
                                "verdict": "unknown",
                                "code": [],
                                "tests": [],
                                "observations": [],
                                "reason": f"Independent audit execution ended with {execution.status}; acceptance is unverified.",
                            }
                            for item in contract.criteria
                        ],
                    },
                }
                marker = f"<!-- preloop-{kind}:{self.account_id}:{self.issue.id}:{operation} -->"
                data["comment_url"] = await self.provider.upsert_comment(
                    self.number, marker, self._audit_comment(data, execution.id)
                )
                self._put(kind, operation, "published", data)
                return data
            result = AuditResult.model_validate(execution.result)
            if (
                result.checked_out_sha != envelope.get("deployed_revision", sha)
                or result.issue_revision != row.data["issue_revision"]
            ):
                raise ValueError("audit_revision_mismatch")
            contract = ReadinessContract.model_validate(row.data["contract"])
            criteria = {criterion.id: criterion for criterion in contract.criteria}
            evidence = {item.criterion_id: item for item in result.criteria}
            if len(evidence) != len(result.criteria) or set(evidence) != set(criteria):
                raise ValueError("audit_acceptance_coverage_mismatch")
            verdicts = []
            for key, item in evidence.items():
                verdict = item.verdict
                if verdict == "complete" and (
                    not item.code
                    or not item.tests
                    or not item.observations
                    or (
                        criteria[key].deployment_required and kind != "deployment_audit"
                    )
                ):
                    verdict = "unknown"
                if (
                    verdict == "gap"
                    and not item.code
                    and not item.tests
                    and not item.observations
                ):
                    verdict = "unknown"
                item.verdict = verdict
                verdicts.append(verdict)
            verdict = (
                "gap"
                if "gap" in verdicts
                else "unknown"
                if not criteria or "unknown" in verdicts
                else "complete"
            )
            current = await self.provider.issue(self.number)
            if current.revision != row.data["issue_revision"]:
                verdict = "unknown"
            follow_ups = dict(row.data.get("follow_ups", {}))
            if self.policy.get("create_follow_ups") is True:
                for item in result.follow_ups:
                    if (
                        item.criterion_id not in evidence
                        or evidence[item.criterion_id].verdict != "gap"
                    ):
                        continue
                    if len(follow_ups) >= 3:
                        break
                    key = sha256(
                        f"{self.account_id}:{self.issue.id}:{item.criterion_id}:{item.kind}".encode()
                    ).hexdigest()
                    if key in follow_ups:
                        continue
                    marker = f"<!-- preloop-follow-up:{key} -->"
                    body = (
                        f"{item.description}\n\nKind: {item.kind}\nSource: {row.data['issue_url']}\nMerge: {sha}\nAcceptance criterion: {item.criterion_id}\nEvidence:\n"
                        + "\n".join(f"- {link}" for link in item.evidence)
                    )
                    body += "\n\nThis follow-up requires a separate readiness review before implementation."
                    follow_ups[key] = await self.provider.ensure_follow_up(
                        self.number, marker, item.title, body
                    )
            deployment = [
                criterion.id
                for criterion in criteria.values()
                if criterion.deployment_required and kind != "deployment_audit"
            ]
            data = {
                **row.data,
                "verdict": verdict,
                "evidence": result.model_dump(),
                "follow_ups": follow_ups,
                "deployment_verification": {
                    "state": "requires_deployment_revision_and_scope_approval",
                    "criteria": deployment,
                    "merge_sha": sha,
                }
                if deployment
                else None,
            }
            body = self._audit_comment(data, execution.id)
            marker = (
                f"<!-- preloop-{kind}:{self.account_id}:{self.issue.id}:{operation} -->"
            )
            data["comment_url"] = await self.provider.upsert_comment(
                self.number, marker, body
            )
            self._put(kind, operation, "published", data)
            return data

    @staticmethod
    def _audit_comment(data: dict[str, Any], execution_id: UUID) -> str:
        rows = [
            f"Completion audit: **{data['verdict']}**",
            f"Merged revision: `{data['merge_sha']}`",
            f"Issue revision: `{data['issue_revision']}`",
            f"Independent execution: [{execution_id}]({settings.preloop_url.rstrip('/')}/console/flows/executions/{execution_id})",
            "",
        ]
        rows.extend(
            f"Merged PR: {merge['url']} (`{merge['sha']}`)" for merge in data["merges"]
        )
        if data.get("deployment_record"):
            rows.append(f"Deployed revision: `{data['deployed_revision']}`")
            rows.append(f"Deployment evidence: {data['deployment_record']['evidence']}")
            rows.append(
                f"Approved scope: {data['deployment_record']['approved_scope']}"
            )
        for item in data["evidence"]["criteria"]:
            rows.append(
                f"- {item['criterion_id']}: {item['verdict']} — {item['reason']}"
            )
            rows.extend(
                f"  - {pointer}"
                for pointer in item["code"] + item["tests"] + item["observations"]
            )
        rows.extend(f"Follow-up: {url}" for url in data["follow_ups"].values())
        if data["deployment_verification"]:
            rows.append(
                "Deployment acceptance remains unknown. Separate verification requires a recorded deployed revision and approved test scope; no environment was probed automatically."
            )
        return "\n".join(rows)
