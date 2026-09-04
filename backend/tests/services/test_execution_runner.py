"""Unit tests for the execution runner read model.

The console names where a run executed. That answer is derived from
``runner_id`` and ``agent_session_reference`` ``runner:...`` forms, not a
new column, so hosted runs still have a runner object.
"""

from __future__ import annotations

from uuid import uuid4

from preloop.models.schemas.flow_execution import (
    ExecutionRunner,
    ExecutionRunnerSummary,
    FlowExecutionListResponse,
    FlowExecutionResponse,
)
from preloop.services.runner_service import (
    HOSTED_RUNNER_NAME,
    PRIVATE_RUNNER_FALLBACK_NAME,
    derive_execution_runner,
    pool_from_session_reference,
    runner_id_from_session_reference,
)


def test_hosted_when_no_runner_signal() -> None:
    payload = derive_execution_runner()
    runner = ExecutionRunner.model_validate(payload)

    assert runner.kind == "hosted"
    assert runner.name == HOSTED_RUNNER_NAME
    assert runner.id is None
    assert runner.pool is None


def test_private_from_runner_id_and_name() -> None:
    runner_id = uuid4()
    payload = derive_execution_runner(
        runner_id=runner_id, runner_name="Office Mac", pool="gpu"
    )
    runner = ExecutionRunner.model_validate(payload)

    assert runner.kind == "private"
    assert runner.id == runner_id
    assert runner.name == "Office Mac"
    assert runner.pool == "gpu"


def test_private_from_assigned_session_reference() -> None:
    runner_id = uuid4()
    execution_id = uuid4()
    ref = f"runner:{runner_id}:{execution_id}"

    assert runner_id_from_session_reference(ref) == runner_id
    payload = derive_execution_runner(
        agent_session_reference=ref, runner_name="Office Mac"
    )
    runner = ExecutionRunner.model_validate(payload)

    assert runner.kind == "private"
    assert runner.id == runner_id
    assert runner.name == "Office Mac"


def test_private_from_queued_session_reference() -> None:
    execution_id = uuid4()
    ref = f"runner:queued:office:{execution_id}"

    assert runner_id_from_session_reference(ref) is None
    assert pool_from_session_reference(ref) == "office"
    payload = derive_execution_runner(agent_session_reference=ref)
    runner = ExecutionRunner.model_validate(payload)

    assert runner.kind == "private"
    assert runner.id is None
    assert runner.name == PRIVATE_RUNNER_FALLBACK_NAME
    assert runner.pool == "office"


def test_list_schema_carries_kind_and_name_only() -> None:
    fields = set(ExecutionRunnerSummary.model_fields)
    assert fields == {"kind", "name"}
    summary = ExecutionRunnerSummary.model_validate(
        {"kind": "hosted", "name": HOSTED_RUNNER_NAME, "id": uuid4(), "pool": "x"}
    )
    assert summary.kind == "hosted"
    assert not hasattr(summary, "id") or "id" not in summary.model_fields


def test_response_schemas_default_to_hosted() -> None:
    assert "runner" in FlowExecutionResponse.model_fields
    assert "runner" in FlowExecutionListResponse.model_fields
    default_detail = FlowExecutionResponse.model_fields["runner"].default_factory()
    default_list = FlowExecutionListResponse.model_fields["runner"].default_factory()
    assert default_detail.kind == "hosted"
    assert default_detail.name == HOSTED_RUNNER_NAME
    assert default_list.kind == "hosted"
    assert default_list.name == HOSTED_RUNNER_NAME
