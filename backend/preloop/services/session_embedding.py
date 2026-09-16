"""Embed session search chunks in bounded, capped, purpose tagged batches.

Keyword search cannot answer "has an agent already solved this migration",
because the asker does not know the words the past session used. Vectors can,
and vectors cost money, so everything here is about the two constraints that
buys: the account decides whether its text is sent anywhere at all, and the
spend is named, capped and kept out of the sessions it indexes.

Shape of one run (:func:`run_account_batch`):

1. Both switches must say yes: the deployment kill switch and the account's
   own opt in. Either one off means zero vectors, not a smaller number.
2. Today's spend under this purpose is compared with the account's daily cap
   *before* anything is claimed. At the cap the run stops with a degraded
   marker and the chunks stay pending; the next day's run takes them.
3. Chunks are claimed oldest first, inside one session, and only if they are
   ``clear``, not under a legal hold, and of a source kind the account's
   embedding scope admits. ``summaries_only``, the default, admits the
   session's own title and summary chunk and nothing else; ``full`` admits
   every kind. Narrowing the scope deletes no vector: it only stops new ones
   being made, and widening it hands the untouched backlog back to the worker.
4. One provider call embeds the batch, one purpose tagged usage row records
   what it cost, and each chunk records the model identity that produced its
   vector.

Nothing here runs on the gateway request path. The gateway already indexes
there and is deliberately failure tolerant; a provider round trip between an
agent and its response is exactly what this design exists to avoid.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, time as day_time
from typing import Any, List, Optional, Protocol, Sequence

import httpx
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import (
    crud_api_usage,
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_embedding_setting import (
    SessionEmbeddingConfigError,
    validate_openai_compatible_base_url,
)
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    DEGRADED_DIMENSION_MISMATCH,
    DEGRADED_MISCONFIGURED,
    DEGRADED_PROVIDER_ERROR,
    DEGRADED_UNPRICED_MODEL,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
    SessionEmbeddingSetting,
    source_kinds_for_scope,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    SessionSearchDocument,
)
from preloop.services.model_pricing import estimate_external_model_usage_cost

logger = logging.getLogger(__name__)

#: ``meta_data.purpose`` on the usage row a batch writes. Registered in
#: ``crud/api_usage.py`` (so the cap query accepts it) and in
#: ``crud/runtime_session.py`` (so session rollups exclude it).
SESSION_EMBEDDING_PURPOSE = "session_embedding"

#: Endpoint recorded on the usage row. It is not an HTTP route on this
#: service; it names where the spend came from in a ledger whose other rows
#: are gateway paths.
USAGE_ENDPOINT = "internal/session-embedding"

#: Characters per token used when the provider reports no usage block. Same
#: constant the budget preflight uses, and deliberately an over-estimate:
#: a cap that under-counts is not a cap.
CHARS_PER_TOKEN = 4.0

# Run outcomes. None of these is an error: a caller that gets `degraded`
# back has lost no data and should not retry harder.
STATUS_OK = "ok"
STATUS_IDLE = "idle"
STATUS_DISABLED = "disabled"
STATUS_DEGRADED = "degraded"


@dataclass(frozen=True)
class EmbeddingBatchResult:
    """What one run of the worker did, and why it did not do more."""

    account_id: str
    status: str
    reason: Optional[str] = None
    runtime_session_id: Optional[str] = None
    embedded: int = 0
    pending: int = 0
    tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: Optional[str] = None
    model_identity: Optional[str] = None

    @property
    def degraded(self) -> bool:
        """Whether the run stopped short of its work on purpose."""
        return self.status == STATUS_DEGRADED


class EmbeddingProviderError(RuntimeError):
    """The provider could not produce vectors for this batch."""


class EmbeddingProvider(Protocol):
    """Anything that turns a batch of texts into a batch of vectors."""

    #: Model name as the provider knows it, recorded on the usage row.
    model: str

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one vector per input, in input order."""


