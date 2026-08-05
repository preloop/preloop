import logging
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    status,
    Query,
    Response,
)
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models.crud import crud_account
from preloop.schemas.ai_model import (
    AIModelCreate,
    AIModelCredentialExportResponse,
    AIModelGatewayUsageSummaryResponse,
    AIModelRead,
    AIModelUpdate,
    AvailableModelsRequest,
    AvailableModelsResponse,
)
from preloop.services.secret_service import (
    PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES,
    CredentialRefreshError,
    get_secret_service,
)
from preloop.models.crud import crud_ai_model
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.models.ai_model import AIModel
from preloop.schemas.gateway_usage import (
    AccountGatewayUsageSearchResponse,
    AccountRuntimeSessionListResponse,
)
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService
from preloop.utils.permissions import require_permission
from preloop.services.ai_model_provider import get_available_models_for_provider

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_account_ai_model(
    *,
    db: Session,
    model_id: uuid.UUID,
    current_user: User,
) -> AIModel:
    """Return an account-owned AI model or raise 404."""
    db_model = crud_ai_model.get(db=db, id=model_id)
    if not db_model or db_model.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="AI Model not found"
        )
    return db_model


def _get_current_account(*, db: Session, current_user: User) -> Account:
    """Return the current user's account."""
    account = crud_account.get(db=db, id=current_user.account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )
    return account


