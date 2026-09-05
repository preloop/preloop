"""Resolve subject ``allowed_models`` entries against account AI models.

Operators write allowlists in the console, which historically persisted
whatever the model picker showed (an AI model display name such as
``Kimi K3``), while the gateway compared those entries only against alias
spellings (``moonshot/kimi-k3``, ``kimi-k3``). This module accepts every key an
operator may reasonably have stored for one model:

* a gateway alias or any spelling the gateway resolver accepts for the model
  (``gateway_model_alias_candidates``),
* the ``AIModel`` id (uuid string), or
* the ``AIModel`` display name (case-insensitive, trimmed).

The gateway preflight and the console endpoints share these helpers so the
policy that is written is the policy that is enforced.
"""

from __future__ import annotations

from typing import Iterable, Optional

from preloop.models.models.ai_model import AIModel
from preloop.services.model_runtime_resolver import (
    effective_gateway_alias,
    gateway_model_alias_candidates,
)

# How many allowlist entries a denial message spells out before eliding.
MAX_LISTED_ALLOWED_MODELS = 5


def normalize_allowed_models(entries: Optional[Iterable[object]]) -> list[str]:
    """Return the trimmed, non-empty allowlist entries in their stored order.

    Args:
        entries: The raw ``allowed_models`` value from a governance config.

    Returns:
        Entries with surrounding whitespace removed and blanks dropped;
        duplicates are collapsed to their first occurrence.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for item in entries or []:
        text = str(item).strip()
        if text and text not in seen:
            normalized.append(text)
            seen.add(text)
    return normalized


def allowlist_entry_matches_model(entry: str, ai_model: AIModel) -> bool:
    """True when one stored allowlist entry names ``ai_model``.

    Aliases compare exactly (they are gateway wire strings); ids and display
    names compare case-insensitively because operators type them by hand.

    Args:
        entry: One stored ``allowed_models`` value.
        ai_model: The account model to test against.

    Returns:
        Whether the entry addresses this model by alias, id, or display name.
    """
    text = str(entry).strip()
    if not text:
        return False
    folded = text.casefold()
    if folded == str(ai_model.id).casefold():
        return True
    name = (ai_model.name or "").strip()
    if name and folded == name.casefold():
        return True
    return text in gateway_model_alias_candidates(ai_model)


def resolve_allowed_model_ids(
    entries: Optional[Iterable[object]], account_models: Iterable[AIModel]
) -> set[str]:
    """Map allowlist entries onto the account inventory.

    Args:
        entries: The raw ``allowed_models`` value from a governance config.
        account_models: The models visible to the account (owned plus system).

    Returns:
        The id strings of every inventory model named by at least one entry.
        Entries that name nothing (typos, models since deleted) resolve to no
        id and are simply absent from the result.
    """
    normalized = normalize_allowed_models(entries)
    if not normalized:
        return set()
    resolved: set[str] = set()
    for model in account_models:
        if any(allowlist_entry_matches_model(entry, model) for entry in normalized):
            resolved.add(str(model.id))
    return resolved


def allowlist_permits_model(
    entries: Optional[Iterable[object]],
    ai_model: AIModel,
    *,
    requested_spellings: Optional[Iterable[str]] = None,
) -> bool:
    """Decide whether one subject allowlist permits a request for ``ai_model``.

    An empty allowlist permits everything. Otherwise the request passes when
    any entry names the resolved model (alias, id, or display name), or when
    any spelling the caller supplied (typically the raw wire ``model`` string)
    is listed verbatim, which keeps historical allowlists working.

    Because the gateway always resolves a request to a model from the account
    inventory, testing the resolved model directly is equivalent to resolving
    the entries through ``resolve_allowed_model_ids`` and checking membership,
    without an extra inventory query per governed request.

    Args:
        entries: The raw ``allowed_models`` value from a governance config.
        ai_model: The model the gateway resolved the request to.
        requested_spellings: Extra spellings that should count as naming the
            model, such as the raw request string.

    Returns:
        True when the request may proceed under this allowlist.
    """
    normalized = normalize_allowed_models(entries)
    if not normalized:
        return True
    entry_set = set(normalized)
    if any(str(s).strip() in entry_set for s in requested_spellings or [] if s):
        return True
    return any(allowlist_entry_matches_model(entry, ai_model) for entry in normalized)


def requested_model_label(ai_model: AIModel, requested: object) -> str:
    """Return the spelling to quote back to a client in a denial message.

    Prefers what the client actually sent; falls back to the model's gateway
    alias, then to its identifier or display name.

    Args:
        ai_model: The resolved model.
        requested: The raw ``model`` value from the request payload.

    Returns:
        A non-empty human-readable model label.
    """
    if isinstance(requested, str) and requested.strip():
        return requested.strip()
    alias = effective_gateway_alias(ai_model)
    if alias:
        return alias
    return (ai_model.model_identifier or ai_model.name or "unknown").strip()


def format_model_not_allowed_detail(
    requested_model: str, allowed_models: Optional[Iterable[object]]
) -> str:
    """Render the client-facing detail for a ``subject_model_not_allowed`` denial.

    Args:
        requested_model: The spelling to quote (see ``requested_model_label``).
        allowed_models: The allowlist that denied the request.

    Returns:
        One sentence naming the model and the allowlist (at most
        ``MAX_LISTED_ALLOWED_MODELS`` entries, then ``...``) followed by the
        remediation.
    """
    normalized = normalize_allowed_models(allowed_models)
    listed = ", ".join(normalized[:MAX_LISTED_ALLOWED_MODELS])
    if len(normalized) > MAX_LISTED_ALLOWED_MODELS:
        listed += ", ..."
    return (
        f"Model '{requested_model}' is not in this agent's allowed models "
        f"({listed}). Edit the agent's governance in the Preloop console or "
        "pick an allowed model."
    )


def is_model_not_allowed_detail(detail: Optional[str]) -> bool:
    """True when a gateway error detail was produced by ``format_model_not_allowed_detail``."""
    return bool(detail) and "is not in this agent's allowed models" in str(detail)
