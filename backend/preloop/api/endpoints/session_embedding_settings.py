"""Read and change one account's session embedding setting.

Two routes, and they take different permissions on purpose. Reading the
setting tells you what this account is doing with its own session text, which
is no more than the sessions permission already grants. Changing it (turning
embedding on, widening ``scope`` to ``full``, raising the daily cap) changes
how much text is posted to a provider and how much that costs per day, so it
takes the permission that already guards spending decisions rather than a new
one nobody has been granted yet.

The write side is what the console's opt in card saves. Turning embedding on
names a provider, a model and an endpoint, and that decision is still
validated in the CRUD layer where the host policy lives: this route only
routes the body to ``enable()``, ``disable()``, ``set_scope()`` and
``set_daily_cap()``, and turns their refusals into a 422 carrying the
refusal's own code.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.crud import (
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_embedding_setting import (
    SessionEmbeddingConfigError,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.session_embedding_setting import (
    SessionEmbeddingSetting,
    effective_scope,
)
from preloop.models.models.user import User
from preloop.schemas.session_embedding_setting import (
    SCOPE_HELP_TEXT,
    SessionEmbeddingCorpus,
    SessionEmbeddingScope,
    SessionEmbeddingSettingResponse,
    SessionEmbeddingSettingUpdate,
)
from preloop.services.session_embedding import (
    deployment_daily_cap_usd,
    embedding_enabled,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter()

#: Two segments after the collection on purpose. ``/runtime-sessions/{id}``
#: is a route that already exists, and a one segment literal next to it would
#: be read as a session id by whichever router FastAPI matched first.
SETTING_PATH = "/runtime-sessions/settings/embedding"


def _corpus(db: Session, setting: SessionEmbeddingSetting) -> SessionEmbeddingCorpus:
    """How far embedding has got, read the way search reads it.

    One aggregate statement over the account's own chunks, the same one
    search uses to explain a thin semantic answer, so the card and the
    search notice cannot disagree about the backlog.
    """
    coverage = crud_session_search_document.embedding_coverage(
        db,
        account_id=setting.account_id,
        embedding_model=setting.model_identity or "",
        source_kinds=setting.embedded_source_kinds,
    )
    return SessionEmbeddingCorpus(
        vectors=coverage.vectors,
        model_vectors=coverage.model_vectors,
        pending=coverage.pending,
        embedded_through=coverage.embedded_through,
    )


def _to_response(
    db: Session, setting: SessionEmbeddingSetting
) -> SessionEmbeddingSettingResponse:
    """Publish the stored row, including why a run may have stopped short."""
    return SessionEmbeddingSettingResponse(
        enabled=bool(setting.enabled),
        # The column is a string and the schema is a Literal. Writes refuse
        # unknown values, but a row from a newer build can still carry one.
        # Mirror the worker: report the default, never 500.
        scope=cast(SessionEmbeddingScope, effective_scope(setting.scope)),
        scope_help=SCOPE_HELP_TEXT,
        provider=setting.provider,
        model_identifier=setting.model_identifier,
        base_url=setting.base_url,
        dimensions=int(setting.dimensions),
        daily_cap_usd=setting.daily_cap_usd,
        deployment_daily_cap_usd=deployment_daily_cap_usd(),
        deployment_embedding_enabled=embedding_enabled(),
        degraded_reason=setting.degraded_reason,
        degraded_at=setting.degraded_at,
        corpus=_corpus(db, setting),
    )


def _refusal(code: str, message: str) -> HTTPException:
    """The house refusal shape, so the console can show the sentence."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "message": message},
    )


def _needs_enable(
    setting: SessionEmbeddingSetting,
    provider: str,
    model_identifier: Optional[str],
    base_url: Optional[str],
) -> bool:
    """Whether this save is an opt in rather than an edit of a live one.

    ``enable()`` stamps who opted in and clears the degraded marker, which is
    right for a fresh opt in or a change of endpoint and wrong for a save
    that only moves the cap: the user would lose the reason the worker
    stopped the moment they looked at it.
    """
    if not setting.enabled:
        return True
    return (
        setting.provider != provider
        or (setting.model_identifier or None) != (model_identifier or None)
        or (setting.base_url or None) != (base_url or None)
    )


