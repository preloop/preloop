"""Durable deduplicated observations and five-minute bounded reconciliation."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable, TypeVar
from sqlalchemy.orm import Session
from uuid import UUID

from preloop.config import settings
from preloop.models.crud import readiness
from preloop.models.db.session import get_db_session
from preloop.services.managed_credentials import tracker_credential_source
from preloop.services.readiness.bitbucket import observe_bitbucket
from preloop.services.readiness.conflict import IsolatedConflictProbe
from preloop.sync.trackers.factory import create_tracker_client

from preloop.sync.trackers.bitbucket import BitbucketTracker
from preloop.sync.trackers.jira import JiraTracker

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _transaction(operation: Callable[[Session], _T]) -> _T:
    db = next(get_db_session())
    try:
        result = operation(db)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim() -> tuple[UUID, UUID, UUID, UUID] | None:
    def take(db: Any) -> tuple[UUID, UUID, UUID, UUID] | None:
        job = readiness.claim(db, now=datetime.now(UTC))
        if job is None:
            return None
        assert job.lease_token is not None
        return job.id, job.account_id, job.pr_id, job.lease_token

    return _transaction(take)


async def observe_job(account_id: UUID, pr_id: UUID) -> bool:
    """Resolve binding, capture policy before reads, then CAS during persistence."""

    def context(db: Any) -> Any:
        result = readiness.observation_context(
            db, account_id=account_id, pr_record_id=pr_id
        )
        if result:
            db.expunge_all()
        return result

    bound = await asyncio.to_thread(_transaction, context)
    if bound is None:
        return True
    pr, rollup, jira_row, host_row, repository, forge_pr_id, policy = bound
    if policy is None:
        return True
    source = tracker_credential_source(host_row, repository=repository.split("/", 1)[1])
    jira = JiraTracker(
        str(jira_row.id),
        jira_row.api_key,
        jira_row.connection_details or {},
        initialize_client=False,
    )
    client = await create_tracker_client(
        host_row.tracker_type,
        str(host_row.id),
        host_row.api_key,
        {
            **(host_row.connection_details or {}),
            "auth_type": host_row.auth_type,
            "repo_full_name": repository,
        },
        credential_source=source,
    )
    if not isinstance(jira, JiraTracker) or not isinstance(client, BitbucketTracker):
        return False
    creation = await jira.get_ticket_creation_evidence(rollup.issue_key)
    observation = await observe_bitbucket(
        client,
        IsolatedConflictProbe(source),
        account_id=account_id,
        tracker_id=host_row.id,
        repository=repository,
        pr_id=forge_pr_id,
        policy=policy,
    )

    def persist(db: Any) -> None:
        readiness.persist_ticket_creation(
            db, account_id=account_id, rollup_id=rollup.id, evidence=creation
        )
        readiness.persist_observation(
            db, account_id=account_id, pr_record_id=pr.id, observation=observation
        )

    await asyncio.to_thread(_transaction, persist)
    return any(
        gate.name == "open" and gate.state == "fail" for gate in observation.gates
    )


class ReadinessSweeper:
    """One observation per lease; multi-replica claiming uses SKIP LOCKED."""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.task is None and settings.ticket_readiness_enabled:
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _run(self) -> None:
        last_reconcile = 0.0
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now - last_reconcile >= 300:
                    await asyncio.to_thread(
                        _transaction,
                        lambda db: readiness.reconcile(db, now=datetime.now(UTC)),
                    )
                    last_reconcile = now
                claimed = await asyncio.to_thread(_claim)
                if claimed is None:
                    await asyncio.sleep(15)
                    continue
                job_id, account_id, pr_id, token = claimed
                closed = False
                retry = False
                try:
                    closed = await asyncio.wait_for(observe_job(account_id, pr_id), 160)
                except readiness.PolicyChangedError:
                    retry = True
                except Exception:
                    retry = True
                    logger.warning("Readiness observation unavailable", exc_info=True)
                await asyncio.to_thread(
                    _transaction,
                    lambda db, job_id=job_id, token=token, closed=closed, retry=retry: (
                        readiness.finish(
                            db,
                            job_id=job_id,
                            token=token,
                            closed=closed,
                            retry=retry,
                            now=datetime.now(UTC),
                        )
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Readiness sweep unavailable", exc_info=True)
                await asyncio.sleep(30)


def schedule_pull_request(db: Any, pull: Any) -> None:
    """Event hook records only a durable job; no provider read in the request."""
    if settings.ticket_readiness_enabled and pull is not None:
        readiness.schedule(
            db, account_id=pull.account_id, pr_id=pull.id, now=datetime.now(UTC)
        )
