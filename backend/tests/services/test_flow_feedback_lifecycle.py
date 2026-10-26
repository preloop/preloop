"""Local provider HTTP fixtures through the durable PostgreSQL repair scheduler."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_feedback
from preloop.services.flow_feedback import ingest_feedback, run_feedback_tick
from preloop.services.flow_feedback_provider import FeedbackProvider
from preloop.sync.exceptions import TrackerResponseError
from backend.tests.services import test_flow_feedback_durable as durable_fixtures

NOW = durable_fixtures.NOW
create_thread = durable_fixtures.create_thread
# Pytest discovers shared fixtures through module globals.
database = durable_fixtures.database


class ProviderLifecycle:
    """Provider-owned state, independent of webhook deliveries and worker restarts."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.head = "initial-head"
        self.ci = "failed"
        self.approved = False
        self.closed = False
        self.comments: list[dict[str, Any]] = [
            self.comment(1, "Repair the boundary case")
        ]
        self.reads: list[str] = []

    @staticmethod
    def comment(identity: int, body: str, actor: int = 42) -> dict[str, Any]:
        return {
            "id": identity,
            "body": body,
            "user": {"id": actor, "type": "Bot"},
            "updated_at": f"2026-09-06T00:00:{identity:02}Z",
        }

    def adapter(self, thread: Any) -> FeedbackProvider:
        client = SimpleNamespace(
            _request=AsyncMock(side_effect=self.github),
            _make_request=AsyncMock(side_effect=self.gitlab),
            gl=SimpleNamespace(http_get=object()),
        )
        from preloop.sync.trackers.utils import ConditionalResponse

        async def conditional_get(
            path: str, etag: str | None, **options: Any
        ) -> ConditionalResponse:
            body = await client._make_request(client.gl.http_get, path, **options)
            return ConditionalResponse(200, body, etag)

        client.conditional_get = conditional_get
        return FeedbackProvider(
            client,
            SimpleNamespace(
                provider=self.provider,
                repository_id=thread.repository_id,
                pr_number=thread.pr_number,
                policy=deepcopy(thread.policy),
            ),
        )

    async def github(self, method: str, path: str, data: Any = None) -> Any:
        self.reads.append(path)
        if path.endswith("/pulls/7"):
            return {
                "node_id": "PR_local",
                "state": "closed" if self.closed else "open",
                "head": {"sha": self.head},
                "base": {"ref": "main", "repo": {"id": 123}},
            }
        if path == "/graphql":
            return {
                "data": {
                    "node": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "pageInfo": {"hasNextPage": False},
                                        "nodes": [
                                            {
                                                "databaseId": c["id"],
                                                "body": c["body"],
                                                "url": "https://example.com/review",
                                                "createdAt": c["updated_at"],
                                                "updatedAt": c["updated_at"],
                                                "author": {
                                                    "__typename": "Bot",
                                                    "databaseId": c["user"]["id"],
                                                },
                                            }
                                        ],
                                    },
                                }
                                for c in self.comments
                            ],
                        }
                    }
                }
            }
        if "/check-runs?" in path:
            return {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 10,
                        "name": "tests",
                        "head_sha": self.head,
                        "status": "in_progress"
                        if self.ci == "pending"
                        else "completed",
                        "conclusion": {"failed": "failure", "pending": None}.get(
                            self.ci, self.ci
                        ),
                        "output": {"summary": "test_boundary FAILED token=secret"},
                    }
                ],
            }
        if "/status?" in path:
            return {"total_count": 0, "statuses": []}
        if "/reviews?" in path:
            return (
                [
                    {
                        "id": 20,
                        "user": {"id": 42},
                        "state": "APPROVED",
                        "commit_id": self.head,
                    }
                ]
                if self.approved
                else []
            )
        if path.endswith("/protection"):
            raise TrackerResponseError("Branch not protected", status_code=404)
        if "/issues/" in path or "/rules/branches/" in path:
            return []
        raise AssertionError((method, path))

    async def gitlab(self, method: Any, path: str, **options: Any) -> Any:
        self.reads.append(path)
        if path.endswith("/merge_requests/7"):
            return {
                "project_id": 123,
                "sha": self.head,
                "state": "merged" if self.closed else "opened",
                "blocking_discussions_resolved": not self.comments,
                "head_pipeline": {"id": 30, "sha": self.head, "project_id": 123},
            }
        if "/statuses?" in path:
            return []
        if "/notes?" in path:
            return [
                {
                    **c,
                    "author": {"id": c["user"]["id"], "bot": True},
                    "resolvable": True,
                    "resolved": False,
                }
                for c in self.comments
            ]
        if path.endswith("/approvals"):
            return {
                "approvals_left": 0 if self.approved else 1,
                "approved_by": [{"user": {"id": 42}}] if self.approved else [],
            }
        if "/jobs?" in path:
            return [
                {
                    "id": 10,
                    "name": "tests",
                    "status": self.ci,
                    "failure_reason": "script_failure" if self.ci == "failed" else None,
                    "pipeline": {"id": 30, "sha": self.head, "project_id": 123},
                }
            ]
        if "/trace" in path:
            return "test_boundary FAILED token=secret"
        raise AssertionError(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["github", "gitlab"])
