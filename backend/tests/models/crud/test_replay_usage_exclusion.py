"""Replay-validation traffic must not leak into user-facing usage numbers.

Replay re-executions are Preloop-driven measurement traffic tagged with
``meta_data.purpose == "replay_validation"``. These tests pin the exclusion
contract across the consumers a replay run could contaminate:

  1. Dashboard usage aggregations (``get_gateway_usage_summary``).
  2. Generic spend reads (``get_gateway_spend`` with no purpose filter).
  3. Budget-bucket accumulation (``log_gateway_request`` -> budget spend).
  4. Session-level metrics (``INTERNAL_USAGE_PURPOSES`` in runtime_session).

The spend stays auditable: a purpose-TARGETED read still sees replay rows.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func

from preloop.models.crud import crud_api_usage
from preloop.models.crud.api_usage import REPLAY_VALIDATION_PURPOSE
from preloop.models.crud.budget import crud_budget_spend
from preloop.models.crud.runtime_session import INTERNAL_USAGE_PURPOSES


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
