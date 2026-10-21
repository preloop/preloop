"""Retired global models preserve history without serving new default selection."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.crud import crud_ai_model


def _model(
    *, owned: bool = False, retired: object = None, default: bool = False
) -> models.AIModel:
    return models.AIModel(
        id=uuid4(),
        account_id=uuid4() if owned else None,
        name="Example model",
        provider_name="openai",
        model_identifier="example-model",
        api_key="synthetic-provider-key",
        meta_data={} if retired is None else {"retired": retired},
        is_default=default,
    )


def _database(rows: list[models.AIModel]) -> MagicMock:
    db = MagicMock()
    query = db.query.return_value
    for method in ("options", "filter", "order_by"):
        getattr(query, method).return_value = query
    query.all.return_value = rows
    return db


@pytest.mark.parametrize("shared", [False, True])
def test_discovery_excludes_only_explicitly_retired_global_rows(shared: bool) -> None:
    retired = _model(retired=True)
    active = _model()
    own = _model(owned=True, retired=True)
    legacy_string = _model(retired="true")
    rows = [own, retired, active, legacy_string]
    db = _database(rows)
    with patch(
        "preloop.plugins.account_hooks.extra_visible_ids",
        return_value=[own.id] if shared else [],
    ):
        selected = crud_ai_model.get_all_for_account(db, account_id=own.account_id)
    assert selected == [own, active, legacy_string]
    assert retired.meta_data == {"retired": True}


@pytest.mark.parametrize("account_id", [None, str(uuid4())])
def test_retired_global_default_cannot_win_or_be_fallback(
    account_id: str | None,
) -> None:
    retired = _model(retired=True, default=True)
    active = _model()
    assert (
        crud_ai_model.get_default_active_model(
            _database([retired, active]), account_id=account_id
        )
        is active
    )
    assert (
        crud_ai_model.get_default_active_model(
            _database([retired]), account_id=account_id
        )
        is None
    )


def test_owned_model_retirement_marker_does_not_disable_byok() -> None:
    owned = _model(owned=True, retired=True, default=True)
    assert (
        crud_ai_model.get_default_active_model(
            _database([owned]), account_id=str(owned.account_id)
        )
        is owned
    )


def test_retired_global_model_is_still_readable_by_id() -> None:
    retired = _model(retired=True)
    db = _database([retired])
    db.query.return_value.filter.return_value.first.return_value = retired
    assert crud_ai_model.get(db, id=retired.id) is retired


def test_existing_disabled_gateway_marker_retires_global_model() -> None:
    global_row = _model(default=True)
    global_row.meta_data = {"gateway": {"enabled": False}}
    own = _model(owned=True, default=True)
    own.meta_data = {"gateway": {"enabled": False}}
    db = _database([global_row, own])
    with patch("preloop.plugins.account_hooks.extra_visible_ids", return_value=[]):
        assert crud_ai_model.get_all_for_account(db, account_id=own.account_id) == [own]
    assert (
        crud_ai_model.get_default_active_model(db, account_id=str(own.account_id))
        is own
    )


@pytest.mark.parametrize(
    "retirement", [{"retired": True}, {"gateway": {"enabled": False}}]
)
def test_selectable_catalog_and_built_in_availability_share_retirement(
    retirement: dict[str, object],
) -> None:
    from preloop.services.built_in_models import (
        is_available_built_in_model,
    )

    retired = _model()
    retired.meta_data = {
        "gateway": {"enabled": True, "model_alias": "example/operator-model"},
        **retirement,
    }
    active = _model()
    active.meta_data = {
        "gateway": {"enabled": True, "model_alias": "example/operator-model"}
    }
    own = _model(owned=True, retired=True)
    selected = crud_ai_model.get_selectable_for_account(
        _database([retired, active, own]),
        account_id=own.account_id,
        include_built_in=True,
    )
    assert selected == [active, own]
    assert not is_available_built_in_model(retired)
    assert is_available_built_in_model(active)


@pytest.mark.parametrize("shared", [False, True])
def test_historical_discovery_keeps_retired_rows(shared: bool) -> None:
    retired = _model(retired=True)
    own = _model(owned=True)
    with patch(
        "preloop.plugins.account_hooks.extra_visible_ids",
        return_value=[own.id] if shared else [],
    ):
        assert crud_ai_model.get_all_for_account(
            _database([own, retired]), account_id=own.account_id, include_retired=True
        ) == [own, retired]


def test_historical_replay_resolves_retired_gateway_alias() -> None:
    from preloop.services.replay_savings_service import resolve_payload_ai_model

    retired = _model(retired=True)
    retired.meta_data["gateway"] = {"enabled": False, "model_alias": "old/model"}
    account = models.Account(id=uuid4())
    with patch("preloop.plugins.account_hooks.extra_visible_ids", return_value=[]):
        assert (
            resolve_payload_ai_model(
                _database([retired]), account=account, payload={"model": "old/model"}
            )
            is retired
        )


def test_trial_spend_includes_retired_hosted_model_usage() -> None:
    from preloop.services.model_gateway_budget import ModelGatewayBudgetService

    retired = _model(retired=True)
    retired.meta_data["hosted"] = True
    retired.meta_data["gateway"] = {"enabled": False}
    db = _database([retired])
    service = ModelGatewayBudgetService(db, MagicMock())
    now = datetime.now(timezone.utc)
    with (
        patch("preloop.plugins.account_hooks.extra_visible_ids", return_value=[]),
        patch(
            "preloop.services.model_gateway_budget.crud_api_usage.get_gateway_usage_by_model",
            return_value=[{"estimated_cost": 0.25}],
        ) as usage,
    ):
        assert (
            service._get_trial_hosted_model_spend(
                account_id=str(uuid4()), start=now, end=now
            )
            == 0.25
        )
    assert usage.call_args.kwargs["ai_model_ids"] == [str(retired.id)]
    assert usage.call_args.kwargs["limit"] is None
