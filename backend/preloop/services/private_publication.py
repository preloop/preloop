"""Authenticated private controller protocol for credential-isolated publication.

Only the registered runner socket can consume durable publication phases. Agent
results, output markers and ordinary completion packets carry no authority.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from typing import Any, TYPE_CHECKING
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from preloop.agents.runner_launch import flow_launch_fingerprint
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_tracker
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.verification import ResolvedVerificationPolicy
from preloop.services.publication_credentials import (
    mint_repository_lease,
    revoke_repository_lease,
)
from preloop.services.trusted_publisher import (
    PublicationBinding,
    PublicationError,
    PublicationLease,
)
from preloop.services.verification import select_required_checks
from preloop.utils.pr_metadata import PublicationRecord

if TYPE_CHECKING:
    from preloop.services.isolated_publication import IsolatedPublicationPolicy

STATE_KEY = "_private_publication"
_HEX40 = re.compile(r"[a-f0-9]{40}")
_HEX64 = re.compile(r"[a-f0-9]{64}")


def persist_private_publication(
    db: Session, flow: models.Flow, policy: IsolatedPublicationPolicy
) -> None:
    """Persist only trusted, secret-free policy before a private runner lease."""
    if not policy.private or not _HEX64.fullmatch(policy.nonce):
        raise PublicationError("Invalid private publication policy")
    snapshot = {
        key: getattr(policy, key)
        for key in (
            "tracker_id",
            "account_id",
            "repository_url",
            "branch",
            "base",
            "expected_remote_sha",
            "execution_id",
            "configured_title",
            "configured_body",
            "issue_number",
            "base_sha",
            "verification_image",
        )
    }
    snapshot["previous_records"] = [
        asdict(record) for record in policy.previous_records
    ]
    snapshot["verification_policy"] = policy.verification_policy.model_dump(mode="json")
    snapshot["flow_id"] = str(flow.id)
    snapshot["flow_fingerprint"] = flow_launch_fingerprint(flow)
    state = {
        "version": 1,
        "nonce": policy.nonce,
        "phase": "agent",
        "policy": snapshot,
        "deadline": datetime.now(timezone.utc).timestamp()
        + (flow.timeout_seconds or settings.flow_execution_max_wait_seconds)
        + policy.verification_policy.gate_budget_seconds
        + 120,
    }
    crud_flow_runner.save_publication_policy(
        db,
        execution_id=UUID(policy.execution_id),
        account_id=UUID(policy.account_id),
        state=state,
    )


def public_publication_descriptor(state: dict[str, Any]) -> dict[str, Any]:
    """The runner delegate sees the binding, never control-plane policy storage."""
    if state.get("phase") != "agent":
        raise PublicationError("Private publication cannot replay a consumed launch")
    policy = state["policy"]
    return {
        "version": 1,
        "nonce": state["nonce"],
        "phase": "agent",
        **{
            key: policy[key]
            for key in (
                "repository_url",
                "branch",
                "base",
                "base_sha",
                "expected_remote_sha",
                "verification_image",
            )
        },
        "verification_budget_seconds": policy["verification_policy"][
            "gate_budget_seconds"
        ],
    }


def _authorized_flow(db: Session, policy: dict[str, Any]) -> models.Flow:
    flow = crud_flow.get(
        db, id=policy["flow_id"], account_id=policy["account_id"], refresh=True
    )
    if flow is None:
        raise PublicationError("Publication flow is no longer authorized")
    if flow_launch_fingerprint(flow) != policy["flow_fingerprint"]:
        raise PublicationError("Publication configuration changed after leasing")
    return flow


async def restore_private_publication(
    db: Session, flow: models.Flow, context: dict[str, Any]
) -> IsolatedPublicationPolicy | None:
    """Rebuild an unstarted private launch with its original pin and a read lease."""
    from preloop.services.isolated_publication import IsolatedPublicationPolicy

    execution = crud_flow_execution.get(
        db, id=context["execution_id"], account_id=str(flow.account_id), refresh=True
    )
    state = (execution.result or {}).get(STATE_KEY) if execution else None
    if not isinstance(state, dict):
        return None
    public_publication_descriptor(state)
    if state["deadline"] <= datetime.now(timezone.utc).timestamp():
        raise PublicationError("Private publication launch expired")
    saved = state["policy"]
    if (
        saved["flow_id"] != str(flow.id)
        or saved["execution_id"] != str(execution.id)
        or saved["account_id"] != str(flow.account_id)
    ):
        raise PublicationError("Private publication restore binding mismatch")
    _authorized_flow(db, saved)
    tracker = crud_tracker.get_by_id_and_account(
        db, id=saved["tracker_id"], account_id=saved["account_id"]
    )
    if tracker is None:
        raise PublicationError("Publication tracker is no longer authorized")
    async with httpx.AsyncClient() as client:
        read_lease = await mint_repository_lease(
            tracker, saved["repository_url"], write=False, client=client
        )
    config = dict(context.get("git_clone_config") or flow.git_clone_config or {})
    repositories = config.get("repositories") or [{}]
    config["repositories"] = [
        {
            **repositories[0],
            "repository_url": saved["repository_url"],
            "tracker_id": saved["tracker_id"],
        }
    ]
    config["source_branch"] = saved["base"]
    config["target_branch"] = saved["branch"]
    context["git_clone_config"] = config
    context["git_credentials_map"] = {
        saved["tracker_id"]: {
            "token": read_lease.token,
            "tracker_type": "github",
            "permission": "read",
        }
    }
    context["trigger_tracker_id"] = saved["tracker_id"]
    trigger = context.get("trigger_event_data") or {}
    if trigger.get("_resume"):
        context["trigger_event_data"] = {
            **trigger,
            "_resume": {**trigger["_resume"], "source_branch": saved["branch"]},
        }
    return IsolatedPublicationPolicy(
        **{
            key: saved[key]
            for key in (
                "tracker_id",
                "account_id",
                "repository_url",
                "branch",
                "base",
                "expected_remote_sha",
                "execution_id",
                "configured_title",
                "configured_body",
                "issue_number",
                "base_sha",
                "verification_image",
            )
        },
        previous_records=tuple(
            PublicationRecord(**record) for record in saved["previous_records"]
        ),
        verification_policy=ResolvedVerificationPolicy.model_validate(
            saved["verification_policy"]
        ),
        read_lease=read_lease,
        private=True,
        nonce=state["nonce"],
    )


def load_private_monitoring_policy(
    db: Session,
    flow: models.Flow,
    execution: models.FlowExecution,
) -> IsolatedPublicationPolicy:
    """Recover monitoring without relaunch, replay, phase advance or credentials."""
    from preloop.services.isolated_publication import IsolatedPublicationPolicy

    execution = crud_flow_execution.get(
        db, id=execution.id, account_id=str(flow.account_id), refresh=True
    )
    state = (execution.result or {}).get(STATE_KEY) if execution else None
    if not isinstance(state, dict):
        raise PublicationError("Private publication recovery policy unavailable")
    saved = state["policy"]
    if saved["flow_id"] != str(flow.id) or saved["execution_id"] != str(execution.id):
        raise PublicationError("Private publication recovery binding mismatch")
    _authorized_flow(db, saved)
    return IsolatedPublicationPolicy(
        **{
            key: saved[key]
            for key in (
                "tracker_id",
                "account_id",
                "repository_url",
                "branch",
                "base",
                "expected_remote_sha",
                "execution_id",
                "configured_title",
                "configured_body",
                "issue_number",
                "base_sha",
                "verification_image",
            )
        },
        previous_records=tuple(
            PublicationRecord(**record) for record in saved["previous_records"]
        ),
        verification_policy=ResolvedVerificationPolicy.model_validate(
            saved["verification_policy"]
        ),
        read_lease=None,
        private=True,
        nonce=state["nonce"],
    )


def trusted_private_receipt(execution: models.FlowExecution) -> dict[str, Any]:
    """Require the controller's completed durable state, not an agent claim."""
    result = execution.result or {}
    state = result.get(STATE_KEY)
    receipt = result.get("trusted_publication")
    if (
        not isinstance(state, dict)
        or state.get("phase") != "complete"
        or not isinstance(receipt, dict)
        or receipt != state.get("receipt")
    ):
        raise PublicationError(
            "Private publication was not acknowledged by the controller"
        )
    return deepcopy(receipt)


