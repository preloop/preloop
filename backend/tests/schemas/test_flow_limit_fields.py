"""Flow ``max_budget`` / ``max_iterations`` map onto the enforced run limits.

The console's flow form sends a per-run spend limit and an iteration limit as
top-level fields. The backend used to drop both silently, so a user who set
"$5 per run" got no limit at all. They are now folded into
``agent_config.limits`` (``max_usd`` / ``max_turns``), which the model gateway
enforces on every request of a run, and read back from there in responses.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.models import schemas
from preloop.models.schemas.flow import apply_flow_limit_fields


def _create(**overrides):
    payload = {
        "name": "Nightly triage",
        "prompt_template": "Triage new issues.",
        "agent_config": {},
    }
    payload.update(overrides)
    return schemas.FlowCreate(**payload)


def test_create_folds_both_fields_into_agent_config_limits():
    flow = _create(max_budget=5, max_iterations=40)

    assert flow.agent_config["limits"] == {"max_usd": 5.0, "max_turns": 40}


def test_create_keeps_other_limits_and_agent_config_keys():
    flow = _create(
        agent_config={
            "max_iterations": 12,
            "limits": {"max_total_tokens": 2_000_000, "max_usd": 50},
        },
        max_budget=7.5,
    )

    assert flow.agent_config["max_iterations"] == 12
    assert flow.agent_config["limits"] == {
        "max_total_tokens": 2_000_000,
        "max_usd": 7.5,
    }


def test_fields_are_not_columns_so_model_dump_leaves_them_out():
    # crud_flow.create builds the ORM row from model_dump(); a key without a
    # column would raise there.
    dumped = _create(max_budget=5, max_iterations=40).model_dump()

    assert "max_budget" not in dumped
    assert "max_iterations" not in dumped
    assert dumped["agent_config"]["limits"] == {"max_usd": 5.0, "max_turns": 40}


def test_explicit_null_clears_that_limit_only():
    flow = _create(
        agent_config={"limits": {"max_usd": 50, "max_turns": 300}},
        max_budget=None,
    )

    assert flow.agent_config["limits"] == {"max_turns": 300}


def test_clearing_the_last_limit_removes_the_limits_key():
    flow = _create(agent_config={"limits": {"max_usd": 50}}, max_budget=None)

    assert "limits" not in flow.agent_config


def test_unset_fields_leave_stored_limits_alone():
    flow = _create(agent_config={"limits": {"max_usd": 50}})

    assert flow.agent_config["limits"] == {"max_usd": 50}


def test_caller_agent_config_is_not_mutated():
    agent_config = {"limits": {"max_usd": 50}}
    _create(agent_config=agent_config, max_budget=5)

    assert agent_config == {"limits": {"max_usd": 50}}


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_budget", 0),
        ("max_budget", -1),
        ("max_budget", 2_000_000),
        ("max_iterations", 0),
        ("max_iterations", 2.5),
    ],
)
def test_out_of_range_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        _create(**{field: value})


def test_update_without_agent_config_defers_the_merge_to_the_stored_config():
    update = schemas.FlowUpdate(max_budget=5)

    # The endpoint folds it into the stored agent_config; the schema must not
    # invent an agent_config that would replace the stored one wholesale.
    assert update.agent_config is None
    assert update.max_budget == 5
    assert "max_budget" in update.model_fields_set


def test_apply_helper_merges_onto_a_stored_config():
    stored = {"agent_type": "CodeActAgent", "limits": {"max_total_tokens": 10}}

    merged = apply_flow_limit_fields(stored, {"max_budget": 3, "max_iterations": None})

    assert merged == {
        "agent_type": "CodeActAgent",
        "limits": {"max_total_tokens": 10, "max_usd": 3.0},
    }
    assert stored == {
        "agent_type": "CodeActAgent",
        "limits": {"max_total_tokens": 10},
    }


def test_response_reads_both_fields_back_from_the_enforced_limits():
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id=uuid4(),
        name="Nightly triage",
        prompt_template="Triage new issues.",
        agent_type="openhands",
        agent_config={"limits": {"max_usd": 5.0, "max_turns": 40}},
        created_at=now,
        updated_at=now,
    )

    response = schemas.FlowResponse.model_validate(row)
    body = response.model_dump(mode="json")

    assert response.max_budget == 5.0
    assert response.max_iterations == 40
    assert body["max_budget"] == 5.0
    assert body["max_iterations"] == 40


def test_response_without_limits_reports_no_limit():
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id=uuid4(),
        name="Nightly triage",
        prompt_template="Triage new issues.",
        agent_type="openhands",
        agent_config={},
        created_at=now,
        updated_at=now,
    )

    body = schemas.FlowResponse.model_validate(row).model_dump(mode="json")

    assert body["max_budget"] is None
    assert body["max_iterations"] is None