class OpenAICompatibleEmbeddingProvider:
    """POST ``{base_url}/embeddings`` and read OpenAI's response shape.

    Named by base url rather than by vendor because that is the provider a
    self hosted or air gapped install actually has: whatever the operator
    runs, reachable on their own network, with the text never leaving it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dimensions = dimensions
        self.timeout = timeout
        self.last_usage: Optional[dict] = None

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch in one request, preserving input order."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {"model": self.model, "input": list(texts)}
        if self.dimensions:
            # Providers that do not implement the parameter ignore it; the
            # width is validated against the column before anything is stored.
            payload["dimensions"] = self.dimensions
        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The message names the endpoint and the model, never the text:
            # the input is customer session content.
            raise EmbeddingProviderError(
                f"embeddings request to {self.base_url} for model "
                f"{self.model} failed: {type(exc).__name__}"
            ) from exc

        self.last_usage = body.get("usage") if isinstance(body, dict) else None
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingProviderError(
                f"embeddings response from {self.base_url} returned "
                f"{len(items) if isinstance(items, list) else 'no'} vectors "
                f"for {len(texts)} inputs"
            )
        ordered = sorted(
            items,
            key=lambda item: (
                item.get("index") if isinstance(item.get("index"), int) else 0
            ),
        )
        vectors: List[List[float]] = []
        for item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                raise EmbeddingProviderError(
                    f"embeddings response from {self.base_url} carried no vector"
                )
            vectors.append([float(value) for value in vector])
        return vectors


class LocalEmbeddingProvider:
    """A sentence-transformers model loaded in this process.

    The path ``crud/embedding.py`` already uses for issue embeddings. Nothing
    leaves the host, so an air gapped install that will not talk to any
    endpoint at all still gets semantic search.
    """

    def __init__(self, *, model: str) -> None:
        self.model = model
        self._encoder: Any = None

    def _load(self) -> Any:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise EmbeddingProviderError(
                    "the local embedding provider needs sentence-transformers installed"
                ) from exc
            self._encoder = SentenceTransformer(self.model)
        return self._encoder

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Encode a batch locally, preserving input order."""
        try:
            encoded = self._load().encode(list(texts))
        except EmbeddingProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider failures are data
            raise EmbeddingProviderError(
                f"local embedding model {self.model} failed: {type(exc).__name__}"
            ) from exc
        return [[float(value) for value in vector] for vector in encoded]


def embedding_enabled() -> bool:
    """Whether the deployment allows embedding at all.

    Read on every run rather than cached, so flipping the kill switch stops
    embedding without restarting a long lived worker. Keyword indexing has a
    switch of its own and is unaffected.
    """
    return bool(getattr(settings, "session_embedding_enabled", True))


def _deployment_api_key_for(base_url: str) -> Optional[str]:
    """Return the shared key only when ``base_url`` is operator allow-listed.

    An empty allow-list means the key is never sent. That is the safe
    default: the account names the endpoint, and a shared credential must
    not ride to a host the account chose.
    """
    allowed = getattr(settings, "session_embedding_api_key_base_urls", "") or ""
    wanted = (base_url or "").strip().rstrip("/")
    if not wanted:
        return None
    for item in str(allowed).split(","):
        if item.strip().rstrip("/") == wanted:
            key = getattr(settings, "session_embedding_api_key", None)
            return str(key) if key else None
    return None


def build_provider(setting: SessionEmbeddingSetting) -> EmbeddingProvider:
    """Construct the provider one account's setting names.

    Raises:
        EmbeddingProviderError: The setting names nothing usable.
    """
    model = (setting.model_identifier or "").strip()
    if not model:
        raise EmbeddingProviderError("the account setting names no model")
    if setting.provider == PROVIDER_LOCAL:
        return LocalEmbeddingProvider(model=model)
    if setting.provider == PROVIDER_OPENAI_COMPATIBLE:
        if not (setting.base_url or "").strip():
            raise EmbeddingProviderError(
                "the account setting names no base url for its provider"
            )
        try:
            validate_openai_compatible_base_url(setting.base_url or "")
        except SessionEmbeddingConfigError as exc:
            raise EmbeddingProviderError(str(exc)) from exc
        return OpenAICompatibleEmbeddingProvider(
            base_url=setting.base_url or "",
            model=model,
            api_key=_deployment_api_key_for(setting.base_url or ""),
            dimensions=int(setting.dimensions or EMBEDDING_DIMENSIONS),
            timeout=float(getattr(settings, "session_embedding_timeout_seconds", 30.0)),
        )
    raise EmbeddingProviderError(f"unsupported provider {setting.provider!r}")


def _daily_cap_for(setting: SessionEmbeddingSetting) -> float:
    """The account's cap, falling back to the deployment default."""
    if setting.daily_cap_usd is not None:
        return max(0.0, float(setting.daily_cap_usd))
    return max(0.0, float(getattr(settings, "session_embedding_daily_cap_usd", 2.0)))


def _day_start(now: Optional[datetime] = None) -> datetime:
    stamp = now or datetime.now(UTC)
    return datetime.combine(stamp.date(), day_time.min, tzinfo=UTC)


def _estimated_tokens(texts: Sequence[str]) -> int:
    """Token count for a batch when the provider reported none."""
    characters = sum(len(text or "") for text in texts)
    return max(1, int(characters / CHARS_PER_TOKEN))