def _apply_update(
    db: Session,
    *,
    account_id: Any,
    user_id: Any,
    payload: SessionEmbeddingSettingUpdate,
) -> SessionEmbeddingSetting:
    """Route one save to the CRUD operations that own each decision.

    Nothing is committed here; the caller commits once, so a refusal half
    way through leaves the row as it was.
    """
    fields = payload.model_fields_set
    setting = crud_session_embedding_setting.get_or_create(db, account_id=account_id)
    target_enabled = (
        bool(payload.enabled) if payload.given("enabled") else bool(setting.enabled)
    )
    if payload.names_provider and not target_enabled:
        raise SessionEmbeddingConfigError(
            "provider_requires_enable",
            "a provider and model are named when turning embedding on",
        )
    # daily_cap_usd is the one field where an explicit null means something:
    # clear the account cap back to the deployment default.
    cap = payload.daily_cap_usd if "daily_cap_usd" in fields else setting.daily_cap_usd

    if payload.given("enabled") and not target_enabled:
        crud_session_embedding_setting.disable(db, account_id=account_id)
    elif target_enabled:
        provider = payload.provider if payload.given("provider") else setting.provider
        model_identifier = (
            payload.model_identifier
            if payload.given("model_identifier")
            else setting.model_identifier
        )
        base_url = payload.base_url if payload.given("base_url") else setting.base_url
        if _needs_enable(setting, provider, model_identifier, base_url):
            crud_session_embedding_setting.enable(
                db,
                account_id=account_id,
                provider=provider,
                model_identifier=model_identifier or "",
                base_url=base_url,
                dimensions=int(setting.dimensions),
                daily_cap_usd=cap,
                user_id=user_id,
            )

    if payload.scope is not None:
        crud_session_embedding_setting.set_scope(
            db, account_id=account_id, scope=payload.scope
        )
    if "daily_cap_usd" in fields:
        crud_session_embedding_setting.set_daily_cap(
            db, account_id=account_id, daily_cap_usd=cap
        )
    return setting


@router.get(
    SETTING_PATH,
    response_model=SessionEmbeddingSettingResponse,
    summary="Read this account's session embedding setting",
)
@require_permission("view_runtime_sessions")
def read_session_embedding_setting(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionEmbeddingSettingResponse:
    """Return the setting, creating the shipped default row if there is none.

    An account that never opted in reads ``enabled = false`` and
    ``scope = summaries_only``, which is what it would get if it opted in
    without saying anything else.
    """
    # Plain def so FastAPI dispatches on the threadpool. An async def that
    # still takes get_db_session would grow the async/sync-session ratchet.
    setting = crud_session_embedding_setting.get_or_create(
        db, account_id=account.id, commit=True
    )
    return _to_response(db, setting)


@router.put(
    SETTING_PATH,
    response_model=SessionEmbeddingSettingResponse,
    summary="Opt in to session embedding, or change its scope and cap",
)
@require_permission("manage_budgets")
def update_session_embedding_setting(
    payload: SessionEmbeddingSettingUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionEmbeddingSettingResponse:
    """Change what this account embeds. Existing vectors are never touched.

    Narrowing to ``summaries_only`` stops new transcript chunks being
    embedded from the next worker pass; widening to ``full`` gives the
    untouched backlog back to the worker, still under the daily cap.
    Turning embedding off keeps the provider details, so turning it back on
    needs only ``{"enabled": true}``.
    """
    # Plain def so FastAPI dispatches on the threadpool. Changing the
    # setting is a spending decision, not a console burst path, and must
    # not grow the async/sync-session route ratchet.
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    try:
        setting = _apply_update(
            db, account_id=account.id, user_id=current_user.id, payload=payload
        )
    except SessionEmbeddingConfigError as exc:
        db.rollback()
        raise _refusal(exc.code, str(exc)) from exc
    db.commit()
    db.refresh(setting)
    return _to_response(db, setting)
