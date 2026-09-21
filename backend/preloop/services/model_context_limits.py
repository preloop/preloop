"""What a model's context window and output ceiling actually are (#851).

A harness that does not know the window it is working in cannot compact at
the right moment. Codex defaults to a conservative window when
``config.toml`` says nothing, so a run on a 1M-token model behaves as if it
had a fraction of that: it compacts early, re-reads what it just dropped, and
spends its budget re-establishing context it already had. Execution
``a50ba8ff`` sent 10.13M prompt tokens over 86 requests and never edited a
file.

Preloop already knows both numbers twice over, and neither was being passed
on:

1. The model row. ``ai_model.model_parameters`` is operator-owned and wins,
   because an operator who wrote a number there meant it (a provisioned
   deployment with a smaller window than the public model, for example).
2. The vendored price catalog, ``services/data/model_prices.json``, which
   carries ``max_input_tokens`` and ``max_output_tokens`` for every model it
   prices. It is a release-pinned snapshot, so the answer is deterministic
   per release.

When neither source has a number the limit is omitted and the harness keeps
its own default. Nothing is guessed: a wrong window is worse than no window,
because the harness would trust it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence

from preloop.services.model_price_catalog import CATALOG_PATH

logger = logging.getLogger(__name__)

#: ``model_parameters`` keys read as the context window, in order. Several
#: spellings are accepted because operators wrote them before this existed.
CONTEXT_WINDOW_KEYS: Sequence[str] = (
    "context_window",
    "max_input_tokens",
    "model_context_window",
)

#: ``model_parameters`` keys read as the output ceiling, in order.
MAX_OUTPUT_KEYS: Sequence[str] = (
    "max_output_tokens",
    "model_max_output_tokens",
    "max_tokens",
)

#: Below this, the number is a typo or a per-request cap somebody stored in
#: the wrong field; a harness told its window is 500 tokens is unusable.
MIN_CONTEXT_WINDOW = 1024
MIN_MAX_OUTPUT_TOKENS = 256


@dataclass(frozen=True)
class ModelContextLimits:
    """The two numbers a harness needs, and where each came from.

    Attributes:
        context_window: Total input tokens the model accepts, or None.
        max_output_tokens: Output tokens per response, or None.
        context_window_source: ``"model_row"``, ``"catalog"`` or None.
        max_output_tokens_source: ``"model_row"``, ``"catalog"`` or None.
        catalog_key: The catalog entry that answered, for the log line.
    """

    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    context_window_source: Optional[str] = None
    max_output_tokens_source: Optional[str] = None
    catalog_key: Optional[str] = None

    @property
    def known(self) -> bool:
        """Whether anything at all was resolved."""
        return self.context_window is not None or self.max_output_tokens is not None


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    """The vendored price snapshot, read once per process.

    The same file :mod:`model_price_catalog` registers with litellm, read
    directly here because this needs two fields litellm does not expose
    uniformly across versions.
    """
    try:
        loaded: Any = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Model price catalog unreadable at %s", CATALOG_PATH)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _positive_int(value: Any, minimum: int) -> Optional[int]:
    """One usable token count, or None for anything else.

    Booleans are rejected: ``True`` is not a context window.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


def _from_mapping(source: Any, keys: Sequence[str], minimum: int) -> Optional[int]:
    """First usable value among ``keys`` in an operator-written mapping."""
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        value = _positive_int(source.get(key), minimum)
        if value is not None:
            return value
    return None


def catalog_candidates(
    model_identifier: Optional[str],
    *,
    provider_name: Optional[str] = None,
    model_alias: Optional[str] = None,
) -> list[str]:
    """Catalog keys to try, most specific first.

    The snapshot keys a model in several ways (``gpt-5.4``,
    ``anthropic/claude-sonnet-4-5``, ``vertex_ai.gemini-3-pro``), so the
    lookup tries the spellings Preloop holds rather than assuming one.

    Args:
        model_identifier: The model id as the provider spells it.
        provider_name: The provider the model row names.
        model_alias: The gateway alias, when the run goes through it.

    Returns:
        Candidate keys, de-duplicated, in lookup order.
    """
    provider = (provider_name or "").strip().lower()
    candidates: list[str] = []

    def add(value: Optional[str]) -> None:
        if not value:
            return
        cleaned = value.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    for raw in (model_alias, model_identifier):
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        add(value)
        if "/" in value:
            bare = value.rsplit("/", 1)[1]
            add(bare)
            add(value.replace("/", "."))
        else:
            bare = value
        if provider:
            add(f"{provider}/{bare}")
            add(f"{provider}.{bare}")
    return candidates


