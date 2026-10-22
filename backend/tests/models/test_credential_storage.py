"""Regression tests for hash-only issuance and secret-reference writes."""

from sqlalchemy import inspect

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_api_key, crud_tracker
from preloop.services.secret_service import get_secret_service


def test_legacy_columns_are_absent_from_database_and_orm(db_session) -> None:
    for model, column in (
        (models.AIModel, "api_key"),
        (models.ApiKey, "key"),
        (models.Tracker, "jira_webhook_secret"),
    ):
        assert column not in model.__table__.columns
        assert column not in {
            col["name"]
            for col in inspect(db_session.bind).get_columns(model.__tablename__)
        }


def test_issue_reload_rotate_and_validate_user_key(
    db_session, create_account, create_user
) -> None:
    user = create_user(account=create_account())
    row, token = crud_api_key.issue(
        db_session,
        obj_in={
            "name": "User credential",
            "account_id": user.account_id,
            "user_id": user.id,
            "scopes": ["read:issues"],
        },
    )
    row_id = row.id
    assert row.key is None
    assert token not in str(row.__dict__)
    db_session.expunge(row)
    loaded = crud_api_key.validate_key(
        db_session, key=token, required_scopes=["read:issues"]
    )
    assert loaded.id == row_id
    assert loaded.key is None
    assert crud_api_key.get_by_key(db_session, key=token + "wrong") is None
    crud_api_key.update(
        db_session, db_obj=loaded, obj_in={"key": "replacement-synthetic-token"}
    )
    assert crud_api_key.get_by_key(db_session, key=token) is None
    assert (
        crud_api_key.get_by_key(db_session, key="replacement-synthetic-token").id
        == row_id
    )
    crud_api_key.deactivate(db_session, key_id=row_id)
    assert (
        crud_api_key.validate_key(db_session, key="replacement-synthetic-token") is None
    )


def test_ai_model_and_webhook_credentials_survive_reload_and_update(
    db_session, create_account
) -> None:
    account = create_account()
    model = crud_ai_model.create_with_account(
        db_session,
        account_id=account.id,
        obj_in={
            "name": "Credential test model",
            "provider_name": "openai",
            "model_identifier": "example-model",
            "api_key": "synthetic-model-secret",
        },
    )
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "Credential test tracker",
            "tracker_type": "jira",
            "jira_webhook_secret": "synthetic-webhook-secret",
            "connection_details": {},
        },
    )
    model_id, tracker_id = model.id, tracker.id
    db_session.expunge_all()
    model = crud_ai_model.get(db_session, id=model_id)
    tracker = crud_tracker.get(db_session, id=tracker_id)
    assert (
        get_secret_service().resolve_ai_model_api_key(model).value
        == "synthetic-model-secret"
    )
    assert tracker.resolved_webhook_secret == "synthetic-webhook-secret"
    crud_ai_model.update(
        db_session, db_obj=model, obj_in={"api_key": "replacement-model-secret"}
    )
    crud_tracker.update(
        db_session,
        db_obj=tracker,
        obj_in={"jira_webhook_secret": "replacement-webhook-secret"},
    )
    db_session.expunge_all()
    model = crud_ai_model.get(db_session, id=model_id)
    tracker = crud_tracker.get(db_session, id=tracker_id)
    assert (
        get_secret_service().resolve_ai_model_api_key(model).value
        == "replacement-model-secret"
    )
    assert tracker.resolved_webhook_secret == "replacement-webhook-secret"
    assert model.api_key is None and tracker.jira_webhook_secret is None


def test_hashed_rotation_accepts_null_legacy_key(
    db_session, create_account, create_user
) -> None:
    """Already-hashed callers can clear the obsolete field during rotation."""
    user = create_user(account=create_account())
    row, previous = crud_api_key.issue(
        db_session,
        obj_in={
            "name": "Runtime credential",
            "account_id": user.account_id,
            "user_id": user.id,
        },
    )
    replacement = "replacement-synthetic-runtime-token"
    crud_api_key.update(
        db_session,
        db_obj=row,
        obj_in={
            "key": None,
            "key_hash": crud_api_key.build_key_hash(replacement),
            "key_prefix": crud_api_key.build_key_prefix(replacement),
        },
    )
    assert crud_api_key.get_by_key(db_session, key=previous) is None
    assert crud_api_key.get_by_key(db_session, key=replacement).id == row.id
    assert row.key is None
    crud_api_key.update(db_session, db_obj=row, obj_in={"key": None})
    assert crud_api_key.get_by_key(db_session, key=replacement).id == row.id
