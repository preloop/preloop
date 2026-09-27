"""Embed a search query so the corpus can be searched by meaning.

The worker in :mod:`preloop.services.session_embedding` writes vectors. This
is the other side of the same money and the same consent: to search by
meaning, the query itself has to be embedded, which is one more provider call
per search, with the same account opt in, the same deployment kill switch and
the same daily cap governing it.

Three properties are the whole point of this module.

*Nothing happens without consent.* An account that has not opted in is not
embedded here either, and the search says so rather than quietly returning
keyword results as though they were the whole answer.

*A failure is never an error.* Every way this can stop (kill switch, opt out,
cap, provider) returns a reason code, not an exception. The search that called
it still answers, with keyword results and a marker naming what is missing.

*The same query is embedded once.* A caller paging through results sends the
same query with a new offset, and re-embedding it per page would multiply the
cost of a search by the number of pages a user scrolls. The cache is keyed by
a digest of account, model identity and query text, so the query itself is not
what sits in process memory.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time as clock
from dataclasses import dataclass
from datetime import UTC, datetime, time as day_time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_api_usage, crud_session_embedding_setting
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    DEGRADED_MISCONFIGURED,
    DEGRADED_PROVIDER_ERROR,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MISCONFIGURED,
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_PROVIDER_ERROR,
)
from preloop.services.model_pricing import estimate_external_model_usage_cost
from preloop.services.session_embedding import (
    daily_cap_for,
    CHARS_PER_TOKEN,
    SESSION_EMBEDDING_PURPOSE,
    EmbeddingProvider,
    EmbeddingProviderError,
    build_provider,
    embedding_enabled,
)

logger = logging.getLogger(__name__)

#: Endpoint recorded on the usage row a query embedding writes. It is not an
#: HTTP route on this service; it names where the spend came from in a ledger
#: whose other rows are gateway paths, and it is deliberately distinct from
#: the worker's endpoint so query spend and corpus spend can be told apart.
USAGE_ENDPOINT = "internal/session-search-query-embedding"

#: Worker reason code to search reason code. The worker records the first on
#: the account's setting row when it stops; a search publishes the second,
#: because the question a reader of a search response is asking is "what is
#: missing from this answer", not "what did the worker do last night".
_WORKER_REASON_CODES = {
    DEGRADED_DAILY_CAP: DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_PROVIDER_ERROR: DEGRADED_SEMANTIC_PROVIDER_ERROR,
    DEGRADED_MISCONFIGURED: DEGRADED_SEMANTIC_MISCONFIGURED,
}


@dataclass(frozen=True)
class QueryEmbedding:
    """One embedded query, and the model that embedded it."""

    vector: List[float]
    model_identity: str
    #: True when the vector came from the cache, so no provider was called
    #: and no spend was recorded for this search.
    cached: bool = False


@dataclass(frozen=True)
class QueryEmbeddingOutcome:
    """Either a query vector, or the reason there is not one.

    Never both, and never an exception: a search whose semantic half cannot
    run still has a keyword half to return.
    """

    embedding: Optional[QueryEmbedding] = None
    reason: Optional[str] = None


class _QueryEmbeddingCache:
    """A small, bounded, time limited cache of query vectors.

    Process local on purpose. A shared cache would put one account's query
    vectors in a store another process reads, and the thing being saved is
    one cheap provider call per page, not a database round trip.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[float, QueryEmbedding]] = {}

    @staticmethod
    def _now() -> float:
        return clock.monotonic()

    def _ttl(self) -> float:
        return max(
            0.0,
            float(
                getattr(settings, "session_search_query_embedding_ttl_seconds", 300.0)
            ),
        )

    def _capacity(self) -> int:
        return max(
            1,
            int(getattr(settings, "session_search_query_embedding_cache_size", 256)),
        )

    def get(self, key: str) -> Optional[QueryEmbedding]:
        """Return a live entry, dropping it if its window has passed."""
        if self._ttl() <= 0:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, embedding = entry
            if expires_at <= self._now():
                self._entries.pop(key, None)
                return None
            return embedding

    def put(self, key: str, embedding: QueryEmbedding) -> None:
        """Store an entry, evicting the entry closest to expiry when full."""
        ttl = self._ttl()
        if ttl <= 0:
            return
        with self._lock:
            now = self._now()
            expired = [
                cached for cached, (until, _) in self._entries.items() if until <= now
            ]
            for cached in expired:
                self._entries.pop(cached, None)
            while len(self._entries) >= self._capacity():
                oldest = min(self._entries, key=lambda item: self._entries[item][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (now + ttl, embedding)

    def clear(self) -> None:
        """Drop everything. Used by tests.

        An account turning embedding off is not a caller. ``embed_query``
        checks ``setting.enabled`` before the cache lookup, so an opted-out
        account cannot read a stale vector even if one is still in memory.
        """
        with self._lock:
            self._entries.clear()


_CACHE = _QueryEmbeddingCache()


def reset_query_embedding_cache() -> None:
    """Empty the process cache of query vectors."""
    _CACHE.clear()


def _cache_key(account_id: Any, model_identity: str, query: str) -> str:
    """Digest of account, model and query text.

    A digest rather than the text itself: the query is whatever an operator
    is hunting for in their own transcripts, and a dictionary key is a place
    it can be read from a heap dump or a debugger. The model identity is part
    of the key so a setting change cannot serve the previous model's vector.
    """
    material = f"{account_id}\x1f{model_identity}\x1f{query}".encode()
    return hashlib.sha256(material).hexdigest()


def _day_start(now: Optional[datetime] = None) -> datetime:
    """Start of the UTC day the cap is measured over."""
    stamp = now or datetime.now(UTC)
    return datetime.combine(stamp.date(), day_time.min, tzinfo=UTC)


def _estimated_tokens(query: str) -> int:
    """Token count for a query when the provider reported none."""
    return max(1, int(len(query or "") / CHARS_PER_TOKEN))


def _reported_tokens(provider: EmbeddingProvider, query: str) -> Tuple[int, str]:
    """Prefer the provider's own count, and say which one was used."""
    usage = getattr(provider, "last_usage", None)
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                return value, "provider"
    return _estimated_tokens(query), "estimated"


def embed_query(
    db: Session,
    *,
    account_id: Any,
    query: str,
    provider: Optional[EmbeddingProvider] = None,
    now: Optional[datetime] = None,
) -> QueryEmbeddingOutcome:
    """Embed one search query, or say why it was not embedded.

    The order of the checks is the order of the costs: the free ones (kill
    switch, opt in, cache) come before the ones that spend money (cap read,
    provider call).

    Args:
        db: Request scoped session.
        account_id: Account whose setting decides the provider and the cap.
        query: The normalised query text.
        provider: Injected provider; built from the account setting when
            omitted. Tests pass a fake so no suite ever calls out.
        now: Clock override for the daily window.

    Returns:
        An outcome carrying either the query vector or a reason code. It
        never raises: a search with no semantic half still has results.
    """
    if not embedding_enabled():
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_DISABLED)

    setting = crud_session_embedding_setting.get_for_account(db, account_id=account_id)
    if setting is None or not setting.enabled:
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_NOT_ENABLED)

    model_identity = setting.model_identity
    if not model_identity:
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_MISCONFIGURED)

    key = _cache_key(account_id, model_identity, query)
    cached = _CACHE.get(key)
    if cached is not None:
        # Paging is the case this exists for: the same query with a new
        # offset costs nothing and reaches no provider.
        return QueryEmbeddingOutcome(
            embedding=QueryEmbedding(
                vector=cached.vector, model_identity=cached.model_identity, cached=True
            )
        )

    cap = daily_cap_for(setting)
    spent = crud_api_usage.get_gateway_spend(
        db,
        account_id=str(account_id),
        start=_day_start(now),
        purpose=SESSION_EMBEDDING_PURPOSE,
    )
    if cap <= 0 or spent >= cap:
        # The same cap the worker respects, read the same way. A search does
        # not get to spend past a ceiling the corpus writer stopped at.
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_DAILY_CAP)

    try:
        active_provider = provider or build_provider(setting)
    except EmbeddingProviderError as exc:
        logger.warning(
            "Session search query embedding misconfigured for account %s: %s",
            account_id,
            exc,
        )
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_MISCONFIGURED)

    started = clock.perf_counter()
    try:
        vectors = active_provider.embed([query])
    except EmbeddingProviderError as exc:
        # The message names the endpoint and the model, never the query.
        logger.warning(
            "Session search query embedding failed for account %s: %s", account_id, exc
        )
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_PROVIDER_ERROR)
    except Exception:
        logger.exception(
            "Session search query embedding failed for account %s", account_id
        )
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_PROVIDER_ERROR)
    duration = clock.perf_counter() - started

    width = int(getattr(setting, "dimensions", 0) or 0)
    if len(vectors) != 1 or (width and len(vectors[0]) != width):
        logger.warning(
            "Session search query embedding for account %s returned %s vectors of "
            "unexpected width",
            account_id,
            len(vectors),
        )
        return QueryEmbeddingOutcome(reason=DEGRADED_SEMANTIC_PROVIDER_ERROR)

    embedding = QueryEmbedding(
        vector=[float(value) for value in vectors[0]], model_identity=model_identity
    )
    _CACHE.put(key, embedding)
    _record_usage(
        db,
        account_id=str(account_id),
        setting=setting,
        provider=active_provider,
        query=query,
        model_identity=model_identity,
        duration=duration,
    )
    return QueryEmbeddingOutcome(embedding=embedding)


