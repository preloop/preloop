"""Tests for the per account embedding opt in.

Enabling is the moment an account decides its session text may be sent to a
named endpoint, so the validation here is the guard on that decision: no
unknown provider, no unnamed model, no OpenAI compatible endpoint without a
base url, and no width the corpus column cannot store.
"""

import pytest

from preloop.models.crud import crud_session_embedding_setting
from preloop.models.crud.session_embedding_setting import SessionEmbeddingConfigError
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    EMBEDDING_SCOPE_FULL,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    SOURCE_KIND_SESSION_SUMMARY,
)


def test_an_account_that_never_asked_starts_disabled(db_session, test_user):
    """Off by default, and the row created on demand says so."""
    account_id = str(test_user.account_id)

    assert (
        crud_session_embedding_setting.get_for_account(
            db_session, account_id=account_id
        )
        is None
    )
    setting = crud_session_embedding_setting.get_or_create(
        db_session, account_id=account_id
    )
    assert setting.enabled is False
    assert setting.dimensions == EMBEDDING_DIMENSIONS


def test_enabling_names_the_provider_and_the_model(db_session, test_user):
    """The identity stored on every vector is built from the opt in."""
    account_id = str(test_user.account_id)

    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="  text-embedding-3-small  ",
        base_url="https://embeddings.example.com/v1",
        daily_cap_usd=1.5,
    )

    assert setting.enabled is True
    assert setting.model_identifier == "text-embedding-3-small"
    assert setting.enabled_at is not None
    assert setting.model_identity == (
        f"openai_compatible:text-embedding-3-small@{EMBEDDING_DIMENSIONS}"
    )
    assert crud_session_embedding_setting.enabled_account_ids(db_session) == [
        account_id
    ]


def test_a_local_provider_keeps_no_base_url(db_session, test_user):
    """A local model runs in process; a base url would misstate where text goes."""
    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=str(test_user.account_id),
        provider=PROVIDER_LOCAL,
        model_identifier="all-MiniLM-L6-v2",
        base_url="https://ignored.example.com/v1",
    )

    assert setting.base_url is None


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"provider": "carrier-pigeon"}, "unknown_provider"),
        ({"model_identifier": "  "}, "model_required"),
        ({"base_url": None}, "base_url_required"),
        ({"base_url": "http://embeddings.example.com/v1"}, "invalid_base_url"),
        ({"base_url": "https://127.0.0.1/v1"}, "invalid_base_url"),
        ({"base_url": "https://169.254.169.254/latest"}, "invalid_base_url"),
        ({"base_url": "https://10.0.0.8/v1"}, "invalid_base_url"),
        ({"base_url": "https://localhost/v1"}, "invalid_base_url"),
        ({"base_url": "https://169.254.169.254.nip.io/v1"}, "invalid_base_url"),
        ({"dimensions": 512}, "unsupported_dimensions"),
        ({"daily_cap_usd": -1.0}, "invalid_daily_cap"),
    ],
)
def test_an_unusable_configuration_is_refused_at_the_opt_in(
    db_session, test_user, kwargs, code
):
    """A bad configuration fails when it is chosen, not on the worker thread."""
    base = {
        "provider": PROVIDER_OPENAI_COMPATIBLE,
        "model_identifier": "text-embedding-3-small",
        "base_url": "https://embeddings.example.com/v1",
    }
    base.update(kwargs)

    with pytest.raises(SessionEmbeddingConfigError) as excinfo:
        crud_session_embedding_setting.enable(
            db_session, account_id=str(test_user.account_id), **base
        )

    assert excinfo.value.code == code
    # Validation runs before anything is written, so a refused opt in leaves
    # no half configured row behind for a worker to read.
    assert (
        crud_session_embedding_setting.get_for_account(
            db_session, account_id=str(test_user.account_id)
        )
        is None
    )


def test_disabling_keeps_the_provider_details_for_a_re_enable(db_session, test_user):
    """Turning it off is not forgetting the decision that turned it on."""
    account_id = str(test_user.account_id)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    setting = crud_session_embedding_setting.disable(db_session, account_id=account_id)

    assert setting is not None
    assert setting.enabled is False
    assert setting.base_url == "https://embeddings.example.com/v1"
    assert crud_session_embedding_setting.enabled_account_ids(db_session) == []


