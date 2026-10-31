"""Default per-execution ceilings for flows that set none.

Pins the four decisions: an unset flow hits the built-in default; an explicit
value on the flow wins; a ``PRELOOP_DEFAULT_*`` environment value wins over
the built-in default (``0`` removes it); and a subscription run billed at $0
is still stopped by tokens and turns. A default that fires ends the run with
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


def test_unset_flow_gets_the_builtin_defaults() -> None:
    limits = resolve_execution_limits({})
    assert limits.as_dict() == {
        "max_total_tokens": 2_000_000,
        "max_usd": 10.0,
        "max_turns": 100,
    }
    assert set(limits.sources.values()) == {"default"}


def test_unset_flow_hits_the_default_token_ceiling_with_its_own_category() -> None:
    limits = resolve_execution_limits(None)
    violation = evaluate_execution_limits(
        limits, ExecutionUsage(total_tokens=2_000_001, cost_usd=1.0, turns=3)
    )
    assert violation is not None
    assert violation.kind == "max_total_tokens"
    assert violation.category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    assert "default execution token ceiling reached" in violation.message
    assert "max_total_tokens=2000000" in violation.message
    assert "agent_config.limits.max_total_tokens" in violation.message
    assert "PRELOOP_DEFAULT_MAX_TOTAL_TOKENS" in violation.message


def test_explicit_flow_value_wins_over_the_default() -> None:
    limits = resolve_execution_limits({"limits": {"max_total_tokens": 5_000_000}})
    assert limits.max_total_tokens == 5_000_000
    assert limits.sources["max_total_tokens"] == "flow"
    # Past the default but under the flow's own ceiling: allowed.
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


def test_environment_value_wins_over_the_builtin_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", "250")
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_USD", "2.5")
    limits = resolve_execution_limits({})
    assert limits.max_turns == 250
    assert limits.max_usd == 2.5
    assert limits.sources["max_turns"] == "environment"
    violation = evaluate_execution_limits(limits, ExecutionUsage(turns=250))
    assert violation is not None
    assert violation.category == FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
    assert "PRELOOP_DEFAULT_MAX_TURNS=250" in violation.message
    # A flow value still wins over the environment.
    assert resolve_execution_limits({"limits": {"max_turns": 7}}).max_turns == 7


def test_environment_zero_disables_that_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TOTAL_TOKENS", "0")
    limits = resolve_execution_limits({})
    assert limits.max_total_tokens is None
    assert "max_total_tokens" not in limits.sources
    assert limits.max_turns == 100


@pytest.mark.parametrize("raw", ["lots", "-5", "1.5"])
def test_unusable_environment_value_keeps_the_builtin_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("PRELOOP_DEFAULT_MAX_TURNS", raw)
    limits = resolve_execution_limits({})
    assert limits.max_turns == 100
    assert limits.sources["max_turns"] == "default"


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
    usage: ExecutionUsage, kind: str
) -> None:
    violation = evaluate_execution_limits(resolve_execution_limits({}), usage)
    assert violation is not None
    assert violation.kind == kind


def test_default_message_classifies_as_default_limit_reached() -> None:
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
    from preloop.services.flow_execution_limits import resolve_execution_limits

    described = resolve_execution_limits({"limits": {"max_turns": 30}}).describe()
    assert described == {
        "max_total_tokens": {"value": 2_000_000, "source": "default"},
        "max_usd": {"value": 4.0, "source": "environment"},
        "max_turns": {"value": 30, "source": "flow"},
    }
    assert "effective_limits" in FlowResponse.model_fields


def test_gateway_refusal_marks_default_limit_reached(
    db_session: Session, test_user
) -> None:
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
    assert "max_turns=100" in execution.error_message
