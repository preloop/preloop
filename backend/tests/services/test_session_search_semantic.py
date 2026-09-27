"""Embedding a search query: consent, cap, failure and the cache.

No test here reaches a provider. Every one of them asserts that a query
embedding either happened for a named reason or did not happen for a named
reason, because a search that cannot say which is a search that cannot be
honest about its own coverage.
"""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models.crud import crud_api_usage, crud_session_embedding_setting
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    DEGRADED_PROVIDER_ERROR,
    PROVIDER_LOCAL,
)
from preloop.models.models.session_search_document import EMBEDDING_DIMENSIONS
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_PROVIDER_ERROR,
)
from preloop.services import session_search_semantic
from preloop.services.session_embedding import (
    SESSION_EMBEDDING_PURPOSE,
    EmbeddingProviderError,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    """Counts its calls, so "did not re-embed" is an assertion, not a hope."""

    def __init__(self, *, fails: bool = False, width: int = EMBEDDING_DIMENSIONS):
        self.model = "test-embed"
        self.calls = 0
        self.fails = fails
        self.width = width
        self.last_usage = {"prompt_tokens": 7}

    def embed(self, texts):
        self.calls += 1
        if self.fails:
            raise EmbeddingProviderError("the provider refused this batch")
        return [[0.0] * (self.width - 1) + [1.0] for _ in texts]


@pytest.fixture(autouse=True)
def clear_query_cache():
    """A process cache shared between tests is a test that lies."""
    session_search_semantic.reset_query_embedding_cache()
    yield
    session_search_semantic.reset_query_embedding_cache()


def _opt_in(db_session, account_id, **kwargs):
    return crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_LOCAL,
        model_identifier="test-embed",
        **kwargs,
    )


def _embed(db_session, account_id, query, provider, **kwargs):
    return session_search_semantic.embed_query(
        db_session,
        account_id=account_id,
        query=query,
        provider=provider,
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_an_account_that_never_opted_in_is_not_embedded(db_session, test_user):
    """No opt in, no provider call, and a reason rather than an error."""
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.embedding is None
    assert outcome.reason == DEGRADED_SEMANTIC_NOT_ENABLED
    assert provider.calls == 0


def test_an_account_that_opted_out_again_is_not_embedded(db_session, test_user):
    """Turning it off turns off the query half too, not only the worker."""
    _opt_in(db_session, test_user.account_id)
    crud_session_embedding_setting.disable(db_session, account_id=test_user.account_id)
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.reason == DEGRADED_SEMANTIC_NOT_ENABLED
    assert provider.calls == 0


def test_the_deployment_kill_switch_stops_query_embedding(
    db_session, test_user, monkeypatch
):
    """Both switches have to say yes here, as they do for the worker."""
    _opt_in(db_session, test_user.account_id)
    monkeypatch.setattr(
        session_search_semantic, "embedding_enabled", lambda: False, raising=True
    )
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.reason == DEGRADED_SEMANTIC_DISABLED
    assert provider.calls == 0


def test_a_reached_daily_cap_stops_the_query_before_it_spends(db_session, test_user):
    """A search does not get to spend past the ceiling the worker stopped at."""
    _opt_in(db_session, test_user.account_id, daily_cap_usd=0.50)
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="internal/session-embedding",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        estimated_cost=0.75,
        meta_data={"purpose": SESSION_EMBEDDING_PURPOSE},
    )
    usage.timestamp = NOW
    db_session.flush()
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.embedding is None
    assert outcome.reason == DEGRADED_SEMANTIC_DAILY_CAP
    assert provider.calls == 0


def test_a_cap_of_zero_embeds_nothing(db_session, test_user):
    """ "Priced, but spend nothing" is a real setting and it is respected."""
    _opt_in(db_session, test_user.account_id, daily_cap_usd=0.0)
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.reason == DEGRADED_SEMANTIC_DAILY_CAP
    assert provider.calls == 0


def test_yesterdays_spend_does_not_stop_todays_query(db_session, test_user):
    """The cap is a daily window, so it has to be read as one."""
    _opt_in(db_session, test_user.account_id, daily_cap_usd=0.50)
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="internal/session-embedding",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        estimated_cost=0.75,
        meta_data={"purpose": SESSION_EMBEDDING_PURPOSE},
    )
    usage.timestamp = NOW - timedelta(days=1)
    db_session.flush()
    provider = FakeProvider()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.embedding is not None
    assert provider.calls == 1


def test_a_provider_failure_is_a_reason_and_not_an_exception(db_session, test_user):
    """The search still has a keyword half; it must not be handed an error."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider(fails=True)

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.embedding is None
    assert outcome.reason == DEGRADED_SEMANTIC_PROVIDER_ERROR
    assert provider.calls == 1


def test_a_vector_of_the_wrong_width_is_refused(db_session, test_user):
    """A width the column cannot store is a provider failure, not a result."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider(width=8)

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert outcome.embedding is None
    assert outcome.reason == DEGRADED_SEMANTIC_PROVIDER_ERROR