def catalog_limits(
    model_identifier: Optional[str],
    *,
    provider_name: Optional[str] = None,
    model_alias: Optional[str] = None,
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Look the two numbers up in the vendored catalog.

    Args:
        model_identifier: The model id as the provider spells it.
        provider_name: The provider the model row names.
        model_alias: The gateway alias, when the run goes through it.

    Returns:
        ``(context_window, max_output_tokens, catalog_key)``, each None when
        the snapshot does not say.
    """
    catalog = _catalog()
    for candidate in catalog_candidates(
        model_identifier, provider_name=provider_name, model_alias=model_alias
    ):
        entry = catalog.get(candidate)
        if not isinstance(entry, Mapping):
            continue
        context_window = _positive_int(
            entry.get("max_input_tokens"), MIN_CONTEXT_WINDOW
        )
        # ``max_tokens`` is litellm's older spelling of the output ceiling and
        # is still the only one some rows carry.
        max_output = _positive_int(
            entry.get("max_output_tokens"), MIN_MAX_OUTPUT_TOKENS
        ) or _positive_int(entry.get("max_tokens"), MIN_MAX_OUTPUT_TOKENS)
        if context_window is not None or max_output is not None:
            return context_window, max_output, candidate
    return None, None, None


def resolve_model_context_limits(
    *,
    model_identifier: Optional[str],
    provider_name: Optional[str] = None,
    model_alias: Optional[str] = None,
    model_parameters: Any = None,
) -> ModelContextLimits:
    """Resolve the window and the output ceiling for one run.

    The model row wins per field: an operator who wrote a number meant it,
    and a provisioned deployment can legitimately differ from the public
    model the catalog describes. Each field is resolved independently, so a
    row that pins only the output ceiling still gets its window from the
    catalog.

    Args:
        model_identifier: The model id as the provider spells it.
        provider_name: The provider the model row names.
        model_alias: The gateway alias, when the run goes through it.
        model_parameters: ``ai_model.model_parameters`` as stored.

    Returns:
        The resolved limits, with the source of each field. Everything None
        means neither source knew, and the caller must omit the setting
        rather than invent one.
    """
    row_context = _from_mapping(
        model_parameters, CONTEXT_WINDOW_KEYS, MIN_CONTEXT_WINDOW
    )
    row_output = _from_mapping(model_parameters, MAX_OUTPUT_KEYS, MIN_MAX_OUTPUT_TOKENS)

    catalog_context: Optional[int] = None
    catalog_output: Optional[int] = None
    catalog_key: Optional[str] = None
    if row_context is None or row_output is None:
        catalog_context, catalog_output, catalog_key = catalog_limits(
            model_identifier, provider_name=provider_name, model_alias=model_alias
        )

    context_window = row_context if row_context is not None else catalog_context
    max_output = row_output if row_output is not None else catalog_output
    return ModelContextLimits(
        context_window=context_window,
        max_output_tokens=max_output,
        context_window_source=(
            "model_row"
            if row_context is not None
            else ("catalog" if catalog_context is not None else None)
        ),
        max_output_tokens_source=(
            "model_row"
            if row_output is not None
            else ("catalog" if catalog_output is not None else None)
        ),
        catalog_key=catalog_key if (catalog_context or catalog_output) else None,
    )


def limits_for_execution(execution_context: Mapping[str, Any]) -> ModelContextLimits:
    """The limits for the model this execution runs on.

    Args:
        execution_context: The harness execution context.

    Returns:
        The resolved limits. Never raises: a harness must start even when the
        catalog is missing.
    """
    try:
        return resolve_model_context_limits(
            model_identifier=execution_context.get("model_identifier"),
            provider_name=execution_context.get("model_provider"),
            model_alias=execution_context.get("model_gateway_model_alias"),
            model_parameters=execution_context.get("model_parameters"),
        )
    except Exception:  # noqa: BLE001 - never block a run on a lookup
        logger.exception("Could not resolve model context limits")
        return ModelContextLimits()
