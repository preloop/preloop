"""Kubernetes agent Job creation: naming, conflict resolution, retries.

These tests pin the behaviour that turned a recoverable provider blip into a
failed run on staging: the second attempt of a retried execution asked
Kubernetes for the Job name the first attempt still owned and died with
``Failed to start agent Job: (409) Conflict``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from preloop.agents.container import (
    AGENT_SESSION_SUFFIX_KEY,
    ContainerAgentExecutor,
    K8S_NAME_MAX_LENGTH,
    kubernetes_job_name,
)
from preloop.agents.errors import AgentStartError
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_RUNNER_CONFLICT,
    FAILURE_CATEGORY_RUNNER_ERROR,
)

pytestmark = pytest.mark.asyncio


EXECUTION_ID = "0f5a0a1e-3a4f-4f0e-9a0a-3c1e2d4b5a6c"


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retry backoff must not slow the suite; assert on calls instead."""
    slept = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "preloop.agents.container._sleep_before_job_create_retry", _record
    )
    return slept


@pytest.fixture
def executor():
    ex = ContainerAgentExecutor(
        agent_type="codex",
        config={},
        image="test-image:latest",
        use_kubernetes=True,
    )
    ex.agent_namespace = "preloop-agents"
    ex._k8s_batch_api = AsyncMock()
    return ex


def _api_error(status: int) -> ApiException:
    return ApiException(status=status, reason="Conflict" if status == 409 else "Error")


def _job(*, execution_id=EXECUTION_ID, active=None, succeeded=None, failed=None):
    """Minimal V1Job stand-in: only what the conflict logic reads.

    ``execution_id=None`` models a Job carrying no ownership label at all.
    """
    labels = {} if execution_id is None else {"preloop.execution_id": execution_id}
    return SimpleNamespace(
        metadata=SimpleNamespace(labels=labels),
        status=SimpleNamespace(
            active=active,
            succeeded=succeeded,
            failed=failed,
            completion_time=None if active else object(),
        ),
    )


class TestJobName:
    """Job names must be unique per session and valid DNS-1123 labels."""

    def test_first_attempt_keeps_historic_name(self):
        # An in-flight run started before this change is addressed by
        # agent-<execution_id>; changing that would orphan it on deploy.
        assert kubernetes_job_name(EXECUTION_ID) == f"agent-{EXECUTION_ID}"

    def test_suffix_distinguishes_sessions(self):
        """The regression: attempt 2 and the nudge must not reuse a name."""
        first = kubernetes_job_name(EXECUTION_ID)
        second = kubernetes_job_name(EXECUTION_ID, session_suffix="a2")
        nudge = kubernetes_job_name(EXECUTION_ID, session_suffix="nudge")
        assert len({first, second, nudge}) == 3
        assert second.endswith("-a2")
        assert EXECUTION_ID[:8] in second

    @pytest.mark.parametrize("suffix", [None, "", "a2", "nudge", "A_2", "a/2"])
    def test_names_are_valid_dns_labels(self, suffix):
        import re

        name = kubernetes_job_name(str(uuid.uuid4()), session_suffix=suffix)
        assert len(name) <= K8S_NAME_MAX_LENGTH
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name

    def test_long_execution_id_is_truncated_with_suffix_intact(self):
        name = kubernetes_job_name("x" * 200, session_suffix="nudge")
        assert len(name) == K8S_NAME_MAX_LENGTH
        assert name.endswith("-nudge")

    def test_suffix_key_is_read_from_execution_context(self):
        """The orchestrator passes the suffix through the execution context."""
        assert AGENT_SESSION_SUFFIX_KEY == "agent_session_suffix"


