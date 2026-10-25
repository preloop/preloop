import pytest
import yaml
from sqlalchemy.orm import Session

from preloop.models import models

from preloop.models.crud import crud_account, crud_ai_model
from scripts.init_db import (
    _merge_gateway_meta,
    reconcile_ai_model_gateway_settings,
    seed_plan_catalog,
)


def test_merge_gateway_meta_preserves_existing_metadata():
    meta_data = {
        "label": "review",
        "gateway": {
            "model_alias": "custom/openai-gpt5",
        },
    }

    merged = _merge_gateway_meta(
        meta_data,
        provider_name="openai",
        model_identifier="gpt-5.4",
    )

    assert merged["label"] == "review"
    assert merged["gateway"]["enabled"] is True
    assert "url" not in merged["gateway"]
    assert merged["gateway"]["provider_adapter"] == "preloop"
    assert merged["gateway"]["model_alias"] == "custom/openai-gpt5"


def test_reconcile_ai_model_gateway_settings_updates_credentialed_models(
    db_session: Session,
):
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Gateway Bootstrap Org",
            "is_active": True,
        },
    )

    system_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "System Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "system-secret",
            "is_default": True,
        },
        account_id=None,
    )
    account_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Account Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "account-secret",
            "meta_data": {"owner": "review"},
        },
        account_id=account.id,
    )

    updated_count = reconcile_ai_model_gateway_settings(
        db_session,
    )

    db_session.refresh(system_model)
    db_session.refresh(account_model)

    assert updated_count >= 2
    assert system_model.meta_data["gateway"]["enabled"] is True
    assert "url" not in system_model.meta_data["gateway"]
    assert system_model.meta_data["gateway"]["model_alias"] == "openai/gpt-5.4"
    assert account_model.meta_data["owner"] == "review"
    assert account_model.meta_data["gateway"]["enabled"] is True
    assert "url" not in account_model.meta_data["gateway"]


@pytest.mark.parametrize("retention_days", [730, -1])
def test_upgrade_plan_seed_preserves_existing_legacy_terms(
    db_session, tmp_path, retention_days
):
    original = {
        "name": "Negotiated legacy plan",
        "price_monthly": 47.0,
        "price_annually": 470.0,
        "features": {
            "retention_days": retention_days,
            "audit_logs_retention_days": retention_days,
            "custom_entitlement": {"limit": 42},
        },
        "stripe_product_id": "negotiated_legacy_product",
    }
    db_session.add(models.Plan(id="legacy-seed", is_active=True, **original))
    db_session.flush()
    (tmp_path / "plans.yaml").write_text(
        yaml.safe_dump(
            {
                "plans": [
                    {
                        "id": "legacy-seed",
                        "name": "Generic legacy baseline",
                        "legacy": True,
                        "price_monthly": 29,
                        "price_annually": 290,
                        "features": {"retention_days": 365},
                    }
                ]
            }
        )
    )
    for _ in range(2):
        seed_plan_catalog(db_session, str(tmp_path))
        db_session.expire_all()
        row = db_session.get(models.Plan, "legacy-seed")
        assert {key: getattr(row, key) for key in original} == original
        assert row.is_active is False


def test_upgrade_plan_seed_creates_missing_legacy_as_inactive(db_session, tmp_path):
    (tmp_path / "plans.yaml").write_text(
        yaml.safe_dump(
            {
                "plans": [
                    {
                        "id": "missing-legacy-seed",
                        "name": "Legacy baseline",
                        "legacy": True,
                        "is_active": True,
                        "price_monthly": 29,
                        "price_annually": 290,
                        "features": {"retention_days": 365},
                    }
                ]
            }
        )
    )
    seed_plan_catalog(db_session, str(tmp_path))
    row = db_session.get(models.Plan, "missing-legacy-seed")
    assert row.name == "Legacy baseline"
    assert row.price_monthly == 29
    assert row.features == {"retention_days": 365}
    assert row.is_active is False


