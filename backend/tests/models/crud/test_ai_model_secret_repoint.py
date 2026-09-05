"""CRUD tests for AIModel credential-secret repointing.

PUT /ai-models/{id} must be able to attach an existing SecretReference (one
OAuth lineage shared across Claude family rows) and garbage-collect the
previous secret when nothing else still points at it.
"""

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model
from preloop.models.models import Account
from preloop.models.models.secret_reference import SecretReference


def test_update_repoints_credentials_secret_and_deletes_orphan(
    db_session: Session, create_account
):
    """Repointing to a shared secret succeeds and reaps the abandoned secret."""
    account: Account = create_account()

    primary = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Fable",
            "provider_name": "anthropic",
            "model_identifier": "claude-fable-5-1",
            "api_key": "fable-lineage-key",
        },
        account_id=account.id,
    )
    sibling = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Sonnet",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "sonnet-lineage-key",
        },
        account_id=account.id,
    )
    live_secret_id = primary.credentials_secret_id
    orphan_secret_id = sibling.credentials_secret_id
    assert live_secret_id is not None
    assert orphan_secret_id is not None
    assert live_secret_id != orphan_secret_id

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=sibling,
        obj_in={"credentials_secret_id": live_secret_id},
    )

    assert updated.credentials_secret_id == live_secret_id
    assert db_session.get(SecretReference, live_secret_id) is not None
    assert db_session.get(SecretReference, orphan_secret_id) is None


def test_update_repoint_keeps_secret_still_referenced_by_another_model(
    db_session: Session, create_account
):
    """Repointing one of two sharers must not delete the previous secret."""
    account: Account = create_account()

    first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model A",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "shared-old-key",
        },
        account_id=account.id,
    )
    old_secret_id = first.credentials_secret_id
    second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model B",
            "provider_name": "anthropic",
            "model_identifier": "claude-haiku-4-5",
            "credentials_secret_id": old_secret_id,
        },
        account_id=account.id,
    )
    live = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Model C",
            "provider_name": "anthropic",
            "model_identifier": "claude-fable-5-1",
            "api_key": "live-key",
        },
        account_id=account.id,
    )

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=second,
        obj_in={"credentials_secret_id": live.credentials_secret_id},
    )

    assert updated.credentials_secret_id == live.credentials_secret_id
    assert first.credentials_secret_id == old_secret_id
    assert db_session.get(SecretReference, old_secret_id) is not None


def test_update_repoint_rejects_secret_from_another_account(
    db_session: Session, create_account
):
    """A model must not attach another account's credential secret."""
    owner: Account = create_account()
    intruder: Account = create_account()

    owned = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Owner Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "owner-key",
        },
        account_id=owner.id,
    )
    stray = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Intruder Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-haiku-4-5",
            "api_key": "intruder-key",
        },
        account_id=intruder.id,
    )

    with pytest.raises(ValueError, match="different account"):
        crud_ai_model.update(
            db=db_session,
            db_obj=stray,
            obj_in={"credentials_secret_id": owned.credentials_secret_id},
        )

    db_session.refresh(stray)
    assert stray.credentials_secret_id != owned.credentials_secret_id


def test_update_with_null_credentials_secret_id_keeps_secret(
    db_session: Session, create_account
):
    """An explicit ``credentials_secret_id: null`` must not detach or GC the secret.

    Review finding 1: now that the field survives ``exclude_unset``, a raw
    ``PUT {"credentials_secret_id": null}`` would NULL the column and reap the
    orphaned SecretReference, destroying a live OAuth lineage. It must stay a
    no-op, as it was before the field existed on the update schema.
    """
    account: Account = create_account()

    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Sonnet",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "sonnet-lineage-key",
        },
        account_id=account.id,
    )
    secret_id = model.credentials_secret_id
    assert secret_id is not None

    updated = crud_ai_model.update(
        db=db_session,
        db_obj=model,
        obj_in={"credentials_secret_id": None, "name": "Claude Sonnet (renamed)"},
    )

    assert updated.name == "Claude Sonnet (renamed)"
    assert updated.credentials_secret_id == secret_id
    assert db_session.get(SecretReference, secret_id) is not None
