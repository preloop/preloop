"""Read and change one account's session embedding setting.

Two routes, and they take different permissions on purpose. Reading the
setting tells you what this account is doing with its own session text, which
is no more than the sessions permission already grants. Changing ``scope``
from ``summaries_only`` to ``full`` changes how much text is posted to a
provider and how much that costs per day, so it takes the permission that
already guards spending decisions rather than a new one nobody has been
granted yet.

The write side carries ``scope`` alone. Enabling embedding names a provider,
a model and an endpoint, and that decision is validated in the CRUD layer
where the host policy lives; it is not a field on a preferences body.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import crud_session_embedding_setting
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.session_embedding_setting import SessionEmbeddingSetting
from preloop.models.models.user import User
from preloop.schemas.session_embedding_setting import (
    SCOPE_HELP_TEXT,
    SessionEmbeddingScope,
    SessionEmbeddingSettingResponse,
    SessionEmbeddingSettingUpdate,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter()

#: Two segments after the collection on purpose. ``/runtime-sessions/{id}``
#: is a route that already exists, and a one segment literal next to it would
#: be read as a session id by whichever router FastAPI matched first.
SETTING_PATH = "/runtime-sessions/settings/embedding"


def _to_response(setting: SessionEmbeddingSetting) -> SessionEmbeddingSettingResponse:
    """Publish the stored row, including why a run may have stopped short."""
    return SessionEmbeddingSettingResponse(
        enabled=bool(setting.enabled),
        # The column is a string and the schema is a Literal. Every write
        # path validates before it stores, so the cast states that rather
        # than re-checking it here.
        scope=cast(SessionEmbeddingScope, setting.scope),
        scope_help=SCOPE_HELP_TEXT,
        provider=setting.provider,
        model_identifier=setting.model_identifier,
        base_url=setting.base_url,
        dimensions=int(setting.dimensions),
        daily_cap_usd=setting.daily_cap_usd,
        degraded_reason=setting.degraded_reason,
        degraded_at=setting.degraded_at,
    )


@router.get(
    SETTING_PATH,
    response_model=SessionEmbeddingSettingResponse,
    summary="Read this account's session embedding setting",
)
@require_permission("view_runtime_sessions")
async def read_session_embedding_setting(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionEmbeddingSettingResponse:
    """Return the setting, creating the shipped default row if there is none.

    An account that never opted in reads ``enabled = false`` and
    ``scope = summaries_only``, which is what it would get if it opted in
    without saying anything else.
    """

    def _read() -> SessionEmbeddingSettingResponse:
        setting = crud_session_embedding_setting.get_or_create(
            db, account_id=account.id, commit=True
        )
        return _to_response(setting)

    return await run_db_off_loop(_read)


@router.put(
    SETTING_PATH,
    response_model=SessionEmbeddingSettingResponse,
    summary="Set how much of a session this account embeds",
)
@require_permission("manage_budgets")
async def update_session_embedding_setting(
    payload: SessionEmbeddingSettingUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionEmbeddingSettingResponse:
    """Change the scope. Existing vectors are never touched by this call.

    Narrowing to ``summaries_only`` stops new transcript chunks being
    embedded from the next worker pass; widening to ``full`` gives the
    untouched backlog back to the worker, still under the daily cap.
    """
    ensure_permission_in_oss(db, current_user, "manage_budgets")

    def _write() -> SessionEmbeddingSettingResponse:
        setting = crud_session_embedding_setting.set_scope(
            db, account_id=account.id, scope=payload.scope, commit=True
        )
        return _to_response(setting)

    return await run_db_off_loop(_write)
