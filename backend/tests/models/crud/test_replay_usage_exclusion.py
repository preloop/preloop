"""Replay-validation traffic must not leak into user-facing usage numbers.

Replay re-executions are Preloop-driven measurement traffic tagged with
``meta_data.purpose == "replay_validation"``. These tests pin the exclusion
contract across the consumers a replay run could contaminate:

  1. Execution-level usage aggregations.
  2. Dashboard usage aggregations (``get_gateway_usage_summary``).
  3. Generic spend reads (``get_gateway_spend`` with no purpose filter).
  4. Budget-bucket accumulation (``log_gateway_request`` -> budget spend).
  5. Session-level metrics (``INTERNAL_USAGE_PURPOSES`` in runtime_session).

The spend stays auditable: a purpose-TARGETED read still sees replay rows.
"""

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import func

from preloop.models.crud import crud_ai_model, crud_flow, crud_flow_execution
from preloop.models.crud.api_usage import REPLAY_VALIDATION_PURPOSE, crud_api_usage
from preloop.models.crud.budget import crud_budget_spend
from preloop.models.crud.runtime_session import INTERNAL_USAGE_PURPOSES
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def test_get_gateway_usage_for_execution_excludes_replay_rows(
    db_session, create_account, create_user
):
    """Execution totals should count real traffic, not replay validation spend."""
    account = create_account()
    user = create_user(account=account)
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Replay Exclusion Model {uuid.uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"Replay Exclusion Flow {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account.id,
        ),
        account_id=account.id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
    )
    execution_id = str(execution.id)

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        flow_id=str(flow.id),
        flow_execution_id=execution_id,
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost=0.25,
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        flow_id=str(flow.id),
        flow_execution_id=execution_id,
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        estimated_cost=2.5,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )

    usage = crud_api_usage.get_gateway_usage_for_execution(db_session, execution_id)

    assert usage["api_requests"] == 1
    assert usage["token_usage"] == {
        "total_tokens": 150,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "uncached_input_tokens": 0,
    }
    assert usage["estimated_cost"] == 0.25
    assert usage["has_pricing"] is True


def _log_gateway_row(
    db_session,
    test_user,
    *,
    estimated_cost: float,
    total_tokens: int,
    meta_data=None,
):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=total_tokens - 5,
        completion_tokens=5,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        meta_data=meta_data,
    )


def _total_budget_spend(db_session, account_id) -> float:
    """Sum every budget-spend bucket for the account (all subjects/periods)."""
    model = crud_budget_spend.model
    total = (
        db_session.query(func.coalesce(func.sum(model.spend_usd), 0.0))
        .filter(model.account_id == account_id)
        .scalar()
    )
    return float(total or 0.0)


def test_replay_rows_excluded_from_usage_summary(db_session, test_user):
    """Dashboard totals reflect agent traffic only, never replay runs."""
    _log_gateway_row(
        db_session, test_user, estimated_cost=0.05, total_tokens=20, meta_data=None
    )
    _log_gateway_row(
        db_session,
        test_user,
        estimated_cost=0.30,
        total_tokens=999,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )

    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC) + timedelta(hours=1)
    totals = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start,
        end_date=end,
    )
    assert totals["request_count"] == 1
    assert totals["total_tokens"] == 20
    assert totals["estimated_cost"] == 0.05


def test_replay_rows_excluded_from_generic_spend_but_auditable_by_purpose(
    db_session, test_user
):
    """Untargeted spend reads exclude replay; purpose-targeted reads see it."""
    _log_gateway_row(
        db_session, test_user, estimated_cost=0.05, total_tokens=20, meta_data=None
    )
    _log_gateway_row(
        db_session,
        test_user,
        estimated_cost=0.30,
        total_tokens=999,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )

    start = datetime.now(UTC) - timedelta(hours=1)
    generic = crud_api_usage.get_gateway_spend(
        db_session, account_id=str(test_user.account_id), start=start
    )
    assert generic == 0.05

    targeted = crud_api_usage.get_gateway_spend(
        db_session,
        account_id=str(test_user.account_id),
        start=start,
        purpose=REPLAY_VALIDATION_PURPOSE,
    )
    assert targeted == 0.30


def test_replay_rows_do_not_accumulate_budget_spend(db_session, test_user):
    """Replay spend never consumes budget-bucket headroom; real traffic does."""
    _log_gateway_row(
        db_session,
        test_user,
        estimated_cost=0.30,
        total_tokens=999,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )
    assert _total_budget_spend(db_session, test_user.account_id) == 0.0

    _log_gateway_row(
        db_session, test_user, estimated_cost=0.05, total_tokens=20, meta_data=None
    )
    assert _total_budget_spend(db_session, test_user.account_id) > 0.0


def test_untagged_and_other_purpose_rows_unaffected(db_session, test_user):
    """The NULL-safe exclusion keeps NULL/other-purpose rows in the numbers."""
    _log_gateway_row(
        db_session,
        test_user,
        estimated_cost=0.02,
        total_tokens=10,
        meta_data={"other_key": "x"},  # meta_data present, purpose NULL
    )
    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC) + timedelta(hours=1)
    totals = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start,
        end_date=end,
    )
    assert totals["request_count"] == 1
    assert totals["total_tokens"] == 10


def test_replay_purpose_registered_as_internal_session_usage():
    """Session-level metrics exclude replay rows via INTERNAL_USAGE_PURPOSES."""
    assert REPLAY_VALIDATION_PURPOSE in INTERNAL_USAGE_PURPOSES


def test_exclude_replay_usage_condition_expression():
    """Unit-level pin: filter uses REPLAY_VALIDATION_PURPOSE and is NULL-safe."""
    from preloop.models.crud.api_usage import exclude_replay_usage_condition

    condition = exclude_replay_usage_condition()
    compiled = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert REPLAY_VALIDATION_PURPOSE in compiled
    # OR of IS NULL / != purpose — NULL-safe inclusion of untagged rows.
    assert "IS NULL" in compiled.upper() or "is null" in compiled.lower()