async def test_provider_lifecycle_coalesces_recovers_and_waits_for_human_merge(
    database: Engine,
    provider_name: str,
) -> None:
    remote = ProviderLifecycle(provider_name)
    with Session(database) as db:
        thread = create_thread(db)
        thread.provider = provider_name
        thread.pr_url = f"https://example.com/repo/{'pull' if provider_name == 'github' else '-/merge_requests'}/7"
        thread.context = {
            "trigger": {"source": provider_name},
            "repository": {"id": 123},
            "original_issue": {
                "number": 99,
                "title": "Boundary case",
                "labels": [{"name": "agent-ready"}],
            },
            "acceptance_version": "original-criteria-v1",
        }
        source = db.get(models.FlowExecution, thread.latest_execution_id)
        source.status = "RUNNING"
        thread.active_execution_id = source.id
        flow = db.get(models.Flow, thread.flow_id)
        flow.agent_config = {
            "feedback": {
                "enabled": True,
                "debounce_seconds": 30,
                "required_checks": ["tests"],
                "required_approvals": 1,
                "trusted_reviewer_ids": [42],
                "implementer_actor_ids": [43],
            }
        }
        db.commit()
        thread_id, source_id = thread.id, source.id
        session = deepcopy(source.cli_session)
        route = {
            "account_id": str(thread.account_id),
            "tracker_id": str(thread.tracker_id),
            "source": provider_name,
            "payload": {
                "repository" if provider_name == "github" else "project": {"id": 123},
                "pull_request" if provider_name == "github" else "merge_request": {
                    "number": 7,
                    "iid": 7,
                },
            },
        }
        # Three delivery types signal the same failed job. Duplicate delivery is
        # durable; the job's semantic receipt comes from the provider read.
        for kind in (
            ("check_run", "check_suite", "workflow_run")
            if provider_name == "github"
            else ("pipeline", "job", "status")
        ):
            signal = {**route, "type": kind, "delivery_id": kind}
            assert ingest_feedback(db, signal)
            assert ingest_feedback(db, signal)
        assert len(crud_flow_feedback.pending(db, thread_id)) == 3

    async def provider_for_thread(db: Session, thread: Any) -> FeedbackProvider:
        return remote.adapter(thread)

    async def tick(seconds: int) -> models.FlowThread:
        # A fresh ORM session on every tick represents restart after claim/ack.
        with Session(database, expire_on_commit=False) as db:
            assert (
                await run_feedback_tick(db, now=NOW + timedelta(seconds=seconds)) == 1
            )
            return db.get(models.FlowThread, thread_id)

    with (
        patch(
            "preloop.services.flow_feedback.FeedbackProvider.for_thread",
            side_effect=provider_for_thread,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            AsyncMock(return_value=False),
        ) as dispatch,
    ):
        # Feedback during initial publication must wait for the publisher.
        assert (await tick(0)).active_execution_id == source_id
        dispatch.assert_not_called()
        with Session(database) as db:
            db.get(models.FlowExecution, source_id).status = "SUCCEEDED"
            db.commit()
        remote.comments += [
            remote.comment(2, "Implementer chatter", 43),
            remote.comment(3, "Untrusted bot", 999),
        ]
        assert (await tick(31)).stop_reason == "feedback_debounce"
        repaired = await tick(62)
        assert repaired.turns == 1
        repair_id = repaired.active_execution_id
        with Session(database) as db:
            repair = db.get(models.FlowExecution, repair_id)
            details = repair.trigger_event_details
            assert details["_resume"]["cli_session"] == session
            assert details["_resume"]["source_branch"] == "implementation-7"
            assert details["_feedback"]["acceptance_version"] == "original-criteria-v1"
            assert len(details["_feedback"]["items"]) == 2
            assert "secret" not in str(details["_feedback"])
            assert repair.flow_id == repaired.flow_id
            assert "Untrusted review/CI task data" in details["_feedback_prompt"]
        dispatch.assert_awaited_once_with(repair_id)
        # Dispatch loss cannot reserve again. A late signal remains pending
        # while the repair is active; its webhook's old head is never authority.
        with Session(database) as db:
            assert ingest_feedback(
                db,
                {
                    **route,
                    "type": "comment_created",
                    "delivery_id": "late",
                    "head_sha": "obsolete-head",
                },
            )
        assert (await tick(93)).active_execution_id == repair_id
        dispatch.assert_awaited_once()
        with Session(database) as db:
            assert any(
                item.delivery_id == "late"
                for item in crud_flow_feedback.pending(db, thread_id)
            )
            repair = db.get(models.FlowExecution, repair_id)
            repair.status = "SUCCEEDED"
            repair.cli_session = session
            db.commit()
        # Simulate a published repair head and externally completed CI. This
        # fixture does not execute verification or provider publication writes.
        # Missing success webhooks are recovered by reconciliation.
        remote.head = "verified-repair-head"
        remote.comments = []
        remote.ci = "pending"
        assert (await tick(124)).stop_reason == "ci_pending"
        remote.ci = "success"
        assert (await tick(155)).state == "waiting"
        remote.approved = True
        ready = await tick(186)
        assert ready.state == "ready"
        assert ready.turns == 1
        assert ready.latest_execution_id == repair_id
        assert ready.head_sha == remote.head
        assert not remote.closed  # Readiness never merges the PR.
        with Session(database) as db:
            assert crud_flow_feedback.pending(db, thread_id) == []
        dispatch.assert_awaited_once()
        remote.closed = True
        assert (await tick(217)).state == "closed"
        dispatch.assert_awaited_once()
    assert all("/123/" in path or path == "/graphql" for path in remote.reads)