def _reported_tokens(
    provider: EmbeddingProvider, texts: Sequence[str]
) -> tuple[int, str]:
    """Prefer the provider's own count; fall back to the estimate.

    Returns the count and the ``usage_source`` the ledger records for it, so
    a reconciliation can tell a counted batch from an estimated one.
    """
    usage = getattr(provider, "last_usage", None)
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                return value, "provider"
    return _estimated_tokens(texts), "estimated"


def _degrade(
    db: Session,
    *,
    account_id: str,
    reason: str,
    pending: int,
    runtime_session_id: Optional[str] = None,
) -> EmbeddingBatchResult:
    """Record a degraded marker and report it. Never raises, never errors."""
    crud_session_embedding_setting.mark_degraded(
        db, account_id=account_id, reason=reason, commit=True
    )
    logger.info(
        "Session embedding degraded for account %s (%s); %s chunks left pending",
        account_id,
        reason,
        pending,
    )
    return EmbeddingBatchResult(
        account_id=str(account_id),
        status=STATUS_DEGRADED,
        reason=reason,
        runtime_session_id=runtime_session_id,
        pending=pending,
    )


def run_account_batch(
    db: Session,
    *,
    account_id: Any,
    provider: Optional[EmbeddingProvider] = None,
    batch_size: Optional[int] = None,
    now: Optional[datetime] = None,
) -> EmbeddingBatchResult:
    """Embed at most one batch of one account's waiting chunks.

    Args:
        db: Session the worker owns. The usage row is committed through it.
        account_id: Account whose backlog to work on.
        provider: Injected provider; built from the account setting when
            omitted. Tests pass a fake so no suite ever calls out.
        batch_size: Chunks in this batch; the configured size when omitted.
        now: Clock override for the daily window.

    Returns:
        What the run did. ``degraded`` means it deliberately did less.
    """
    account = str(account_id)
    if not embedding_enabled():
        return EmbeddingBatchResult(
            account_id=account, status=STATUS_DISABLED, reason="kill_switch"
        )

    setting = crud_session_embedding_setting.get_for_account(db, account_id=account_id)
    if setting is None or not setting.enabled:
        return EmbeddingBatchResult(
            account_id=account, status=STATUS_DISABLED, reason="account_opt_out"
        )

    # What the account's scope admits. ``None`` is ``full``: every kind.
    # summaries_only is the default, so an account that never said anything
    # embeds one short chunk per session rather than its whole transcript.
    source_kinds = source_kinds_for_scope(setting.scope)

    held_sessions = crud_session_search_document.held_runtime_session_ids(
        db, account_id=account_id
    )
    pending = crud_session_search_document.count_pending_embeddings(
        db,
        account_id=account_id,
        excluded_session_ids=held_sessions,
        now=now,
        source_kinds=source_kinds,
    )
    if pending == 0:
        return EmbeddingBatchResult(account_id=account, status=STATUS_IDLE)

    cap = _daily_cap_for(setting)
    spent = crud_api_usage.get_gateway_spend(
        db,
        account_id=account,
        start=_day_start(now),
        purpose=SESSION_EMBEDDING_PURPOSE,
    )
    if cap <= 0 or spent >= cap:
        return _degrade(
            db, account_id=account, reason=DEGRADED_DAILY_CAP, pending=pending
        )

    if setting.provider == PROVIDER_OPENAI_COMPATIBLE:
        probe = estimate_external_model_usage_cost(
            setting.model_identifier or "",
            prompt_tokens=1,
            completion_tokens=0,
        )
        if probe.source == "unpriced" or probe.cost is None:
            return _degrade(
                db,
                account_id=account,
                reason=DEGRADED_UNPRICED_MODEL,
                pending=pending,
            )

    try:
        active_provider = provider or build_provider(setting)
    except EmbeddingProviderError as exc:
        logger.warning(
            "Session embedding misconfigured for account %s: %s", account, exc
        )
        return _degrade(
            db, account_id=account, reason=DEGRADED_MISCONFIGURED, pending=pending
        )

    size = int(batch_size or getattr(settings, "session_embedding_batch_size", 32))
    claimed = crud_session_search_document.claim_pending_chunks(
        db,
        account_id=account_id,
        limit=size,
        excluded_session_ids=held_sessions,
        now=now,
        source_kinds=source_kinds,
        commit=True,
    )
    if not claimed:
        return EmbeddingBatchResult(account_id=account, status=STATUS_IDLE)

    runtime_session_id = str(claimed[0].runtime_session_id)
    texts = [row.content for row in claimed]
    started = time.perf_counter()
    try:
        vectors = active_provider.embed(texts)
    except EmbeddingProviderError as exc:
        logger.warning(
            "Session embedding provider failed for account %s: %s", account, exc
        )
        _release(db, claimed)
        return _degrade(
            db,
            account_id=account,
            reason=DEGRADED_PROVIDER_ERROR,
            pending=pending,
            runtime_session_id=runtime_session_id,
        )
    except Exception:
        logger.exception(
            "Session embedding failed after claiming chunks for account %s",
            account,
        )
        _release(db, claimed)
        return _degrade(
            db,
            account_id=account,
            reason=DEGRADED_PROVIDER_ERROR,
            pending=pending,
            runtime_session_id=runtime_session_id,
        )
    duration = time.perf_counter() - started

    width = int(setting.dimensions or EMBEDDING_DIMENSIONS)
    if len(vectors) != len(claimed) or any(len(v) != width for v in vectors):
        logger.warning(
            "Session embedding for account %s returned %s vectors of unexpected "
            "width; expected %s of %s dimensions",
            account,
            len(vectors),
            len(claimed),
            width,
        )
        _release(db, claimed)
        return _degrade(
            db,
            account_id=account,
            reason=DEGRADED_DIMENSION_MISMATCH,
            pending=pending,
            runtime_session_id=runtime_session_id,
        )

    model_identity = setting.model_identity or f"{setting.provider}:{width}"
    try:
        written = crud_session_search_document.store_embeddings(
            db,
            vectors=list(zip(claimed, vectors, strict=False)),
            model_identity=model_identity,
            commit=True,
        )
    except Exception:
        logger.exception(
            "Session embedding could not store vectors for account %s",
            account,
        )
        _release(db, claimed)
        return _degrade(
            db,
            account_id=account,
            reason=DEGRADED_PROVIDER_ERROR,
            pending=pending,
            runtime_session_id=runtime_session_id,
        )

    tokens, usage_source = _reported_tokens(active_provider, texts)
    estimate = estimate_external_model_usage_cost(
        getattr(active_provider, "model", "") or (setting.model_identifier or ""),
        prompt_tokens=tokens,
        completion_tokens=0,
    )
    _record_usage(
        db,
        account_id=account,
        runtime_session_id=runtime_session_id,
        setting=setting,
        model_identity=model_identity,
        chunk_count=written,
        tokens=tokens,
        usage_source=usage_source,
        duration=duration,
        estimate=estimate,
    )
    crud_session_embedding_setting.clear_degraded(
        db, account_id=account_id, commit=True
    )

    remaining = crud_session_search_document.count_pending_embeddings(
        db,
        account_id=account_id,
        excluded_session_ids=held_sessions,
        now=now,
        source_kinds=source_kinds,
    )
    return EmbeddingBatchResult(
        account_id=account,
        status=STATUS_OK,
        runtime_session_id=runtime_session_id,
        embedded=written,
        pending=remaining,
        tokens=tokens,
        cost_usd=estimate.cost,
        cost_source=estimate.source,
        model_identity=model_identity,
    )