def _manifest(message: dict[str, Any], *, candidate: bool) -> dict[str, Any]:
    if type(message.get("version")) is not int or message["version"] != 1:
        raise PublicationError("Unsupported publication protocol")
    result = {}
    for key, pattern in (
        ("head_sha", _HEX40),
        ("tree_sha", _HEX40),
        ("bundle_sha256", _HEX64),
    ):
        value = message.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise PublicationError("Invalid publication manifest")
        result[key] = value
    if candidate:
        paths = message.get("changed_files")
        if not isinstance(paths, list) or len(paths) > 10000:
            raise PublicationError("Invalid changed-file manifest")
        if any(
            not isinstance(path, str)
            or not path
            or len(path.encode()) > 4096
            or path.startswith("/")
            or "\\" in path
            or any(ord(char) < 32 for char in path)
            or any(part in {".", "..", ""} for part in path.split("/"))
            for path in paths
        ):
            raise PublicationError("Unsafe changed-file manifest")
        if len(json.dumps(paths).encode()) > 1024 * 1024 or len(set(paths)) != len(
            paths
        ):
            raise PublicationError("Changed-file manifest exceeds bounds")
        result["changed_files"] = paths
    return result


class PrivatePublicationController:
    """One authenticated socket's transient writer and durable protocol cursor."""

    def __init__(
        self, db: Session, *, runner_id: UUID, account_id: UUID, connection_id: str
    ) -> None:
        self.db = db
        self.connection_id = connection_id
        self._lock = asyncio.Lock()
        self.expiry_task: asyncio.Task[None] | None = None
        self.runner_id = runner_id
        self.account_id = account_id
        self.writer: PublicationLease | None = None
        self.execution_id: UUID | None = None
        self.nonce: str | None = None

    async def revoke(self) -> None:
        """Revoke any live write credential even when runtime removal is unknown."""
        task, self.expiry_task = self.expiry_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        writer, self.writer = self.writer, None
        if writer is not None:
            async with httpx.AsyncClient() as client:
                await revoke_repository_lease(writer, client)

    async def _expire_writer(self, deadline: float) -> None:
        """Serialize expiry with messages and use an independent DB session."""
        from preloop.models.db.session import get_db_session

        await asyncio.sleep(max(0, deadline - datetime.now(timezone.utc).timestamp()))
        async with self._lock:
            try:
                await self.revoke()
            finally:
                if self.execution_id is not None and self.nonce is not None:
                    sessions = get_db_session()
                    db = next(sessions)
                    try:
                        crud_flow_runner.abandon_publication(
                            db,
                            runner_id=self.runner_id,
                            execution_id=self.execution_id,
                            nonce=self.nonce,
                        )
                    finally:
                        sessions.close()

    async def close(self) -> None:
        """A disconnect consumes interrupted phases and never replays a writer."""
        async with self._lock:
            try:
                await self.revoke()
            finally:
                if self.execution_id is not None and self.nonce is not None:
                    self.db.rollback()
                    crud_flow_runner.abandon_publication(
                        self.db,
                        runner_id=self.runner_id,
                        execution_id=self.execution_id,
                        nonce=self.nonce,
                    )

    def _read(self, execution_id: UUID, nonce: str) -> dict[str, Any]:
        try:
            state = crud_flow_runner.publication_state(
                self.db,
                runner_id=self.runner_id,
                account_id=self.account_id,
                execution_id=execution_id,
                nonce=nonce,
            )
        except ValueError as exc:
            self.db.rollback()
            raise PublicationError(str(exc)) from exc
        _authorized_flow(self.db, state["policy"])
        if state.get("connection_id") not in {None, self.connection_id}:
            raise PublicationError("Publication connection was replaced")
        return state

    def _transition(
        self,
        expected: dict[str, Any],
        updated: dict[str, Any],
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return crud_flow_runner.transition_publication(
                self.db,
                runner_id=self.runner_id,
                account_id=self.account_id,
                execution_id=self.execution_id,
                nonce=self.nonce,
                expected=expected,
                updated={**updated, "connection_id": self.connection_id},
                receipt=receipt,
            )
        except ValueError as exc:
            self.db.rollback()
            raise PublicationError(str(exc)) from exc

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        """Serialize one exact phase with token revocation and expiry."""
        async with self._lock:
            return await self._handle(message)

    async def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        """Mint only after verified state is consumed."""
        try:
            execution_id = UUID(str(message.get("execution_id")))
        except ValueError as exc:
            raise PublicationError("Invalid publication execution") from exc
        nonce = message.get("nonce")
        if not isinstance(nonce, str) or not _HEX64.fullmatch(nonce):
            raise PublicationError("Invalid publication nonce")
        state = self._read(execution_id, nonce)
        self.execution_id, self.nonce = execution_id, nonce
        policy = state["policy"]
        event = message.get("type")
        identity = _manifest(message, candidate=event == "publication_candidate")
        envelope = {
            "version": 1,
            "execution_id": str(execution_id),
            "nonce": nonce,
            **{key: identity[key] for key in ("head_sha", "tree_sha", "bundle_sha256")},
        }
        if event == "publication_candidate":
            if state["phase"] != "agent":
                raise PublicationError(
                    "Publication candidate phase was already consumed"
                )
            verification = ResolvedVerificationPolicy.model_validate(
                policy["verification_policy"]
            )
            if verification.mode != "gate" or verification.profile is None:
                raise PublicationError("Publication requires a verification gate")
            checks = [
                {
                    "id": check.id,
                    "command": check.command,
                    "timeout_seconds": check.timeout_seconds,
                }
                for check in select_required_checks(
                    verification.profile, identity["changed_files"]
                ).commands
            ]
            if (
                not checks
                or len(checks) > 100
                or len({check["id"] for check in checks}) != len(checks)
            ):
                raise PublicationError("Publication requires nonempty exact checks")
            self._transition(
                state,
                {
                    **state,
                    "phase": "verifying",
                    "manifest": identity,
                    "checks": checks,
                    "verification_deadline": min(
                        state["deadline"],
                        datetime.now(timezone.utc).timestamp()
                        + verification.gate_budget_seconds,
                    ),
                    "deadline": min(
                        state["deadline"],
                        datetime.now(timezone.utc).timestamp()
                        + verification.gate_budget_seconds
                        + 120,
                    ),
                },
            )
            return {
                **envelope,
                "type": "publication_verify",
                "checks": checks,
                "image": policy["verification_image"],
                "budget_seconds": verification.gate_budget_seconds,
            }
        manifest = state.get("manifest") or {}
        if any(identity[key] != manifest.get(key) for key in identity):
            raise PublicationError("Publication manifest changed after freezing")
        if event == "publication_verified":
            if (
                state["phase"] != "verifying"
                or message.get("agent_removed") is not True
                or message.get("verifiers_removed") is not True
            ):
                raise PublicationError("Publication verification phase is invalid")
            if (
                state.get("verification_deadline", 0)
                <= datetime.now(timezone.utc).timestamp()
            ):
                raise PublicationError("Publication verification budget expired")
            checks = message.get("checks")
            expected = [{**check, "exit_code": 0} for check in state["checks"]]
            if (
                checks != expected
                or not checks
                or any(
                    type(check.get("exit_code")) is not int
                    or type(check.get("timeout_seconds")) is not int
                    for check in checks
                )
            ):
                raise PublicationError(
                    "Publication checks do not match required successful commands"
                )
            tracker = crud_tracker.get_by_id_and_account(
                self.db, id=policy["tracker_id"], account_id=str(self.account_id)
            )
            if tracker is None:
                raise PublicationError("Publication tracker is no longer authorized")
            publishing = self._transition(state, {**state, "phase": "publishing"})
            binding = PublicationBinding(
                policy["repository_url"],
                policy["branch"],
                policy["base"],
                identity["head_sha"],
                policy["expected_remote_sha"],
                (
                    *[
                        PublicationRecord(**record)
                        for record in policy["previous_records"]
                    ],
                    PublicationRecord(str(execution_id), identity["head_sha"]),
                ),
                settings.preloop_url,
                "github",
                policy["configured_title"],
                policy["configured_body"],
                policy["issue_number"],
            )
            try:
                async with httpx.AsyncClient() as client:
                    self.writer = await mint_repository_lease(
                        tracker, binding.repository_url, write=True, client=client
                    )
                self.writer.validate(binding)
                if self._read(execution_id, nonce) != publishing:
                    raise PublicationError(
                        "Publication lease changed during credential issuance"
                    )
                self.expiry_task = asyncio.create_task(
                    self._expire_writer(publishing["deadline"])
                )
                return {
                    **envelope,
                    "type": "publication_publish",
                    "binding": asdict(binding),
                    "lease": {
                        "token": self.writer.token,
                        "repository_url": self.writer.repository_url,
                        "expires_at": self.writer.expires_at.isoformat(),
                    },
                }
            except BaseException:
                await self.revoke()
                raise
        if event == "publication_complete":
            if state["phase"] != "publishing" or self.writer is None:
                raise PublicationError(
                    "Publication completion has no active writer lease"
                )
            receipt = message.get("publication")
            if (
                not isinstance(receipt, dict)
                or len(json.dumps(receipt).encode()) > 65536
                or self.writer.token in json.dumps(receipt)
                or set(receipt)
                - {
                    "url",
                    "number",
                    "branch",
                    "provider",
                    "head_sha",
                    "metadata_warnings",
                }
            ):
                raise PublicationError("Invalid publication receipt")
            number = receipt.get("number")
            project = (
                policy["repository_url"]
                .removeprefix("https://github.com/")
                .removesuffix(".git")
            )
            if (
                type(number) is not int
                or number < 1
                or receipt.get("url") != f"https://github.com/{project}/pull/{number}"
                or receipt.get("branch") != policy["branch"]
                or receipt.get("provider") != "github"
                or receipt.get("head_sha") != identity["head_sha"]
            ):
                raise PublicationError(
                    "Publication receipt does not match its verified binding"
                )
            receipt = {
                **receipt,
                "repository_url": policy["repository_url"],
                "base": policy["base"],
                "records": [
                    *policy["previous_records"],
                    {
                        "execution_id": str(execution_id),
                        "head_sha": identity["head_sha"],
                    },
                ],
            }
            await self.revoke()
            self._transition(
                state,
                {**state, "phase": "complete", "receipt": receipt},
                receipt=receipt,
            )
            return {**envelope, "type": "publication_ack"}
        raise PublicationError("Unknown publication protocol event")