def stored_degraded_reason(db: Session, *, account_id: Any) -> Optional[str]:
    """The worker's own degraded marker, translated for a search response.

    The worker stops on a cap or a provider failure and records why on the
    account's setting row. A search that happens after that is answering over
    a corpus that stopped filling, and it says so even when its own query
    embedding went through: the coverage is what is degraded, not the query.
    """
    setting = crud_session_embedding_setting.get_for_account(db, account_id=account_id)
    if setting is None or not setting.degraded_reason:
        return None
    return _WORKER_REASON_CODES.get(setting.degraded_reason)


def _record_usage(
    db: Session,
    *,
    account_id: str,
    setting: Any,
    provider: EmbeddingProvider,
    query: str,
    model_identity: str,
    duration: float,
) -> None:
    """Record what this query embedding cost, under the worker's purpose.

    Same purpose tag as the corpus worker, so query spend counts against the
    same daily cap rather than against a ceiling nobody set. The row carries
    no session id, because a search belongs to a person and not to a session,
    and it carries no query text for the same reason the search never logs it.
    """
    tokens, usage_source = _reported_tokens(provider, query)
    estimate = estimate_external_model_usage_cost(
        getattr(provider, "model", "") or (setting.model_identifier or ""),
        prompt_tokens=tokens,
        completion_tokens=0,
    )
    try:
        crud_api_usage.log_gateway_request(
            db,
            endpoint=USAGE_ENDPOINT,
            method="POST",
            status_code=200,
            duration=duration,
            account_id=account_id,
            model_alias=setting.model_identifier,
            provider_name=setting.provider,
            prompt_tokens=tokens,
            completion_tokens=0,
            total_tokens=tokens,
            estimated_cost=estimate.cost,
            cost_source=estimate.source,
            usage_source=usage_source,
            meta_data={
                "purpose": SESSION_EMBEDDING_PURPOSE,
                "kind": "query",
                "model_identity": model_identity,
            },
        )
    except Exception:  # noqa: BLE001 - the search has its vector; the row is not
        db.rollback()
        logger.warning(
            "Session search could not record query embedding usage for account %s",
            account_id,
            exc_info=True,
        )