class TestCreateJobConflict:
    """409 AlreadyExists must never fail a run that can still be started."""

    async def test_plain_create_returns_name(self, executor):
        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )
        assert name == "agent-x"
        executor._k8s_batch_api.create_namespaced_job.assert_awaited_once()

    async def test_conflict_with_live_job_of_same_execution_is_adopted(self, executor):
        """A duplicate dispatch must adopt the running agent, not fail."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(active=1)

        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )

        assert name == "agent-x"
        # Adopted, so exactly one create was attempted and nothing deleted.
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()

    async def test_conflict_with_finished_job_deletes_then_recreates(self, executor):
        """The staging shape: a finished Job lingering inside its TTL."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = [
            _api_error(409),
            None,
        ]
        executor._k8s_batch_api.read_namespaced_job.side_effect = [
            _job(succeeded=1),  # conflict lookup: finished
            _api_error(404),  # deletion confirmed
        ]

        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )

        assert name == "agent-x"
        delete_kwargs = executor._k8s_batch_api.delete_namespaced_job.await_args.kwargs
        # Without background propagation the Job's pods outlive it and keep
        # the name's resources around.
        assert delete_kwargs["propagation_policy"] == "Background"
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 2

    async def test_conflict_with_vanished_job_just_retries(self, executor):
        executor._k8s_batch_api.create_namespaced_job.side_effect = [
            _api_error(409),
            None,
        ]
        executor._k8s_batch_api.read_namespaced_job.side_effect = _api_error(404)

        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )

        assert name == "agent-x"
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()

    async def test_conflict_with_other_execution_fails_immediately(self, executor):
        """Never adopt or delete another execution's agent."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(
            execution_id="somebody-else", active=1
        )

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_CONFLICT
        assert "somebody-else" in str(excinfo.value)
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1

    async def test_conflict_with_unlabelled_live_job_fails_closed(self, executor):
        """An unlabelled Job is not provably ours, so it is never adopted."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(
            execution_id=None, active=1
        )

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_CONFLICT
        assert "unknown owner" in str(excinfo.value)
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1

    async def test_conflict_with_unlabelled_finished_job_is_not_deleted(self, executor):
        """Nor deleted: the guard must not destroy a stranger's Job."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(
            execution_id=None, succeeded=1
        )

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_CONFLICT
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()

    async def test_unresolvable_conflict_is_categorised(self, executor, monkeypatch):
        """Exhausting attempts on a 409 is a runner_conflict, not unknown."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.side_effect = _api_error(404)

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_CONFLICT


class TestCreateJobApiErrors:
    """Control-plane blips are retried; real errors are not."""

    async def test_server_error_is_retried(self, executor, no_backoff_sleep):
        executor._k8s_batch_api.create_namespaced_job.side_effect = [
            _api_error(503),
            None,
        ]

        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )

        assert name == "agent-x"
        assert len(no_backoff_sleep) == 1
        assert no_backoff_sleep[0] > 0  # jittered backoff, not a busy loop

    async def test_rate_limit_is_retried(self, executor):
        executor._k8s_batch_api.create_namespaced_job.side_effect = [
            _api_error(429),
            None,
        ]
        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )
        assert name == "agent-x"

    async def test_forbidden_is_terminal(self, executor):
        """403 will not fix itself; the user must see it immediately."""
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(403)

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_ERROR
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1

    async def test_attempts_are_bounded_by_settings(self, executor, monkeypatch):
        monkeypatch.setattr(
            "preloop.agents.container.settings.agent_job_create_max_attempts", 2
        )
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(500)

        with pytest.raises(AgentStartError):
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert executor._k8s_batch_api.create_namespaced_job.await_count == 2

    async def test_single_attempt_setting_disables_retry(self, executor, monkeypatch):
        monkeypatch.setattr(
            "preloop.agents.container.settings.agent_job_create_max_attempts", 1
        )
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(500)

        with pytest.raises(AgentStartError):
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1

    async def test_single_attempt_setting_keeps_the_leftover_on_conflict(
        self, executor, monkeypatch
    ):
        """Disabling the retry must not delete a Job nothing will recreate.

        Deleting the finished leftover only pays off if a further create
        follows. With the budget at one attempt the run fails either way, so
        the leftover (and its logs, the only record of the previous session)
        stays.
        """
        monkeypatch.setattr(
            "preloop.agents.container.settings.agent_job_create_max_attempts", 1
        )
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(succeeded=1)

        with pytest.raises(AgentStartError) as excinfo:
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert excinfo.value.category == FAILURE_CATEGORY_RUNNER_CONFLICT
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()
        assert executor._k8s_batch_api.create_namespaced_job.await_count == 1

    async def test_single_attempt_setting_still_adopts_a_live_job(
        self, executor, monkeypatch
    ):
        """Adoption needs no further attempt, so the budget does not block it."""
        monkeypatch.setattr(
            "preloop.agents.container.settings.agent_job_create_max_attempts", 1
        )
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(active=1)

        name = await executor._create_kubernetes_job(
            object(), job_name="agent-x", execution_id=EXECUTION_ID
        )

        assert name == "agent-x"
        executor._k8s_batch_api.delete_namespaced_job.assert_not_awaited()

    async def test_last_attempt_never_deletes_the_leftover(self, executor, monkeypatch):
        """The same rule on the final attempt of a wider budget."""
        monkeypatch.setattr(
            "preloop.agents.container.settings.agent_job_create_max_attempts", 3
        )
        executor._k8s_batch_api.create_namespaced_job.side_effect = _api_error(409)
        executor._k8s_batch_api.read_namespaced_job.return_value = _job(succeeded=1)

        with pytest.raises(AgentStartError):
            await executor._create_kubernetes_job(
                object(), job_name="agent-x", execution_id=EXECUTION_ID
            )

        assert executor._k8s_batch_api.create_namespaced_job.await_count == 3
        # Attempts 1 and 2 free the name for the create that follows them;
        # attempt 3 has nothing to free it for.
        assert executor._k8s_batch_api.delete_namespaced_job.await_count == 2
