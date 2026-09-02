import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional, Tuple

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    status,
    Query,
    Request,
    Response,
)
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models.crud import crud_account
from preloop.schemas.ai_model import (
    AIModelCatalogSyncProviderResult,
    AIModelCatalogSyncRequest,
    AIModelCatalogSyncResponse,
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
from preloop.services.ai_model_provider import (
    ERROR_SUBSCRIPTION_OAUTH,
    ProviderAuthError,
    ProviderValidationError,
    get_available_models_for_provider,
)
from preloop.services.ai_model_catalog_sync import sync_account_model_catalog

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
    "/ai-models/sync",
    response_model=AIModelCatalogSyncResponse,
    summary="Sync Provider Model Catalogs",
    tags=["AI Models"],
)
@require_permission("create_ai_models")
async def sync_ai_model_catalog(
    request: Request,
    request_in: Optional[AIModelCatalogSyncRequest] = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelCatalogSyncResponse:
    """Discover newly released provider models and add them to the catalog.

    Runs the existing live provider discovery against credentials the account
    already stores (the same discovery the console model-add flow uses) and
    creates one AI model per newly discovered identifier via the CRUD layer.
    New rows share the seed model's credential secret and inherit its gateway
    exposure, so authorization semantics are unchanged: API-key models stay
    account-wide, and principal-bound subscription-OAuth models (which cannot
    authenticate server-side discovery) are never created or widened here.

    Backing service: ``preloop.services.ai_model_catalog_sync``. Every added
    model is recorded in the audit trail. Use ``dry_run`` to preview.
    """
    summary = await sync_account_model_catalog(
        db,
        user=current_user,
        provider=request_in.provider if request_in else None,
        dry_run=bool(request_in.dry_run) if request_in else False,
        request=request,
    )
    return AIModelCatalogSyncResponse(
        providers=[
            AIModelCatalogSyncProviderResult(
                provider=result.provider,
                source=result.source,
                error=result.error,
                discovered=result.discovered,
                added=result.added,
                skipped_existing=result.skipped_existing,
                note=result.note,
            )
            for result in summary.providers
        ],
        dry_run=summary.dry_run,
    )


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
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AvailableModelsResponse:
    """
    Fetch available models from the specified AI provider, with provenance.

    Every provider lists live when a key (and endpoint where applicable) is
    present; the response reports ``source`` ("live" or "fallback") and a
    short safe ``error`` reason when a live attempt failed or credentials
    were missing. The reason comes from a fixed vocabulary and never contains
    raw provider error text, endpoint URLs, or key material.

    The provider API key travels in the request BODY, never the query string:
    as a query parameter it was written to access logs in plaintext.

    Edit-mode refresh should send ``ai_model_id`` instead of the stored key.
    The server decrypts the stored secret via CRUD. A typed ``api_key`` in
    the body always wins. Stored secrets are never returned to the client.
    """
    (
        api_key,
        api_endpoint,
        aws_auth,
        model_kind,
        stored_subscription_oauth,
    ) = _resolve_listing_inputs(
        provider=provider,
        request_in=request_in,
        db=db,
        current_user=current_user,
    )
    if stored_subscription_oauth and not api_key:
        # The stored credential is a principal-bound subscription-OAuth bundle
        # (e.g. Claude Code). HARD CONSTRAINT: the server never initiates its
        # own provider API calls with such a token; Anthropic fingerprints
        # Claude Code OAuth traffic and can invalidate the subscription (see
        # the error-code-1010 note in secret_service.py). Answer from the
        # account's own catalog instead of returning an API-key auth error.
        return AvailableModelsResponse(
            models=_account_catalog_identifiers(
                db=db, current_user=current_user, provider=provider
            ),
            source="fallback",
            error=ERROR_SUBSCRIPTION_OAUTH,
        )
    return await _fetch_provider_models(
        provider=provider,
        api_key=api_key,
        model_kind=model_kind,
        api_endpoint=api_endpoint,
        aws_auth=aws_auth,
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


def _aws_auth_from_stored_bedrock_secret(
    secret_value: str,
    ai_model: AIModel,
) -> Optional[Dict[str, str]]:
    """Parse a stored Bedrock JSON blob plus routing region into aws_auth.

    The stored secret is the same JSON shape the add-model modal writes
    (``aws_access_key_id``, ``aws_secret_access_key``, optional session
    token). Region lives on ``meta_data.provider_runtime.region``.
    """
    try:
        payload = json.loads(secret_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    auth: Dict[str, str] = {}
    for key in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_region_name",
    ):
        value = payload.get(key)
        if value:
            auth[key] = str(value).strip()
    meta = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    runtime_raw = meta.get("provider_runtime")
    runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
    region = runtime.get("region") if isinstance(runtime, dict) else None
    if isinstance(region, str) and region.strip() and "aws_region_name" not in auth:
        auth["aws_region_name"] = region.strip()
    if not auth.get("aws_access_key_id") or not auth.get("aws_secret_access_key"):
        return None
    return auth


def _resolve_listing_inputs(
    *,
    provider: str,
    request_in: Optional[AvailableModelsRequest],
    db: Optional[Session],
    current_user: Optional[User],
) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[dict],
    Literal["llm", "stt", "tts"],
    bool,
]:
    """Typed credentials win; otherwise decrypt the stored model secret.

    The stored plaintext is used only for the live list call and is never
    copied into the response.

    The final tuple element reports whether the stored model carries a
    principal-bound subscription-OAuth credential (Claude Code / Codex). Such
    secrets are never decrypted here: the caller must not contact the
    provider with them at all, so there is nothing to resolve.

    A stored secret is only ever used for the provider it was stored for. The
    edit form leaves the provider dropdown enabled, so without that check a
    caller could pair one model's ``ai_model_id`` with a different ``provider``
    plus an attacker-chosen ``api_endpoint`` and have the server forward the
    decrypted key there. ``validate_discovery_endpoint`` blocks private hosts
    but not public ones, so the mismatch is rejected before decryption.
    """
    typed_key = (request_in.api_key or "").strip() if request_in else ""
    typed_endpoint = (request_in.api_endpoint or "").strip() if request_in else ""
    typed_aws = _aws_auth_from_request(request_in)
    model_kind: Literal["llm", "stt", "tts"] = (
        request_in.model_kind if request_in else "llm"
    )

    stored_key: Optional[str] = None
    stored_endpoint: Optional[str] = None
    stored_aws: Optional[Dict[str, str]] = None
    stored_subscription_oauth = False
    model_id = request_in.ai_model_id if request_in else None
    if model_id is not None:
        if (
            db is None
            or current_user is None
            or not getattr(current_user, "account_id", None)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to list with a stored model",
            )
        db_model = crud_ai_model.get_for_account(
            db, id=model_id, account_id=current_user.account_id
        )
        if db_model is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="AI Model not found"
            )
        if (db_model.provider_name or "").strip().lower() != (
            provider or ""
        ).strip().lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Stored model provider does not match the requested provider",
            )
        if bool(getattr(db_model, "is_principal_bound_oauth", False)):
            # Never decrypt or use a principal-bound subscription-OAuth token
            # for server-initiated listing; the caller answers from the
            # catalog instead (see list_provider_available_models).
            return (
                (typed_key or None),
                typed_endpoint or (db_model.api_endpoint or "").strip() or None,
                typed_aws,
                model_kind,
                True,
            )
        try:
            stored_key = crud_ai_model.resolve_listing_secret(db_model)
        except ValueError:
            # Every expected failure below resolve_listing_secret normalizes to
            # ValueError: decrypt_value re-raises InvalidToken, the vault
            # backend re-raises transport/lookup errors, credential payload
            # parsing raises on bad JSON, and CredentialRefreshError subclasses
            # ValueError. Anything else is a real bug and must stay loud rather
            # than degrade to "missing_key".
            logger.warning(
                "Failed to decrypt stored listing credentials for model %s",
                model_id,
            )
            stored_key = None
        stored_endpoint = (db_model.api_endpoint or "").strip() or None
        if (db_model.provider_name or "").lower() == "bedrock" and stored_key:
            stored_aws = _aws_auth_from_stored_bedrock_secret(stored_key, db_model)
            stored_key = None

    api_key = typed_key or stored_key
    api_endpoint = typed_endpoint or stored_endpoint
    aws_auth = typed_aws or stored_aws
    return api_key, api_endpoint, aws_auth, model_kind, stored_subscription_oauth


