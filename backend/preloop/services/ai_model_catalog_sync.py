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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_ai_model, crud_audit_log
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

# Stable identifier for the scheduled (non-user) sync in audit events.
SYSTEM_ACTOR_MODEL_CATALOG_SYNC = "model-catalog-sync"


@dataclass(frozen=True)
class CatalogSyncActor:
    """Who ran a catalog sync, for audit attribution.

    The audit_log schema already models system actions as ``user_id=None``
    (see :class:`preloop.models.models.audit_log.AuditLog`); this dataclass is
    the smallest honest layer on top: it pins the account, carries the
    optional user, and stamps ``actor_type``/``actor`` into the audit event
    details so a scheduled run is distinguishable from an operator run.
    """

    account_id: object
    user_id: Optional[object] = None
    actor_type: str = "user"  # "user" or "system"
    actor_id: str = ""  # user id, or a stable system identifier

    @classmethod
    def for_user(cls, user: User) -> "CatalogSyncActor":
        return cls(
            account_id=user.account_id,
            user_id=user.id,
            actor_type="user",
            actor_id=str(user.id),
        )

    @classmethod
    def system(cls, account_id) -> "CatalogSyncActor":
        return cls(
            account_id=account_id,
            user_id=None,
            actor_type="system",
            actor_id=SYSTEM_ACTOR_MODEL_CATALOG_SYNC,
        )


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


def _record_create_failure(
    result: ProviderCatalogSyncResult, *, identifier: str, note: str
) -> None:
    """Keep the first create failure on the provider result; keep syncing."""
    logger.warning(
        "Catalog sync could not create model %s for provider %s: %s",
        identifier,
        result.provider,
        note,
    )
    if result.error is None:
        result.error = "create"
        result.note = note


def _create_discovered_model(
    db: Session,
    *,
    result: ProviderCatalogSyncResult,
    account_id,
    seed: AIModel,
    provider_name: str,
    identifier: str,
    alias: str,
    seed_gateway_enabled: bool,
) -> bool:
    """Create one catalog row inside a savepoint.

    ``ValueError`` (deleted secret reference, alias collision) and
    ``IntegrityError`` must not 500 the request or roll back siblings.
    Returns True when the row was flushed into the outer transaction.
    """
    nested = db.begin_nested()
    try:
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
            account_id=account_id,
            commit=False,
        )
    except ValueError as exc:
        nested.rollback()
        _record_create_failure(result, identifier=identifier, note=str(exc))
        return False
    except IntegrityError:
        nested.rollback()
        _record_create_failure(
            result,
            identifier=identifier,
            note="could not persist a newly discovered model",
        )
        return False
    nested.commit()
    return True