def test_upgrade_plan_seed_updates_current_catalog_and_active_flag(
    db_session, tmp_path
):
    plan = {
        "id": "current-seed",
        "name": "Current plan",
        "price_monthly": 10,
        "features": {"retention_days": 365},
        "is_active": False,
    }
    catalog_path = tmp_path / "plans.yaml"
    catalog_path.write_text(yaml.safe_dump({"plans": [plan]}))
    seed_plan_catalog(db_session, str(tmp_path))
    row = db_session.get(models.Plan, "current-seed")
    assert row.is_active is False
    plan.update(
        name="Updated plan",
        price_monthly=12,
        is_active=True,
        features={"retention_days": 730},
    )
    catalog_path.write_text(yaml.safe_dump({"plans": [plan]}))
    seed_plan_catalog(db_session, str(tmp_path))
    db_session.refresh(row)
    assert row.name == "Updated plan"
    assert row.price_monthly == 12
    assert row.features == {"retention_days": 730}
    assert row.is_active is True


@pytest.mark.parametrize(
    "gateway",
    [
        {"enabled": True, "model_alias": "local/check", "provider_adapter": "custom"},
        {
            "enabled": True,
            "model_alias": "local/check",
            "url": "http://gateway:8000/openai/v1",
        },
        {"enabled": False, "model_alias": "local/check"},
    ],
)
@pytest.mark.parametrize("retired", [False, True])
def test_repeated_upgrade_preserves_configured_gateway(db_session, gateway, retired):
    original = {"gateway": gateway, "local_inference": True, "retired": retired}
    row = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Configured local model",
            "provider_name": "openai",
            "model_identifier": "local-check",
            "api_key": "synthetic-test-key",
            "meta_data": original,
        },
        account_id=None,
    )
    # A configured adapter may be omitted intentionally; bootstrap fills only
    # defaults for enabled rows, never route or alias replacements.
    expected = dict(original)
    if not retired and gateway["enabled"]:
        expected["gateway"] = {"provider_adapter": "preloop", **gateway}
    for _ in range(2):
        reconcile_ai_model_gateway_settings(db_session)
        db_session.expire_all()
        refreshed = crud_ai_model.get(db_session, id=row.id)
        assert refreshed.meta_data == expected


def test_new_gateway_metadata_uses_runtime_environment_route():
    merged = _merge_gateway_meta(None, provider_name="openai", model_identifier="local")
    assert merged == {
        "gateway": {
            "enabled": True,
            "provider_adapter": "preloop",
            "model_alias": "openai/local",
        }
    }


@pytest.mark.parametrize("api_key", [None, ""])
def test_reconcile_does_not_enable_models_without_credentials(db_session, api_key):
    row = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "No credential",
            "provider_name": "openai",
            "model_identifier": "no-credential",
            "api_key": api_key,
            "meta_data": {"label": "unchanged"},
        },
        account_id=None,
    )
    reconcile_ai_model_gateway_settings(db_session)
    db_session.expire_all()
    assert crud_ai_model.get(db_session, id=row.id).meta_data == {"label": "unchanged"}


def test_bootstrap_preserves_legacy_duplicate_aliases(db_session):
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Legacy duplicate models", "is_active": True},
    )
    rows = [
        crud_ai_model.create_with_account(
            db=db_session,
            obj_in={
                "name": f"Legacy model {index}",
                "provider_name": "openai",
                "model_identifier": "same-legacy-model",
                "api_key": "test-key",
            },
            account_id=account.id,
        )
        for index in range(2)
    ]
    for _ in range(2):
        reconcile_ai_model_gateway_settings(db_session)
        db_session.expire_all()
        for row in rows:
            current = crud_ai_model.get(db_session, id=row.id)
            assert current.meta_data["gateway"]["enabled"] is True
            assert (
                current.meta_data["gateway"]["model_alias"]
                == "openai/same-legacy-model"
            )
