"""Default per-execution ceilings for flows that set none.

There is no built-in default: an unset flow runs without a ceiling. An
operator can set ``PRELOOP_DEFAULT_*`` to give every flow that does not set a
ceiling itself a default (``0`` or an unusable value means none). An explicit
value on the flow wins, a subscription run billed at $0 is still stopped by
tokens and turns, and a default that fires ends the run with
``default_limit_reached`` and a message naming the knob and the value.
"""

from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate, FlowResponse
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_execution_limits import (
    DEFAULT_LIMIT_ENV_VARS,
    ExecutionBudgetExceededError,
    ExecutionUsage,
    enforce_execution_limits_for_id,
    evaluate_execution_limits,
    resolve_execution_limits,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_BUDGET_EXCEEDED,
    FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED,
    derive_failure_category,
)


@pytest.fixture(autouse=True)
def _no_env_defaults(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in DEFAULT_LIMIT_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    yield


def test_unset_flow_has_no_ceiling() -> None:
    limits = resolve_execution_limits({})
    assert limits.as_dict() == {}
    assert limits.is_empty
    assert (
        evaluate_execution_limits(
            limits, ExecutionUsage(total_tokens=50_000_000, cost_usd=500, turns=5000)
        )
        is None
    )


def test_environment_default_stops_an_unset_flow_with_its_own_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TOTAL_TOKENS", "2000000")
    limits = resolve_execution_limits(None)
    assert limits.sources["max_total_tokens"] == "environment"
    violation = evaluate_execution_limits(
        limits, ExecutionUsage(total_tokens=2_000_001, cost_usd=1.0, turns=3)
    )
    assert violation is not None
    assert violation.kind == "max_total_tokens"
    assert violation.category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    assert "default execution token ceiling reached" in violation.message
    assert "PRELOOP_DEFAULT_MAX_TOTAL_TOKENS=2000000" in violation.message
    assert "agent_config.limits.max_total_tokens" in violation.message


def test_explicit_flow_value_is_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TOTAL_TOKENS", "2000000")
    limits = resolve_execution_limits({"limits": {"max_total_tokens": 5_000_000}})
    assert limits.max_total_tokens == 5_000_000
    assert limits.sources["max_total_tokens"] == "flow"
    assert (
        evaluate_execution_limits(limits, ExecutionUsage(total_tokens=3_000_000))
        is None
    )
    violation = evaluate_execution_limits(
        limits, ExecutionUsage(total_tokens=5_000_000)
    )
    assert violation is not None
    assert violation.category == FAILURE_CATEGORY_BUDGET_EXCEEDED
    assert violation.message.startswith("execution token ceiling reached")


def test_environment_values_set_defaults_and_flow_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", "250")
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_USD", "2.5")
    limits = resolve_execution_limits({})
    assert limits.max_turns == 250
    assert limits.max_usd == 2.5
    assert limits.max_total_tokens is None
    violation = evaluate_execution_limits(limits, ExecutionUsage(turns=250))
    assert violation is not None
    assert violation.category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    assert "PRELOOP_DEFAULT_MAX_TURNS=250" in violation.message
    assert resolve_execution_limits({"limits": {"max_turns": 7}}).max_turns == 7


@pytest.mark.parametrize("raw", ["0", "lots", "-5", "nan", "inf", "-inf", ""])
def test_zero_or_unusable_environment_value_means_no_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    for name in DEFAULT_LIMIT_ENV_VARS.values():
        monkeypatch.setenv(name, raw)
    assert resolve_execution_limits({}).as_dict() == {}


@pytest.mark.parametrize(
    ("usage", "kind"),
    [
        (
            ExecutionUsage(total_tokens=2_000_000, cost_usd=0.0, turns=5),
            "max_total_tokens",
        ),
        (ExecutionUsage(total_tokens=1_000, cost_usd=0.0, turns=100), "max_turns"),
    ],
)
def test_zero_cost_subscription_run_still_stops_on_tokens_and_turns(
    usage: ExecutionUsage, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TOTAL_TOKENS", "2000000")
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", "100")
    violation = evaluate_execution_limits(resolve_execution_limits({}), usage)
    assert violation is not None
    assert violation.kind == kind


def test_default_message_classifies_as_default_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", "100")
    violation = evaluate_execution_limits(
        resolve_execution_limits({}), ExecutionUsage(turns=100)
    )
    assert violation is not None
    message = f"Execution budget exceeded: {violation.message}"
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    )


def test_flow_response_reports_effective_limits(monkeypatch) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_USD", "4")
    described = resolve_execution_limits({"limits": {"max_turns": 30}}).describe()
    assert described == {
        "max_usd": {"value": 4.0, "source": "environment"},
        "max_turns": {"value": 30, "source": "flow"},
    }
    assert "effective_limits" in FlowResponse.model_fields


def test_gateway_refusal_marks_default_limit_reached(
    db_session: Session, test_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", "100")
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Unbounded Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    for _ in range(100):
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            flow_id=str(flow.id),
            flow_execution_id=str(execution.id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            prompt_tokens=10,
            completion_tokens=0,
            total_tokens=10,
            estimated_cost=0.0,
            cost_source="subscription",
        )
    db_session.commit()

    with pytest.raises(ExecutionBudgetExceededError) as raised:
        enforce_execution_limits_for_id(db_session, execution_id=execution.id)

    assert raised.value.category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    assert "PRELOOP_DEFAULT_MAX_TURNS=100" in execution.error_message


@pytest.mark.parametrize(
    ("agent_config", "expected"),
    [
        ({}, {}),
        (
            {"limits": {"max_total_tokens": 900}},
            {"max_total_tokens": {"value": 900, "source": "flow"}},
        ),
    ],
)
def test_flow_response_fills_effective_limits(
    agent_config: dict, expected: dict
) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id=uuid4(),
        name="Review",
        prompt_template="Review.",
        agent_type="codex",
        agent_config=agent_config,
        created_at=now,
        updated_at=now,
    )
    response = FlowResponse.model_validate(row)
    assert (response.effective_limits or {}) == expected