async def sync_account_model_catalog(
    db: Session,
    *,
    actor: Optional[CatalogSyncActor] = None,
    user: Optional[User] = None,
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

    One audit event is emitted per run when anything was added (or would be
    added on a dry run); a run that changes nothing is logged only.

    Args:
        db: Active database session.
        actor: Attribution for the run. Defaults to the acting ``user`` when
            omitted; the scheduled job passes :meth:`CatalogSyncActor.system`.
        user: The operator running a manual sync. Also feeds the EE
            config-change audit plugin, which is user-centric; system runs
            record only the OSS audit event.
        provider: Optional provider filter, e.g. ``"anthropic"``.
        dry_run: When True, report what would be added without writing.
        request: Optional FastAPI request for audit IP/user-agent context.

    Returns:
        Per-provider results: discovery provenance, added aliases, and
        skip/error notes.
    """
    if actor is None:
        if user is None:
            raise ValueError("sync_account_model_catalog needs an actor or a user")
        actor = CatalogSyncActor.for_user(user)
    account_models = crud_ai_model.get_by_account(db=db, account_id=actor.account_id)
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
            actor=actor,
            provider_name=provider_name,
            provider_models=grouped[provider_name],
            dry_run=dry_run,
        )
        summary.providers.append(result)

    _audit_catalog_sync_run(
        db,
        actor=actor,
        user=user,
        summary=summary,
        request=request,
    )
    return summary


async def _sync_provider_catalog(
    db: Session,
    *,
    actor: CatalogSyncActor,
    provider_name: str,
    provider_models: List[AIModel],
    dry_run: bool,
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

    seed, secret = _resolve_seed_model(db, actor.account_id, provider_models)
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
        existing.add(identifier.lower())
        if dry_run:
            added_aliases.append(alias)
            continue
        created = _create_discovered_model(
            db,
            result=result,
            account_id=actor.account_id,
            seed=seed,
            provider_name=provider_name,
            identifier=identifier,
            alias=alias,
            seed_gateway_enabled=seed_gateway_enabled,
        )
        if created:
            added_aliases.append(alias)

    if not dry_run and added_aliases:
        db.commit()

    result.added = added_aliases
    return result


def _audit_catalog_sync_run(
    db: Session,
    *,
    actor: CatalogSyncActor,
    user: Optional[User],
    summary: CatalogSyncSummary,
    request=None,
) -> None:
    """Record one audit event summarizing a sync run's added models.

    A run that added nothing (and would add nothing on a dry run) is logged
    only, never audited: a scheduled no-op every interval would otherwise
    bury real events. System runs carry ``user_id=None`` plus
    ``actor_type``/``actor`` details; the user-centric EE config-change
    plugin is invoked only for operator runs.
    """
    provider_details = [
        {"provider": result.provider, "added": result.added}
        for result in summary.providers
        if result.added
    ]
    total_added = sum(len(entry["added"]) for entry in provider_details)
    if total_added == 0:
        logger.info(
            "Model catalog sync for account %s (%s) added nothing",
            actor.account_id,
            actor.actor_id or actor.actor_type,
        )
        return

    details = {
        "actor_type": actor.actor_type,
        "actor": actor.actor_id,
        "trigger": "scheduled" if actor.actor_type == "system" else "manual",
        "dry_run": summary.dry_run,
        "providers": provider_details,
        "total_added": total_added,
    }
    try:
        crud_audit_log.log_action(
            db,
            account_id=actor.account_id,
            user_id=actor.user_id,
            action="ai_model_catalog_sync",
            resource_type="ai_model",
            status="success",
            details=details,
        )
    except Exception:  # pragma: no cover - audit must not break the sync
        logger.debug("Audit log for model catalog sync failed", exc_info=True)
    if user is not None:
        log_config_change(
            db,
            user=user,
            config_type="ai_model",
            action="synced",
            new_value=details,
            request=request,
        )


async def sync_all_account_model_catalogs(db: Session) -> dict[str, int]:
    """Run the catalog sync for every account, as the scheduled system actor.

    Pages through all accounts via the CRUD layer and syncs each one exactly
    like the manual endpoint would, attributed to the
    ``model-catalog-sync`` system actor (``user_id=None`` in audit events).
    Principal-bound subscription-OAuth credentials stay hard-excluded by
    :func:`_resolve_seed_model`, identically to manual runs. Per-account
    failures are logged and skipped so one broken credential cannot stall
    the whole run.

    Returns:
        Mapping of account id to the number of models added for accounts
        where anything changed.
    """
    added_by_account: dict[str, int] = {}
    skip = 0
    page_size = 100
    while True:
        accounts = crud_account.get_multi(db, skip=skip, limit=page_size)
        if not accounts:
            break
        for account in accounts:
            try:
                summary = await sync_account_model_catalog(
                    db,
                    actor=CatalogSyncActor.system(account.id),
                )
            except Exception:
                logger.exception(
                    "Scheduled model catalog sync failed for account %s",
                    account.id,
                )
                continue
            total = sum(len(result.added) for result in summary.providers)
            if total:
                added_by_account[str(account.id)] = total
        if len(accounts) < page_size:
            break
        skip += page_size
    logger.info(
        "Scheduled model catalog sync finished: %d account(s) gained models",
        len(added_by_account),
    )
    return added_by_account