def _account_catalog_identifiers(
    *,
    db: Session,
    current_user: User,
    provider: str,
) -> List[str]:
    """The account's own model identifiers for one provider.

    Used as the honest picker fallback for subscription-OAuth credentials:
    there is no bundled provider catalog and no server-initiated listing, so
    what the account already knows (from onboarding imports, `models sync`,
    and gateway traffic-observed auto-registration) is the curated list.

    Sorted reverse-lexicographic. That is roughly newest-first for
    date-suffixed ids, but not a chronological sort: ``model-5-20260415``
    sorts ahead of ``model-5-1-20260901``. Display order only.
    """
    provider_name = (provider or "").strip().lower()
    identifiers = {
        (model.model_identifier or "").strip()
        for model in crud_ai_model.get_by_account(
            db=db, account_id=current_user.account_id
        )
        if (model.provider_name or "").strip().lower() == provider_name
        and (model.model_identifier or "").strip()
    }
    return sorted(identifiers, reverse=True)


def _aws_auth_from_request(
    request_in: Optional[AvailableModelsRequest],
) -> Optional[dict]:
    """Collect AWS credential fields into a service-layer mapping, or None.

    Only present fields are forwarded, so boto3's default credential chain
    stays in play for the bedrock provider when the user supplied nothing.
    """
    if request_in is None:
        return None
    auth = {
        key: getattr(request_in, key)
        for key in (
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_region_name",
        )
        if getattr(request_in, key) is not None
    }
    return auth or None


async def _fetch_provider_models(
    *,
    provider: str,
    api_key: Optional[str],
    model_kind: Literal["llm", "stt", "tts"],
    api_endpoint: Optional[str],
    aws_auth: Optional[dict] = None,
) -> AvailableModelsResponse:
    """Shared body of the GET and POST available-models endpoints."""
    try:
        result = await get_available_models_for_provider(
            provider,
            api_key,
            model_kind,
            api_endpoint,
            aws_auth=aws_auth,
        )
        return AvailableModelsResponse(
            models=result.models,
            source=result.source,
            error=result.error,
        )
    except ProviderAuthError as e:
        # The provider rejected the caller's API key. The message is our own
        # fixed text, never the key.
        logger.warning("Cannot list models for provider %s: %s", provider, e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    except ProviderValidationError as e:
        # The request was invalid before any provider was contacted (bad
        # model_kind, rejected or SSRF-blocked endpoint).
        logger.warning("Cannot list models for provider %s: %s", provider, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except ValueError as e:
        # An unexpected internal ValueError. Treat it as a bad request:
        # calling it "unauthorized" would mislabel non-auth failures.
        logger.warning("Cannot list models for provider %s: %s", provider, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
