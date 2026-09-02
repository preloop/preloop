"""Account model-catalog sync against live provider discovery.

``preloop models sync`` (and its backing endpoint) exists because AI-model
rows are otherwise created only interactively: live provider discovery
(:mod:`preloop.services.ai_model_provider`) runs at model-add time in the
console, so a newly released provider model never enters the account catalog
on its own. This service runs the same live discovery against credentials the
account already stores and adds the missing models through the CRUD layer.

Authorization semantics are preserved, not widened: new rows share the seed
model's credential secret and inherit the seed's gateway exposure, so an
API-key-backed provider gains account-wide models exactly as if an operator
had added them in the console. Principal-bound subscription-OAuth credentials
(Claude Code / Codex) are never used for discovery: they cannot authenticate
server-side listing calls and their models are only authorized per managed
agent binding, which ``preloop agents refresh`` handles client-side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model, crud_audit_log
from preloop.models.models.ai_model import AIModel
from preloop.models.models.user import User
from preloop.services.ai_model_provider import (
    ERROR_MISSING_KEY,
    ERROR_UNSUPPORTED,
    OPENAI_COMPATIBLE_PROVIDERS,
    get_available_models_for_provider,
)
from preloop.services.model_runtime_resolver import effective_gateway_alias
from preloop.utils.audit import log_config_change

logger = logging.getLogger(__name__)

# Providers the sync deliberately does not discover against. The
# OpenAI-compatible family has no curated first-party catalog (OpenRouter
# alone lists hundreds of models), and Bedrock listing needs the AWS
# credential envelope the console flow collects interactively.
UNSYNCABLE_PROVIDERS = frozenset(OPENAI_COMPATIBLE_PROVIDERS) | {"bedrock"}


@dataclass
class ProviderCatalogSyncResult:
    """Outcome of syncing one provider's catalog."""

    provider: str
    source: str = "fallback"  # "live" when the provider listing answered
    error: Optional[str] = None
    discovered: int = 0
    added: List[str] = field(default_factory=list)  # gateway aliases added
    skipped_existing: int = 0
    note: Optional[str] = None


@dataclass
class CatalogSyncSummary:
    """Outcome of one catalog sync run across providers."""

    providers: List[ProviderCatalogSyncResult] = field(default_factory=list)
    dry_run: bool = False


def _resolve_seed_model(
    db: Session, account_id, provider_models: List[AIModel]
) -> tuple[Optional[AIModel], Optional[str]]:
    """Pick the model whose stored credential can authenticate discovery.

    Only non-principal-bound credentials qualify: subscription OAuth bundles
    (Claude Code / Codex) cannot authenticate server-side listing calls and
    their models are per-agent anyway.

    Returns:
        The seed model and its decrypted listing secret, or ``(None, None)``.
    """
    for candidate in provider_models:
        if bool(getattr(candidate, "is_principal_bound_oauth", False)):
            continue
        loaded = crud_ai_model.get_for_account(
            db, id=candidate.id, account_id=account_id
        )
        if loaded is None:
            continue
        try:
            secret = crud_ai_model.resolve_listing_secret(loaded)
        except ValueError:
            logger.warning(
                "Could not decrypt stored credentials for model %s during catalog sync",
                candidate.id,
            )
            continue
        if secret:
            return loaded, secret
    return None, None


def _existing_identifiers(provider_models: List[AIModel]) -> set[str]:
    return {
        (model.model_identifier or "").strip().lower()
        for model in provider_models
        if (model.model_identifier or "").strip()
    }


async def sync_account_model_catalog(
    db: Session,
    *,
    user: User,
    provider: Optional[str] = None,
    dry_run: bool = False,
    request=None,
) -> CatalogSyncSummary:
    """Discover newly released provider models and add them to the catalog.

    For every provider the account already has credentialed models for (or
    only ``provider`` when given), this runs the existing live discovery with
    the stored credential and creates one AIModel row per newly discovered
    identifier via the CRUD layer. New rows share the seed model's credential
    secret and inherit its gateway exposure.

    Args:
        db: Active database session.
        user: The operator running the sync (audit attribution and account
            scoping).
        provider: Optional provider filter, e.g. ``"anthropic"``.
        dry_run: When True, report what would be added without writing.
        request: Optional FastAPI request for audit IP/user-agent context.

    Returns:
        Per-provider results: discovery provenance, added aliases, and
        skip/error notes.
    """
    account_models = crud_ai_model.get_by_account(db=db, account_id=user.account_id)
    provider_filter = (provider or "").strip().lower()

    grouped: dict[str, List[AIModel]] = {}
    for model in account_models:
        provider_name = (model.provider_name or "").strip().lower()
        if not provider_name:
            continue
        if provider_filter and provider_name != provider_filter:
            continue
        grouped.setdefault(provider_name, []).append(model)

    summary = CatalogSyncSummary(dry_run=dry_run)
    if provider_filter and provider_filter not in grouped:
        summary.providers.append(
            ProviderCatalogSyncResult(
                provider=provider_filter,
                error=ERROR_MISSING_KEY,
                note=(
                    "no account models exist for this provider; add the first "
                    "model (with its credential) in the console or via "
                    "'preloop agents onboard'"
                ),
            )
        )
        return summary

    for provider_name in sorted(grouped):
        result = await _sync_provider_catalog(
            db,
            user=user,
            provider_name=provider_name,
            provider_models=grouped[provider_name],
            dry_run=dry_run,
            request=request,
        )
        summary.providers.append(result)
    return summary