@router.post(
    "/ai-models",
    response_model=AIModelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create AI Model",
    tags=["AI Models"],
)
@require_permission("create_ai_models")
def create_ai_model(
    ai_model_in: AIModelCreate,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModel:
    """Create a new AI Model for the authenticated user's account."""
    try:
        created_model = crud_ai_model.create_with_account(
            db=db,
            obj_in=ai_model_in.dict(),
            account_id=current_user.account_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return created_model


@router.get(
    "/ai-models",
    response_model=List[AIModelRead],
    summary="List AI Models",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def list_ai_models(
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> List[AIModelRead]:
    """List all AI Models associated with the authenticated user's account."""
    models = crud_ai_model.get_by_account(db=db, account_id=current_user.account_id)
    return models


@router.get(
    "/ai-models/{model_id}",
    response_model=AIModelRead,
    summary="Get AI Model by ID",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelRead:
    """Retrieve a specific AI Model by its ID."""
    return _get_account_ai_model(db=db, model_id=model_id, current_user=current_user)


@router.get(
    "/ai-models/{model_id}/summary",
    response_model=AIModelGatewayUsageSummaryResponse,
    summary="Get AI Model Usage Summary",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model_usage_summary(
    model_id: uuid.UUID,
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelGatewayUsageSummaryResponse:
    """Return model-scoped gateway usage totals for one AI model."""
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    return ModelGatewayUsageService(db).get_ai_model_summary(
        ai_model=db_model,
        start_date=start_date,
        end_date=end_date,
    )


@router.get(
    "/ai-models/{model_id}/runtime-sessions",
    response_model=AccountRuntimeSessionListResponse,
    summary="List AI Model Runtime Sessions",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def list_ai_model_runtime_sessions(
    model_id: uuid.UUID,
    query: Optional[str] = Query(None, min_length=1),
    session_source_type: Optional[str] = Query(None),
    status: str = Query("all", pattern="^(all|active|ended)$"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AccountRuntimeSessionListResponse:
    """List runtime sessions that used one durable AI model."""
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    account = _get_current_account(db=db, current_user=current_user)
    return RuntimeSessionExplorerService(db).list_account_sessions(
        account=account,
        query=query,
        ai_model_id=str(db_model.id),
        session_source_type=session_source_type,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/ai-models/{model_id}/interactions",
    response_model=AccountGatewayUsageSearchResponse,
    summary="List AI Model Interactions",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def list_ai_model_interactions(
    model_id: uuid.UUID,
    query: Optional[str] = Query(None, min_length=1),
    runtime_session_id: Optional[str] = Query(None),
    session_source_type: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AccountGatewayUsageSearchResponse:
    """List indexed gateway interactions scoped to one AI model."""
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    account = _get_current_account(db=db, current_user=current_user)
    return ModelGatewayUsageService(db).search_account_interactions(
        account=account,
        query=query,
        start_date=start_date,
        end_date=end_date,
        ai_model_id=str(db_model.id),
        runtime_session_id=runtime_session_id,
        session_source_type=session_source_type,
        limit=limit,
        offset=offset,
    )


@router.put(
    "/ai-models/{model_id}",
    response_model=AIModelRead,
    summary="Update AI Model",
    tags=["AI Models"],
)
@require_permission("edit_ai_models")
def update_ai_model(
    model_id: uuid.UUID,
    ai_model_in: AIModelUpdate,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelRead:
    """Update an existing AI Model by its ID."""
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )

    try:
        updated_model = crud_ai_model.update(
            db=db,
            db_obj=db_model,
            obj_in=ai_model_in.dict(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return updated_model


@router.delete(
    "/ai-models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete AI Model",
    tags=["AI Models"],
)
@require_permission("delete_ai_models")
def delete_ai_model(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """Delete an AI Model by its ID."""
    _get_account_ai_model(db=db, model_id=model_id, current_user=current_user)

    crud_ai_model.remove(db=db, id=model_id)

    # No content returned for HTTP 204
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/ai-models/{model_id}/credentials/export",
    response_model=AIModelCredentialExportResponse,
    summary="Export Subscription OAuth Credential",
    tags=["AI Models"],
)
@require_permission("edit_ai_models")
def export_ai_model_credentials(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelCredentialExportResponse:
    """Export the live subscription-OAuth bundle for an account AI model.

    Only principal-bound subscription OAuth credentials (Claude Code, Codex)
    are exportable: their provider refresh tokens are single-use and rotate on
    every server-side refresh, so once imported the Preloop copy is the only
    live lineage. The CLI calls this at offboard time to restore the agent's
    local login before the Preloop-held credential is removed. API-key
    credentials are never exportable.
    """
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    service = get_secret_service()
    try:
        resolved = service.resolve_ai_model_credentials(
            db_model, db=db, allow_refresh=True
        )
    except CredentialRefreshError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Credential refresh failed: {exc.safe_summary()}",
        )
    if (
        resolved is None
        or resolved.credential_type not in PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only subscription OAuth credentials can be exported",
        )
    payload = resolved.payload or {}
    access = str(payload.get("access") or "").strip()
    if not access:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored credential has no access token",
        )
    expires: Optional[int] = None
    expires_raw = payload.get("expires")
    if isinstance(expires_raw, (int, float)) and expires_raw > 0:
        expires = int(expires_raw)
    logger.info(
        "Exported subscription OAuth credential: model=%s type=%s account=%s user=%s",
        model_id,
        resolved.credential_type,
        current_user.account_id,
        current_user.id,
    )
    refresh = str(payload.get("refresh") or "").strip() or None
    account_id = str(payload.get("account_id") or "").strip() or None
    return AIModelCredentialExportResponse(
        credential_type=resolved.credential_type,
        access=access,
        refresh=refresh,
        expires=expires,
        account_id=account_id,
    )


@router.post(
    "/ai-models/providers/{provider}/available-models",
    response_model=AvailableModelsResponse,
    summary="Get Available Models for Provider",
    tags=["AI Models"],
)
async def list_provider_available_models(
    provider: str,
    request_in: Optional[AvailableModelsRequest] = None,
) -> AvailableModelsResponse:
    """
    Fetch available models from the specified AI provider, with provenance.

    Every provider attempts a live listing when a key (and endpoint where
    applicable) is present; the response reports ``source`` ("live" or
    "fallback") and a short safe ``error`` reason when a live attempt failed.
    The reason comes from a fixed vocabulary and never contains raw provider
    error text, endpoint URLs, or key material.

    The provider API key travels in the request BODY, never the query string:
    as a query parameter it was written to access logs in plaintext.
    """
    return await _fetch_provider_models(
        provider=provider,
        api_key=request_in.api_key if request_in else None,
        model_kind=request_in.model_kind if request_in else "llm",
        api_endpoint=request_in.api_endpoint if request_in else None,
    )


@router.get(
    "/ai-models/providers/{provider}/available-models",
    response_model=List[str],
    summary="Get Available Models for Provider (deprecated)",
    tags=["AI Models"],
    deprecated=True,
)
async def get_provider_available_models(
    provider: str,
    x_provider_api_key: Optional[str] = Header(
        None,
        alias="X-Provider-Api-Key",
        description="Provider API key. Headers are not written to access logs.",
    ),
    api_endpoint: Optional[str] = Query(
        None,
        description=(
            "Base URL of an OpenAI-compatible endpoint, required for the "
            "openai-compatible and custom providers."
        ),
    ),
    model_kind: Literal["llm", "stt", "tts"] = Query(
        "llm",
        pattern="^(llm|stt|tts)$",
        description="Model service kind to fetch",
    ),
) -> List[str]:
    """
    Deprecated GET form, kept for clients that have not moved to POST yet.

    Returns a BARE LIST of model ids, unlike the POST form, which reports
    provenance as ``{models, source, error}``. The bare-list shape is kept
    here on purpose so unknown external callers of the deprecated route do
    not break; new clients should use POST and read the provenance.

    The api_key query parameter this endpoint used to accept has been REMOVED,
    not merely deprecated: it wrote live provider keys into access logs in
    plaintext. Pass the key in the X-Provider-Api-Key header, or use the POST
    form. A key sent as a query parameter is ignored.
    """
    result = await _fetch_provider_models(
        provider=provider,
        api_key=x_provider_api_key,
        model_kind=model_kind,
        api_endpoint=api_endpoint,
    )
    return result.models


async def _fetch_provider_models(
    *,
    provider: str,
    api_key: Optional[str],
    model_kind: Literal["llm", "stt", "tts"],
    api_endpoint: Optional[str],
) -> AvailableModelsResponse:
    """Shared body of the GET and POST available-models endpoints."""
    try:
        result = await get_available_models_for_provider(
            provider, api_key, model_kind, api_endpoint
        )
        return AvailableModelsResponse(
            models=result.models,
            source=result.source,
            error=result.error,
        )
    except ValueError as e:
        # ValueError is raised for authentication and validation errors. The
        # message is provider text, never the key.
        logger.warning("Cannot list models for provider %s: %s", provider, e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    except Exception as e:
        logger.error(
            "Failed to fetch models for provider %s: %s", provider, type(e).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch available models. Check server logs for details.",
        )