def test_a_degraded_marker_survives_until_a_run_clears_it(db_session, test_user):
    """Degraded is a state an operator can read, not a log line that scrolls."""
    account_id = str(test_user.account_id)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    degraded = crud_session_embedding_setting.mark_degraded(
        db_session, account_id=account_id, reason=DEGRADED_DAILY_CAP
    )
    assert degraded is not None
    assert degraded.degraded_reason == DEGRADED_DAILY_CAP
    assert degraded.degraded_at is not None
    assert degraded.enabled is True

    cleared = crud_session_embedding_setting.clear_degraded(
        db_session, account_id=account_id
    )
    assert cleared is not None
    assert cleared.degraded_reason is None
    assert cleared.degraded_at is None


def test_a_self_hosted_hostname_on_a_private_network_is_kept(db_session, test_user):
    """openai_compatible is for operator endpoints named by hostname."""
    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=str(test_user.account_id),
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.vpc.internal/v1",
    )

    assert setting.base_url == "https://embeddings.vpc.internal/v1"


def test_a_new_row_is_summaries_only(db_session, test_user):
    """The default answer to "how much" is the cheap one."""
    account_id = str(test_user.account_id)

    created = crud_session_embedding_setting.get_or_create(
        db_session, account_id=account_id
    )
    assert created.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY
    assert created.embedded_source_kinds == (SOURCE_KIND_SESSION_SUMMARY,)


def test_opting_in_without_saying_a_scope_keeps_the_default(db_session, test_user):
    """Naming a provider is not an invitation to embed everything."""
    account_id = str(test_user.account_id)

    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    assert setting.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY


def test_a_re_enable_keeps_the_scope_the_account_chose(db_session, test_user):
    """Turning it off and on again does not silently widen what is sent."""
    account_id = str(test_user.account_id)
    crud_session_embedding_setting.set_scope(
        db_session, account_id=account_id, scope=EMBEDDING_SCOPE_FULL
    )

    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    assert setting.scope == EMBEDDING_SCOPE_FULL


def test_the_scope_round_trips_in_both_directions(db_session, test_user):
    """Widening and narrowing are both one call, and neither is a migration."""
    account_id = str(test_user.account_id)

    widened = crud_session_embedding_setting.set_scope(
        db_session, account_id=account_id, scope=EMBEDDING_SCOPE_FULL, commit=True
    )
    assert widened.scope == EMBEDDING_SCOPE_FULL
    assert widened.embedded_source_kinds is None

    narrowed = crud_session_embedding_setting.set_scope(
        db_session,
        account_id=account_id,
        scope=EMBEDDING_SCOPE_SUMMARIES_ONLY,
        commit=True,
    )
    assert narrowed.scope == EMBEDDING_SCOPE_SUMMARIES_ONLY
    assert (
        crud_session_embedding_setting.get_for_account(
            db_session, account_id=account_id
        ).scope
        == EMBEDDING_SCOPE_SUMMARIES_ONLY
    )


@pytest.mark.parametrize("value", ["everything", "", "SUMMARIES_ONLY", "transcripts"])
def test_an_unknown_scope_is_refused(db_session, test_user, value):
    """Guessing here would either overspend or embed nothing at all."""
    account_id = str(test_user.account_id)

    with pytest.raises(SessionEmbeddingConfigError) as error:
        crud_session_embedding_setting.set_scope(
            db_session, account_id=account_id, scope=value
        )
    assert error.value.code == "invalid_scope"

    with pytest.raises(SessionEmbeddingConfigError) as enable_error:
        crud_session_embedding_setting.enable(
            db_session,
            account_id=account_id,
            provider=PROVIDER_OPENAI_COMPATIBLE,
            model_identifier="text-embedding-3-small",
            base_url="https://embeddings.example.com/v1",
            scope=value,
        )
    assert enable_error.value.code == "invalid_scope"


def test_a_scope_this_build_does_not_know_embeds_less_not_more(db_session, test_user):
    """A row from a newer build is read as the default, never as full."""
    account_id = str(test_user.account_id)
    setting = crud_session_embedding_setting.get_or_create(
        db_session, account_id=account_id
    )
    setting.scope = "titles_only_and_more"
    db_session.flush()

    assert setting.embedded_source_kinds == (SOURCE_KIND_SESSION_SUMMARY,)
