"""Endpoint tests for reading and changing the session embedding setting.

The contract worth pinning here is the default an account reads before it has
decided anything, the round trip of ``scope``, and the refusal of a value
that is neither scope: a 422 is the difference between "this build does not
know that word" and a row that silently embeds nothing or everything.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_account, crud_session_embedding_setting
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    EMBEDDING_SCOPE_FULL,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
)
from preloop.schemas.session_search import DEGRADED_SEMANTIC_NOT_ENABLED
from preloop.services import session_search_semantic

SETTING_URL = "/api/v1/runtime-sessions/settings/embedding"


@pytest.fixture(autouse=True)
def account_owner(db_session, test_user):
    """Make the caller the account's own owner.

    Changing the scope is gated on ``manage_budgets``, and the OSS fallback
    lets the account's primary user through. The seeded role matrix is not
    present in the test database, so this is how a test says "the person
    whose account this is".
    """
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


def test_an_account_that_never_decided_reads_the_shipped_default(
    client, db_session, test_user
):
    """Off, and summaries only: the same answer the worker acts on."""
    response = client.get(SETTING_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["scope"] == EMBEDDING_SCOPE_SUMMARIES_ONLY
    # The help text ships with the setting so a console does not have to
    # invent its own wording for the trade off.
    assert "summaries_only" in body["scope_help"]
    assert "full" in body["scope_help"]


def test_the_scope_round_trips_through_the_endpoint(client, db_session, test_user):
    """What the endpoint returns is what the worker will read next pass."""
    widened = client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL})

    assert widened.status_code == 200
    assert widened.json()["scope"] == EMBEDDING_SCOPE_FULL
    assert client.get(SETTING_URL).json()["scope"] == EMBEDDING_SCOPE_FULL
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert stored is not None
    assert stored.scope == EMBEDDING_SCOPE_FULL

    narrowed = client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_SUMMARIES_ONLY})

    assert narrowed.status_code == 200
    assert narrowed.json()["scope"] == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_an_unknown_scope_is_rejected_with_422(client):
    """A misspelled scope is a validation error, not a quiet default."""
    response = client.put(SETTING_URL, json={"scope": "everything"})

    assert response.status_code == 422


def test_a_scope_this_build_does_not_know_reads_as_the_default(
    client, db_session, test_user
):
    """A newer-build row must not 500 the console; the worker already degrades."""
    setting = crud_session_embedding_setting.get_or_create(
        db_session, account_id=test_user.account_id
    )
    setting.scope = "titles_only_and_more"
    db_session.flush()

    response = client.get(SETTING_URL)

    assert response.status_code == 200
    assert response.json()["scope"] == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_an_unrecognised_field_is_rejected_with_422(client):
    """A field the body does not know is refused, not silently dropped."""
    response = client.put(
        SETTING_URL,
        json={"scope": EMBEDDING_SCOPE_FULL, "provider_api_key": "sk-nope"},
    )

    assert response.status_code == 422


def test_an_empty_body_is_rejected_with_422(client):
    """A save that says nothing is a client bug, not a no-op success."""
    assert client.put(SETTING_URL, json={}).status_code == 422


def test_the_change_is_bound_to_the_calling_account(client, db_session, test_user):
    """Another account's setting is not touched by this one's decision."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    crud_session_embedding_setting.get_or_create(
        db_session, account_id=other_account.id, commit=True
    )

    assert (
        client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL}).status_code == 200
    )

    theirs = crud_session_embedding_setting.get_for_account(
        db_session, account_id=other_account.id
    )
    assert theirs.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_a_user_without_the_budget_permission_cannot_widen_the_scope(
    db_session, test_viewer_user
):
    """Widening the scope spends money, so a viewer is refused."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user

    with TestClient(app) as viewer_client:
        response = viewer_client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL})

    assert response.status_code == 403
    assert "manage_budgets" in response.json()["detail"]
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_viewer_user.account_id
    )
    assert stored is None or stored.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY


# --- Opting in from the console (#791) -------------------------------------
#
# The console has to be able to turn embedding on, name what the text is sent
# to, pick a scope and set a cap in one save. Enabling still goes through the
# CRUD validation that owns the host policy, so these pin that the endpoint
# refuses what ``enable()`` refuses, as a 422 with the refusal's own code.

LOCAL_OPT_IN = {
    "enabled": True,
    "provider": PROVIDER_LOCAL,
    "model_identifier": "test-embed",
}


def test_an_account_can_opt_in_with_scope_and_cap_in_one_save(
    client, db_session, test_user
):
    """Enable, scope and cap together: the whole console card in one PUT."""
    response = client.put(
        SETTING_URL,
        json={**LOCAL_OPT_IN, "scope": EMBEDDING_SCOPE_FULL, "daily_cap_usd": 0.5},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["provider"] == PROVIDER_LOCAL
    assert body["model_identifier"] == "test-embed"
    assert body["scope"] == EMBEDDING_SCOPE_FULL
    assert body["daily_cap_usd"] == 0.5
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert stored.enabled is True
    assert str(stored.enabled_by_user_id) == str(test_user.id)


def test_opting_in_ends_semantic_not_enabled_for_search(client, db_session, test_user):
    """The acceptance line: search stops saying this account never opted in."""
    session_search_semantic.reset_query_embedding_cache()
    before = session_search_semantic.embed_query(
        db_session,
        account_id=test_user.account_id,
        query="rolling restart",
        provider=_ZeroProvider(),
    )
    assert before.reason == DEGRADED_SEMANTIC_NOT_ENABLED

    assert client.put(SETTING_URL, json=LOCAL_OPT_IN).status_code == 200

    after = session_search_semantic.embed_query(
        db_session,
        account_id=test_user.account_id,
        query="rolling restart",
        provider=_ZeroProvider(),
    )
    assert after.reason != DEGRADED_SEMANTIC_NOT_ENABLED
    session_search_semantic.reset_query_embedding_cache()


class _ZeroProvider:
    """A provider that answers without a network, for the search check."""

    model = "test-embed"
    last_usage = {"prompt_tokens": 1}

    def embed(self, texts):
        from preloop.models.models.session_search_document import (
            EMBEDDING_DIMENSIONS,
        )

        return [[0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0] for _ in texts]


def test_enabling_without_a_model_is_a_422_with_the_refusal_code(client):
    """A fresh account has no model to fall back on, so it has to name one."""
    response = client.put(SETTING_URL, json={"enabled": True})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "model_required"


def test_enabling_against_a_private_ip_literal_is_refused(
    client, db_session, test_user
):
    """The host policy in enable() still applies when the console opts in."""
    response = client.put(
        SETTING_URL,
        json={
            "enabled": True,
            "provider": PROVIDER_OPENAI_COMPATIBLE,
            "model_identifier": "text-embedding-3-small",
            "base_url": "https://10.0.0.1/v1",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_base_url"
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert stored is None or stored.enabled is False


def test_an_unknown_provider_is_rejected_with_422(client):
    """Only the two providers the worker knows can be named."""
    response = client.put(
        SETTING_URL,
        json={"enabled": True, "provider": "carrier_pigeon", "model_identifier": "x"},
    )

    assert response.status_code == 422


def test_turning_it_off_keeps_the_provider_for_a_re_enable(
    client, db_session, test_user
):
    """Off then on again needs no provider details the row already holds."""
    assert client.put(SETTING_URL, json=LOCAL_OPT_IN).status_code == 200

    off = client.put(SETTING_URL, json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert off.json()["model_identifier"] == "test-embed"

    on = client.put(SETTING_URL, json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True
    assert on.json()["model_identifier"] == "test-embed"


def test_provider_details_on_a_disabled_setting_are_refused(client):
    """Naming a provider is the opt in; it cannot be stored while off."""
    response = client.put(
        SETTING_URL,
        json={"provider": PROVIDER_LOCAL, "model_identifier": "test-embed"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "provider_requires_enable"


def test_the_daily_cap_is_set_and_cleared_without_touching_the_rest(
    client, db_session, test_user
):
    """An explicit null falls back to the deployment default; absent keeps it."""
    assert client.put(SETTING_URL, json=LOCAL_OPT_IN).status_code == 200

    capped = client.put(SETTING_URL, json={"daily_cap_usd": 1.25})
    assert capped.status_code == 200
    assert capped.json()["daily_cap_usd"] == 1.25
    assert capped.json()["enabled"] is True

    scoped = client.put(SETTING_URL, json={"scope": EMBEDDING_SCOPE_FULL})
    assert scoped.json()["daily_cap_usd"] == 1.25

    cleared = client.put(SETTING_URL, json={"daily_cap_usd": None})
    assert cleared.status_code == 200
    assert cleared.json()["daily_cap_usd"] is None


def test_a_negative_daily_cap_is_rejected_with_422(client):
    """A negative ceiling means nothing; it is refused, not clamped."""
    assert client.put(SETTING_URL, json={"daily_cap_usd": -1}).status_code == 422


def test_a_save_while_enabled_does_not_wipe_the_degraded_marker(
    client, db_session, test_user
):
    """Changing the cap is not a fresh opt in: why it stopped stays visible."""
    assert client.put(SETTING_URL, json=LOCAL_OPT_IN).status_code == 200
    crud_session_embedding_setting.mark_degraded(
        db_session,
        account_id=test_user.account_id,
        reason=DEGRADED_DAILY_CAP,
        commit=True,
    )

    response = client.put(SETTING_URL, json={**LOCAL_OPT_IN, "daily_cap_usd": 3.0})

    assert response.status_code == 200
    assert response.json()["degraded_reason"] == DEGRADED_DAILY_CAP


def test_the_read_carries_the_deployment_cap_and_corpus_progress(client):
    """The console shows the fallback cap and how far the backlog has got."""
    body = client.get(SETTING_URL).json()

    assert body["deployment_daily_cap_usd"] >= 0
    assert body["deployment_embedding_enabled"] in (True, False)
    corpus = body["corpus"]
    assert corpus == {
        "vectors": 0,
        "model_vectors": 0,
        "pending": 0,
        "embedded_through": None,
    }


def test_a_viewer_cannot_opt_the_account_in(db_session, test_viewer_user):
    """Opting in spends money, so it takes manage_budgets like the scope."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user

    with TestClient(app) as viewer_client:
        response = viewer_client.put(SETTING_URL, json=LOCAL_OPT_IN)

    assert response.status_code == 403
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_viewer_user.account_id
    )
    assert stored is None or stored.enabled is False


def test_an_explicit_null_enabled_leaves_the_opt_in_alone(
    client, db_session, test_user
):
    """A client round tripping nulls must not switch embedding off."""
    assert client.put(SETTING_URL, json=LOCAL_OPT_IN).status_code == 200

    response = client.put(SETTING_URL, json={"enabled": None})

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    stored = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    assert stored.enabled is True


def test_null_provider_fields_on_a_disabled_setting_name_nothing(client):
    """An explicit null is not naming a provider, so it is not refused."""
    response = client.put(
        SETTING_URL,
        json={"base_url": None, "model_identifier": None, "provider": None},
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_an_empty_endpoint_is_refused_when_opting_in(client, db_session, test_user):
    """The console sends what was typed; an empty endpoint is base_url_required."""
    response = client.put(
        SETTING_URL,
        json={
            "enabled": True,
            "provider": PROVIDER_OPENAI_COMPATIBLE,
            "model_identifier": "text-embedding-3-small",
            "base_url": "",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "base_url_required"
