"""Worker subject selection must never overlap on the workqueue stream.

Regression (0.11.0-rc.0 smoke test): the `tasks` JetStream stream uses
WORKQUEUE retention, where consumer subject filters may not overlap. The
default worker subscribed to `preloop.sync.tasks.*` while the new dedicated
flow-worker filtered `execute_flow` / `resume_flow_execution`, so NATS
rejected every flow-worker subscription with `filtered consumer not unique on
workqueue stream`. With no subscriptions the worker's `gather()` returned
immediately and the container exited 0 into a restart loop.
"""

from __future__ import annotations

import pytest

from preloop.sync.services.nats_worker import PreloopSyncNatsWorker
from preloop.sync.tasks import DISPATCHABLE_TASKS

FLOW_TASKS = ["execute_flow", "resume_flow_execution"]


def _worker(**kwargs) -> PreloopSyncNatsWorker:
    return PreloopSyncNatsWorker(
        nats_url="nats://localhost:4222",
        queue_name="preloop_sync_worker_queue",
        **kwargs,
    )


def test_no_filters_uses_wildcard():
    """A lone worker still consumes everything via the wildcard."""
    assert _worker()._subjects_to_subscribe() == ["preloop.sync.tasks.*"]


def test_allowlist_subscribes_only_those_subjects():
    subjects = _worker(tasks_allowlist=FLOW_TASKS)._subjects_to_subscribe()
    assert subjects == [
        "preloop.sync.tasks.execute_flow",
        "preloop.sync.tasks.resume_flow_execution",
    ]


def test_excludelist_never_uses_wildcard():
    """The excluding pool must enumerate subjects; a wildcard would overlap
    the dedicated pool's filtered consumers and be rejected by NATS."""
    subjects = _worker(tasks_excludelist=FLOW_TASKS)._subjects_to_subscribe()
    assert "preloop.sync.tasks.*" not in subjects
    assert subjects  # it must still consume the remaining tasks


def test_pools_partition_the_stream_without_overlap():
    """Together the two pools cover every dispatchable task exactly once."""
    generic = set(_worker(tasks_excludelist=FLOW_TASKS)._subjects_to_subscribe())
    flow = set(_worker(tasks_allowlist=FLOW_TASKS)._subjects_to_subscribe())

    assert generic & flow == set(), "consumer filters must not overlap"
    expected = {f"preloop.sync.tasks.{name}" for name in DISPATCHABLE_TASKS}
    assert generic | flow == expected, "every dispatchable task must have a consumer"


def test_unknown_excluded_task_is_ignored_not_fatal():
    subjects = _worker(
        tasks_excludelist=["execute_flow", "not_a_task"]
    )._subjects_to_subscribe()
    assert "preloop.sync.tasks.execute_flow" not in subjects
    assert "preloop.sync.tasks.poll_tracker" in subjects


@pytest.mark.parametrize("task", DISPATCHABLE_TASKS)
def test_registry_matches_real_task_functions(task):
    """Every registered task name must exist in preloop.sync.tasks, or a pool
    would subscribe to a subject nothing can handle."""
    from preloop.sync import tasks as task_module

    assert callable(getattr(task_module, task, None)), (
        f"{task} is in DISPATCHABLE_TASKS but is not a function in sync.tasks"
    )