def test_the_same_query_is_embedded_once(db_session, test_user):
    """This is what makes paging free: the second page reaches no provider."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    first = _embed(db_session, test_user.account_id, "rolling restart", provider)
    second = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert provider.calls == 1
    assert first.embedding is not None and second.embedding is not None
    assert second.embedding.cached is True
    assert second.embedding.vector == first.embedding.vector


def test_a_different_query_is_embedded_again(db_session, test_user):
    """The cache is keyed by the query, not by the account alone."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    _embed(db_session, test_user.account_id, "rolling restart", provider)
    _embed(db_session, test_user.account_id, "ledger migration", provider)

    assert provider.calls == 2


def test_another_account_never_reads_a_cached_vector(db_session, test_user):
    """Two accounts asking the same question are two provider calls."""
    from preloop.models.crud import crud_account

    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    _opt_in(db_session, test_user.account_id)
    _opt_in(db_session, other_account.id)
    provider = FakeProvider()

    _embed(db_session, test_user.account_id, "rolling restart", provider)
    _embed(db_session, other_account.id, "rolling restart", provider)

    assert provider.calls == 2


def test_changing_the_model_invalidates_the_cached_vector(db_session, test_user):
    """A cached vector of the previous model would be scored against nothing."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()
    _embed(db_session, test_user.account_id, "rolling restart", provider)

    _opt_in(db_session, test_user.account_id)
    setting = crud_session_embedding_setting.get_for_account(
        db_session, account_id=test_user.account_id
    )
    setting.model_identifier = "test-embed-v2"
    db_session.flush()

    outcome = _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert provider.calls == 2
    assert outcome.embedding is not None
    assert outcome.embedding.model_identity.endswith("test-embed-v2@1536")


def test_a_zero_ttl_turns_the_cache_off(db_session, test_user, monkeypatch):
    """An operator who does not want query vectors in memory can say so."""
    from preloop.config import settings

    monkeypatch.setattr(
        settings, "session_search_query_embedding_ttl_seconds", 0.0, raising=False
    )
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    _embed(db_session, test_user.account_id, "rolling restart", provider)
    _embed(db_session, test_user.account_id, "rolling restart", provider)

    assert provider.calls == 2


def test_the_cache_never_holds_more_than_its_capacity(
    db_session, test_user, monkeypatch
):
    """A per process cache that grows without a bound is a leak."""
    from preloop.config import settings

    monkeypatch.setattr(
        settings, "session_search_query_embedding_cache_size", 2, raising=False
    )
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    for query in ("first query", "second query", "third query"):
        _embed(db_session, test_user.account_id, query, provider)
    _embed(db_session, test_user.account_id, "third query", provider)

    assert provider.calls == 3


def test_a_query_embedding_records_what_it_spent(db_session, test_user):
    """Spending money without a ledger row is how a cap stops being a cap."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    _embed(db_session, test_user.account_id, "rolling restart", provider)
    db_session.flush()

    rows = [
        row
        for row in crud_api_usage.get_multi(db_session, limit=100)
        if (row.meta_data or {}).get("purpose") == SESSION_EMBEDDING_PURPOSE
    ]
    assert len(rows) == 1
    assert rows[0].endpoint == session_search_semantic.USAGE_ENDPOINT
    assert (rows[0].meta_data or {}).get("kind") == "query"
    assert rows[0].prompt_tokens == 7
    assert "rolling restart" not in str(rows[0].meta_data)


def test_a_cached_query_spends_nothing(db_session, test_user):
    """A page that reaches no provider must not write a ledger row either."""
    _opt_in(db_session, test_user.account_id)
    provider = FakeProvider()

    _embed(db_session, test_user.account_id, "rolling restart", provider)
    _embed(db_session, test_user.account_id, "rolling restart", provider)
    db_session.flush()

    rows = [
        row
        for row in crud_api_usage.get_multi(db_session, limit=100)
        if (row.meta_data or {}).get("purpose") == SESSION_EMBEDDING_PURPOSE
    ]
    assert len(rows) == 1


def test_the_workers_degraded_marker_is_translated_for_a_search(db_session, test_user):
    """What stopped the corpus filling is what a search has to report."""
    _opt_in(db_session, test_user.account_id)
    crud_session_embedding_setting.mark_degraded(
        db_session, account_id=test_user.account_id, reason=DEGRADED_DAILY_CAP
    )

    assert (
        session_search_semantic.stored_degraded_reason(
            db_session, account_id=test_user.account_id
        )
        == DEGRADED_SEMANTIC_DAILY_CAP
    )

    crud_session_embedding_setting.mark_degraded(
        db_session, account_id=test_user.account_id, reason=DEGRADED_PROVIDER_ERROR
    )

    assert (
        session_search_semantic.stored_degraded_reason(
            db_session, account_id=test_user.account_id
        )
        == DEGRADED_SEMANTIC_PROVIDER_ERROR
    )


def test_the_deployment_cap_has_one_reading(monkeypatch):
    """Worker, query path and settings card all use the same fallback."""
    from preloop.services import session_embedding

    monkeypatch.setattr(
        session_embedding.settings, "session_embedding_daily_cap_usd", 3.5
    )

    assert session_embedding.deployment_daily_cap_usd() == 3.5

    class _NoCap:
        daily_cap_usd = None

    class _OwnCap:
        daily_cap_usd = 0.25

    assert session_embedding.daily_cap_for(_NoCap()) == 3.5
    assert session_embedding.daily_cap_for(_OwnCap()) == 0.25
