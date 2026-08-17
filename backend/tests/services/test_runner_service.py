"""Tests for self-hosted runner pool resolution and queue timeout."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from preloop.models.crud.flow_runner import runner_matches_pool
from preloop.services.runner_service import (
    DEFAULT_QUEUE_TIMEOUT,
    hash_runner_token,
    mark_queued_or_fail,
    resolve_runner_pool,
)


def test_resolve_runner_pool_prefers_trigger_override() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"_runner": "office", "ref": "main"}},
    )
    assert pool == "office"


def test_resolve_runner_pool_reads_nested_payload() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"payload": {"_runner": "gpu"}}},
    )
    assert pool == "gpu"


def test_resolve_runner_pool_falls_back_to_flow() -> None:
    flow = SimpleNamespace(runner_pool="nightly")
    assert resolve_runner_pool(flow, {"trigger_event_data": {}}) == "nightly"
    assert resolve_runner_pool(SimpleNamespace(runner_pool=None), {}) is None


def test_mark_queued_or_fail_times_out() -> None:
    now = datetime.now(timezone.utc)
    assert mark_queued_or_fail(queued_since=now) == "PENDING"
    assert (
        mark_queued_or_fail(
            queued_since=now - DEFAULT_QUEUE_TIMEOUT - timedelta(seconds=1)
        )
        == "FAILED"
    )


def test_hash_runner_token_is_stable() -> None:
    assert hash_runner_token("abc") == hash_runner_token("abc")
    assert hash_runner_token("abc") != hash_runner_token("def")


def test_runner_matches_pool_id_name_or_label() -> None:
    runner_id = uuid4()
    row = SimpleNamespace(id=runner_id, name="Office Mac", labels=["local", "gpu"])
    assert runner_matches_pool(row, str(runner_id))
    assert runner_matches_pool(row, "office mac")
    assert runner_matches_pool(row, "GPU")
    assert not runner_matches_pool(row, "missing")
