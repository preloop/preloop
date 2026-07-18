"""Principal-bound OAuth models must never be auto-selected as the default.

A Claude Code / Codex subscription OAuth credential only authorizes its owner's
own interactive traffic. It cannot serve Preloop's server-side generation
(session optimization, policy generation, summaries), so auto-selecting one
makes the user's very first "Optimize" click fail.

These tests pin the rule at the CRUD chokepoint every server-side generation
path resolves through:

  1. A BYOK/API-key model wins over an OAuth model, even when the OAuth model
     is the one flagged ``is_default``.
  2. With no flagged default, the FIRST BYOK model wins (creation order).
  3. When only OAuth-backed models exist, resolution returns ``None`` rather
     than handing back a model that is guaranteed to fail.
  4. The rule is keyed off credential type, not provider or model name.
"""

import uuid

import pytest

from preloop.models.crud import crud_ai_model
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
)


def _make_api_key_model(db_session, account, *, name: str, is_default: bool = False):
    """Create a BYOK/API-key-backed model for the account."""
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"{name} {uuid.uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "sk-byok-secret",
            "is_default": is_default,
        },
        account_id=account.id,
    )


def _make_oauth_model(
    db_session,
    account,
    *,
    name: str,
    credential_type: str = ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    is_default: bool = False,
):
    """Create a principal-bound OAuth-backed model for the account."""
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"{name} {uuid.uuid4()}",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "credential_type": credential_type,
            "credential_payload": {"access": "oauth-access-token"},
            "is_default": is_default,
        },
        account_id=account.id,
    )


def test_oauth_model_is_not_principal_agnostic(db_session, create_account):
    """The credential-type distinction the rule keys off is wired correctly."""
    account = create_account()
    oauth_model = _make_oauth_model(db_session, account, name="Claude Code")
    api_key_model = _make_api_key_model(db_session, account, name="BYOK")

    assert oauth_model.credential_type == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
    assert oauth_model.is_principal_bound_oauth is True
    assert oauth_model.supports_server_side_generation is False

    assert api_key_model.credential_type == "api_key"
    assert api_key_model.is_principal_bound_oauth is False
    assert api_key_model.supports_server_side_generation is True


def test_byok_model_wins_over_oauth_model_flagged_default(db_session, create_account):
    """An OAuth model flagged is_default must still lose to a BYOK model."""
    account = create_account()
    _make_oauth_model(db_session, account, name="Claude Code", is_default=True)
    api_key_model = _make_api_key_model(db_session, account, name="BYOK")

    resolved = crud_ai_model.get_default_active_model(
        db_session, account_id=str(account.id)
    )

    assert resolved is not None
    assert resolved.id == api_key_model.id


def test_first_byok_model_wins_when_nothing_is_flagged(db_session, create_account):
    """With no flagged default, the first BYOK model by creation order wins."""
    account = create_account()
    _make_oauth_model(db_session, account, name="Claude Code")
    first_byok = _make_api_key_model(db_session, account, name="First BYOK")
    _make_api_key_model(db_session, account, name="Second BYOK")

    resolved = crud_ai_model.get_default_active_model(
        db_session, account_id=str(account.id)
    )

    assert resolved is not None
    assert resolved.id == first_byok.id


def test_flagged_byok_default_still_wins_over_earlier_byok(db_session, create_account):
    """An explicit BYOK default is honored ahead of creation order."""
    account = create_account()
    _make_api_key_model(db_session, account, name="Earlier BYOK")
    flagged = _make_api_key_model(db_session, account, name="Chosen", is_default=True)

    resolved = crud_ai_model.get_default_active_model(
        db_session, account_id=str(account.id)
    )

    assert resolved is not None
    assert resolved.id == flagged.id


@pytest.mark.parametrize(
    "credential_type",
    [
        ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
        OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
    ],
)
def test_oauth_only_account_resolves_to_none(
    db_session, create_account, credential_type
):
    """Claude-Code-only / Codex-only users get no default, not a broken one.

    This is the Show HN persona: their only credential is a subscription
    login. Returning it here would make their first Optimize click fail
    server-side; returning None lets callers surface "add an API key".
    """
    account = create_account()
    _make_oauth_model(
        db_session,
        account,
        name="Subscription Model",
        credential_type=credential_type,
        is_default=True,
    )

    resolved = crud_ai_model.get_default_active_model(
        db_session, account_id=str(account.id)
    )

    assert resolved is None


def test_account_with_no_models_resolves_to_none(db_session, create_account):
    """An empty account still resolves to None (unchanged behaviour)."""
    account = create_account()

    resolved = crud_ai_model.get_default_active_model(
        db_session, account_id=str(account.id)
    )

    assert resolved is None