async def _sync_provider_catalog(
    db: Session,
    *,
    user: User,
    provider_name: str,
    provider_models: List[AIModel],
    dry_run: bool,
    request=None,
) -> ProviderCatalogSyncResult:
    """Sync one provider's catalog; see :func:`sync_account_model_catalog`."""
    result = ProviderCatalogSyncResult(provider=provider_name)

    if provider_name in UNSYNCABLE_PROVIDERS:
        result.error = ERROR_UNSUPPORTED
        result.note = (
            "this provider has no bounded first-party catalog to sync "
            "(openai-compatible endpoints) or needs interactive credentials "
            "(bedrock); add models in the console instead"
        )
        return result

    seed, secret = _resolve_seed_model(db, user.account_id, provider_models)
    if seed is None or not secret:
        result.error = ERROR_MISSING_KEY
        result.note = (
            "no API-key credential is stored for this provider; "
            "subscription-OAuth (Claude Code / Codex) credentials cannot "
            "authenticate server-side discovery"
        )
        return result

    try:
        discovery = await get_available_models_for_provider(
            provider_name,
            secret,
            "llm",
            (seed.api_endpoint or "").strip() or None,
        )
    except ValueError as exc:
        # ProviderAuthError / ProviderValidationError both subclass
        # ValueError; the message is our own fixed text, never key material.
        result.error = "auth"
        result.note = str(exc)
        return result

    result.source = discovery.source
    result.error = discovery.error
    result.discovered = len(discovery.models)
    if discovery.source != "live":
        result.note = "live discovery failed; nothing was added"
        return result

    existing = _existing_identifiers(provider_models)
    seed_gateway_enabled = effective_gateway_alias(seed) is not None
    added_aliases: List[str] = []
    for model_identifier in discovery.models:
        identifier = (model_identifier or "").strip()
        if not identifier or identifier.lower() in existing:
            result.skipped_existing += 1
            continue
        alias = f"{provider_name}/{identifier}"
        added_aliases.append(alias)
        existing.add(identifier.lower())
        if dry_run:
            continue
        crud_ai_model.create_with_account(
            db=db,
            obj_in={
                "name": identifier,
                "description": (
                    f"Added by preloop models sync from live {provider_name} discovery"
                ),
                "provider_name": seed.provider_name,
                "model_identifier": identifier,
                "api_endpoint": seed.api_endpoint,
                "credentials_secret_id": seed.credentials_secret_id,
                "meta_data": {
                    "managed_by": "preloop models sync",
                    "gateway": {
                        "enabled": seed_gateway_enabled,
                        "model_alias": alias,
                    },
                },
            },
            account_id=user.account_id,
            commit=False,
        )

    if not dry_run and added_aliases:
        db.commit()

    result.added = added_aliases
    if added_aliases:
        _audit_catalog_sync(
            db,
            user=user,
            provider_name=provider_name,
            added=added_aliases,
            dry_run=dry_run,
            request=request,
        )
    return result


def _audit_catalog_sync(
    db: Session,
    *,
    user: User,
    provider_name: str,
    added: List[str],
    dry_run: bool,
    request=None,
) -> None:
    """Record the sync in the audit trail (OSS audit log + EE audit plugin)."""
    details = {
        "provider": provider_name,
        "added": added,
        "dry_run": dry_run,
    }
    try:
        crud_audit_log.log_action(
            db,
            account_id=user.account_id,
            user_id=user.id,
            action="ai_model_catalog_sync",
            resource_type="ai_model",
            status="success",
            details=details,
        )
    except Exception:  # pragma: no cover - audit must not break the sync
        logger.debug("Audit log for model catalog sync failed", exc_info=True)
    log_config_change(
        db,
        user=user,
        config_type="ai_model",
        action="synced",
        new_value=details,
        request=request,
    )