def _release(db: Session, chunks: Sequence[SessionSearchDocument]) -> None:
    """Hand a failed batch back to the queue without failing the run."""
    try:
        crud_session_search_document.release_claim(
            db,
            chunks=chunks,
            max_attempts=int(getattr(settings, "session_embedding_max_attempts", 3)),
            commit=True,
        )
    except Exception:  # noqa: BLE001 - releasing must not raise over a failure
        db.rollback()
        logger.warning("Session embedding could not release its claim", exc_info=True)


def _record_usage(
    db: Session,
    *,
    account_id: str,
    runtime_session_id: str,
    setting: SessionEmbeddingSetting,
    model_identity: str,
    chunk_count: int,
    tokens: int,
    usage_source: str,
    duration: float,
    estimate: Any,
) -> None:
    """Write the one purpose tagged usage row this batch is allowed.

    The row carries the session it indexed, which is what makes the exclusion
    testable: without the purpose tag this spend would land in that session's
    reported cost.
    """
    try:
        crud_api_usage.log_gateway_request(
            db,
            endpoint=USAGE_ENDPOINT,
            method="POST",
            status_code=200,
            duration=duration,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
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
                "chunks": chunk_count,
                "dimensions": int(setting.dimensions or EMBEDDING_DIMENSIONS),
                "model_identity": model_identity,
            },
        )
    except Exception:  # noqa: BLE001 - the vectors are written; the row is not
        db.rollback()
        logger.warning(
            "Session embedding could not record usage for account %s",
            account_id,
            exc_info=True,
        )
