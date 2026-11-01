import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

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
from preloop.schemas.resource_share import SharedResourceRead
from preloop.schemas.ai_model import (
    AIModelCatalogSyncProviderResult,
    AIModelCatalogSyncRequest,
    AIModelCatalogSyncResponse,
    AIModelCreate,
    AIModelCredentialExportResponse,
    AIModelCredentialMarkerResponse,
    AIModelGatewayUsageSummaryResponse,
    AIModelOverviewItem,
    AIModelOwnerFlowUsage,
    AIModelOwnerFlowUseRead,
    AIModelOwnerFlowUseUpdate,
    AIModelRead,
    AIModelsOverviewResponse,
    AIModelUpdate,
    AIModelUsedByResponse,
    AvailableModelsRequest,
    AvailableModelsResponse,
)
from preloop.services.secret_service import (
    PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES,
    CredentialRefreshError,
    get_secret_service,
)
from preloop.models.crud import crud_ai_model, crud_api_usage, crud_runtime_session
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.models.ai_model import AIModel
from preloop.schemas.gateway_usage import (
    AccountGatewayUsageSearchResponse,
    AccountRuntimeSessionListResponse,
    GatewayTokenUsage,
)
from preloop.schemas.ai_model_pricing import (
    AIModelPriceQuote,
    AIModelPricingResponse,
)
from preloop.services.ai_model_pricing import (
    PriceFetchUnavailableError,
    PriceFetchUnsupportedError,
    fetch_provider_pricing,
    get_effective_pricing,
    get_effective_pricing_bulk,
)
from preloop.services.model_gateway_usage import (
    ModelGatewayUsageService,
    _alias_failures,
    model_detail_window,
    normalize_usage_period,
)
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from preloop.services.hosted_spend_guard import hosted_model_selectable
from preloop.services.ai_model_provider import (
    ERROR_SUBSCRIPTION_OAUTH,
    ProviderAuthError,
    ProviderValidationError,
    get_available_models_for_provider,
)
from preloop.services.ai_model_catalog_sync import sync_account_model_catalog

from preloop.services.built_in_models import (
    built_in_models_enabled,
    is_available_built_in_model,
    public_built_in_model,
)
from preloop.api.heavy_routes import heavy_route
from preloop.api.loop_safety import run_db_off_loop

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_account_ai_model(
    *,
    db: Session,
    model_id: uuid.UUID,
    current_user: User,
    view: bool = False,
) -> AIModel:
    """Return an account-owned model; gate read-only credential access too."""
    db_model = crud_ai_model.get(db=db, id=model_id)
    if not db_model or db_model.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="AI Model not found"
        )
    if view:
        from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable

        if not filter_viewable(db, current_user, VISIBLE_AI_MODEL, [db_model]):
            raise HTTPException(404, "AI Model not found")
    return db_model


def _get_readable_ai_model(
    *, db: Session, model_id: uuid.UUID, current_user: User
) -> AIModel:
    """Permit built-in reads while keeping all mutations account-owned."""
    model = crud_ai_model.get(db=db, id=model_id)
    if model and (
        model.account_id == current_user.account_id
        or (
            built_in_models_enabled()
            and is_available_built_in_model(model)
            and (
                hosted_model_selectable(model)
                or getattr(current_user, "is_superuser", False) is True
            )
        )
    ):
        from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable

        if model.account_id is not None and not filter_viewable(
            db, current_user, VISIBLE_AI_MODEL, [model]
        ):
            raise HTTPException(404, "AI Model not found")
        return model
    raise HTTPException(404, "AI Model not found")


def _gateway_alias(ai_model: AIModel) -> Optional[str]:
    """Return the gateway alias clients call this model by, if configured."""
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    gateway = meta_data.get("gateway")
    if isinstance(gateway, dict):
        alias = gateway.get("model_alias")
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
    return None


#: Upper bound on ``failed_since`` pairs. One pair per model on the page is
#: the shape the console sends, and each pair is one more OR branch in the
#: aggregate, so an unbounded list is an unbounded query.
MAX_FAILED_SINCE_PAIRS = 200


def _parse_failed_since(pairs: Optional[List[str]]) -> Dict[str, datetime]:
    """Parse ``<ai_model_id>:<timestamp>`` pairs into a mapping.

    The id is a UUID and carries no colon, so the pair splits on the first
    one and the rest is the timestamp (which has several).

    Args:
        pairs: Raw query values, or None.

    Returns:
        Mapping of model id to the moment its failures should be counted from.

    Raises:
        HTTPException: 422 when a pair is malformed or there are too many.
    """
    if not pairs:
        return {}
    if len(pairs) > MAX_FAILED_SINCE_PAIRS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"At most {MAX_FAILED_SINCE_PAIRS} failed_since pairs",
        )
    parsed: Dict[str, datetime] = {}
    for pair in pairs:
        model_id, separator, raw_moment = pair.partition(":")
        if not separator or not model_id.strip() or not raw_moment.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="failed_since must be '<ai_model_id>:<timestamp>'",
            )
        try:
            moment = datetime.fromisoformat(raw_moment.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"failed_since timestamp is not ISO-8601: {raw_moment}",
            ) from exc
        if moment.tzinfo is None:
            # Same reading as the window bounds: a naive timestamp is UTC.
            moment = moment.replace(tzinfo=timezone.utc)
        parsed[model_id.strip()] = moment
    return parsed


def _collapse_usage_by_model(usage_rows: List[Dict]) -> Dict[str, Dict]:
    """Sum the per-alias usage groups into one total per model.

    ``get_gateway_usage_by_model`` groups by (model, alias, provider), so a
    model renamed at the gateway comes back as several rows. The Models page
    shows one row per configured model, so the aliases are added together.

    Args:
        usage_rows: Grouped rows as returned by the usage CRUD.

    Returns:
        Mapping of model id to summed counters, with the latest
        ``last_request_at`` across that model's aliases, the latest
        ``last_failure_at`` and the alias that failure was recorded under,
        plus the raw per-alias rows ``_alias_failures`` folds.
    """
    totals: Dict[str, Dict] = {}
    for row in usage_rows:
        model_id = row.get("ai_model_id")
        if not model_id:
            continue
        total = totals.setdefault(
            model_id,
            {
                "request_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
                "unpriced_request_count": 0,
                "failed_request_count": 0,
                "failed_request_count_since": 0,
                "last_request_at": None,
                "last_failure_at": None,
                "last_failure_alias": None,
                "failure_groups": [],
            },
        )
        for key in (
            "request_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
            "unpriced_request_count",
            "failed_request_count",
            "failed_request_count_since",
        ):
            total[key] += row.get(key) or 0
        last_request_at = row.get("last_request_at")
        if last_request_at is not None and (
            total["last_request_at"] is None
            or last_request_at > total["last_request_at"]
        ):
            total["last_request_at"] = last_request_at
        last_failure_at = row.get("last_failure_at")
        if last_failure_at is not None and (
            total["last_failure_at"] is None
            or last_failure_at > total["last_failure_at"]
        ):
            # The alias travels with the timestamp: the console groups gateway
            # failures by the alias the request carried, so a model renamed at
            # the gateway is named on the console the way the failing calls
            # named it, not the way it is configured today.
            total["last_failure_at"] = last_failure_at
            total["last_failure_alias"] = row.get("model_alias") or row.get(
                "provider_name"
            )
        # Same groups the detail summary folds: one inbox key per alias.
        total["failure_groups"].append(row)
    return totals


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
    ensure_permission_in_oss(db, current_user, "create_ai_models")
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
    response_model=List[SharedResourceRead | AIModelRead],
    summary="List AI Models",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def list_ai_models(
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
    default_only: Annotated[
        bool,
        Query(
            description=(
                "Only the models marked default. Use this when a caller only "
                "needs to know whether a default model exists, rather than "
                "reading the whole catalog."
            ),
        ),
    ] = False,
) -> List[SharedResourceRead | AIModelRead]:
    """List all AI Models associated with the authenticated user's account."""
    from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable

    models = crud_ai_model.get_selectable_for_account(
        db=db,
        account_id=current_user.account_id,
        include_built_in=built_in_models_enabled(),
    )
    from preloop.models.crud.resource_share import crud_resource_share

    shared = crud_resource_share.public_list(
        db, account_id=current_user.account_id, resource_type="ai_model"
    )
    own_models = [model for model in models if model.account_id is not None]
    built_in = [model for model in models if model.account_id is None]
    account_default_kinds = crud_ai_model.configured_account_default_kinds(own_models)
    visible = [
        *built_in,
        *filter_viewable(db, current_user, VISIBLE_AI_MODEL, [*own_models, *shared]),
    ]
    priced = _apply_tariff_visibility(
        visible, is_admin=getattr(current_user, "is_superuser", False) is True
    )
    rows = [
        public_built_in_model(
            row, account_has_default=row.model_kind in account_default_kinds
        )
        if not isinstance(row, SharedResourceRead) and row.account_id is None
        else row
        for row in priced
    ]
    if default_only:
        rows = [row for row in rows if getattr(row, "is_default", False) is True]
    return rows


def _apply_tariff_visibility(
    rows: List, *, is_admin: bool
) -> List[SharedResourceRead | AIModelRead]:
    """Hide hosted models with no verified operator tariff from pickers.

    Non-admin callers never see them, so a model that would be refused at
    request time is not offered at selection time. Platform admins keep them,
    marked ``unpriced``, so an operator can find and fix the gap.
    """
    result: List = []
    for row in rows:
        if hosted_model_selectable(row):
            result.append(row)
        elif is_admin:
            result.append(
                AIModelRead.model_validate(row).model_copy(update={"unpriced": True})
            )
    return result


@router.get(
    "/ai-models/overview",
    response_model=AIModelsOverviewResponse,
    summary="Get AI Models Overview",
    tags=["AI Models"],
)
@heavy_route
@require_permission("view_ai_models")
def get_ai_models_overview(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    failed_since: Optional[List[str]] = Query(
        None,
        description=(
            "Repeatable '<ai_model_id>:<ISO-8601 timestamp>' pair. Each pair "
            "asks for that model's failed_requests_since: how many of its "
            "failures in this window are newer than the timestamp. The "
            "console passes the moment an operator marked the model fixed, "
            "so a row can say '2 failed since fix' instead of repeating the "
            "whole window's total."
        ),
    ),
    include_prior_period: bool = Query(
        False,
        description=(
            "Also return prior_period_estimated_cost, the spend of the window "
            "of equal length just before this one, so the page needs one "
            "request instead of two."
        ),
    ),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelsOverviewResponse:
    """Return usage, active sessions and price source for every model."""
    # Declared before ``/ai-models/{model_id}`` so the literal path wins the
    # route match.
    #
    # Answers the whole page in a fixed number of queries: the models, one
    # grouped usage aggregate, one grouped active-session count and one read
    # of the account's price overrides. The per-model summary, runtime-session,
    # interaction and pricing endpoints stay for the detail page, where they
    # are asked for one model at a time.
    period_start, period_end = normalize_usage_period(start_date, end_date)
    account_id = str(current_user.account_id)
    models = crud_ai_model.get_selectable_for_account(
        db=db,
        account_id=current_user.account_id,
        include_built_in=built_in_models_enabled(),
    )
    account_default_kinds = crud_ai_model.configured_account_default_kinds(models)
    from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable

    models = [
        *[model for model in models if model.account_id is None],
        *filter_viewable(
            db,
            current_user,
            VISIBLE_AI_MODEL,
            [model for model in models if model.account_id is not None],
        ),
    ]
    models = [
        model
        for model in models
        if hosted_model_selectable(model)
        or getattr(current_user, "is_superuser", False) is True
    ]
    if not models:
        return AIModelsOverviewResponse(
            period_start=period_start,
            period_end=period_end,
            models=[],
            prior_period_estimated_cost=0.0 if include_prior_period else None,
        )

    model_ids = [str(model.id) for model in models]
    known_ids = set(model_ids)
    # Pairs for models this account does not have are dropped rather than
    # rejected: a stale console tab is not a bad request.
    failed_since_by_model = {
        model_id: moment
        for model_id, moment in _parse_failed_since(failed_since).items()
        if model_id in known_ids
    }
    # ``limit=None``: this is a total per model, and a request-count-ordered
    # truncation would silently zero the quietest models on the page.
    usage_rows = crud_api_usage.get_gateway_usage_by_model(
        db,
        account_id=account_id,
        start_date=period_start,
        end_date=period_end,
        ai_model_ids=model_ids,
        failed_since=failed_since_by_model or None,
        limit=None,
    )
    usage_by_model = _collapse_usage_by_model(usage_rows)
    active_sessions = crud_runtime_session.count_active_sessions_by_model(
        db,
        account_id=account_id,
        ai_model_ids=model_ids,
        start_date=period_start,
        end_date=period_end,
    )
    pricing = get_effective_pricing_bulk(
        db, account_id=current_user.account_id, ai_models=models
    )

    items: List[AIModelOverviewItem] = []
    for model in models:
        model_id = str(model.id)
        usage = usage_by_model.get(model_id)
        requests = usage["request_count"] if usage else 0
        failed = usage["failed_request_count"] if usage else 0
        asked_since = model_id in failed_since_by_model
        alias_failures = _alias_failures(
            usage["failure_groups"] if usage else [],
            asked_since=asked_since,
        )
        items.append(
            AIModelOverviewItem(
                ai_model_id=model_id,
                model_name=model.name,
                provider_name=model.provider_name,
                model_identifier=model.model_identifier,
                model_alias=_gateway_alias(model),
                is_default=bool(model.is_default)
                and (
                    model.account_id is not None
                    or model.model_kind not in account_default_kinds
                ),
                total_requests=requests,
                successful_requests=max(requests - failed, 0),
                failed_requests=failed,
                token_usage=GatewayTokenUsage.from_row(usage),
                estimated_cost=usage["estimated_cost"] if usage else 0.0,
                unpriced_request_count=usage["unpriced_request_count"] if usage else 0,
                active_session_count=active_sessions.get(model_id, 0),
                last_request_at=usage["last_request_at"] if usage else None,
                last_failure_at=usage["last_failure_at"] if usage else None,
                last_failure_alias=usage["last_failure_alias"] if usage else None,
                failed_requests_since=(
                    usage["failed_request_count_since"]
                    if usage and asked_since
                    else None
                ),
                alias_failures=alias_failures,
                pricing_source=(
                    pricing[model_id].source if model_id in pricing else "none"
                ),
            )
        )

    prior_cost: Optional[float] = None
    if include_prior_period:
        prior_rows = crud_api_usage.get_gateway_usage_by_model(
            db,
            account_id=account_id,
            start_date=period_start - (period_end - period_start),
            end_date=period_start,
            ai_model_ids=model_ids,
            limit=None,
        )
        prior_cost = float(
            sum(
                usage["estimated_cost"] or 0.0
                for usage in _collapse_usage_by_model(prior_rows).values()
            )
        )

    return AIModelsOverviewResponse(
        period_start=period_start,
        period_end=period_end,
        models=items,
        prior_period_estimated_cost=prior_cost,
    )


@router.get(
    "/ai-models/{model_id}",
    response_model=SharedResourceRead | AIModelRead,
    summary="Get AI Model by ID",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> SharedResourceRead | AIModelRead:
    """Retrieve a specific AI Model by its ID."""
    from preloop.models.crud.resource_share import crud_resource_share

    shared = crud_resource_share.public_read(
        db,
        account_id=current_user.account_id,
        resource_type="ai_model",
        resource_id=model_id,
    )
    if shared is not None:
        from preloop.plugins.account_hooks import VISIBLE_AI_MODEL, filter_viewable

        if not filter_viewable(db, current_user, VISIBLE_AI_MODEL, [shared]):
            raise HTTPException(404, "Resource not found")
        return shared
    model = _get_readable_ai_model(db=db, model_id=model_id, current_user=current_user)
    if model.account_id is None:
        return public_built_in_model(
            model,
            account_has_default=crud_ai_model.account_default_model_exists(
                db, account_id=current_user.account_id, model_kind=model.model_kind
            ),
        )
    return model


@router.get(
    "/ai-models/{model_id}/used-by",
    response_model=AIModelUsedByResponse,
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model_used_by(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelUsedByResponse:
    """List visible, tenant-owned agents and flows configured to use a model."""
    from preloop.services.ai_model_usage_links import configured_model_consumers

    _get_readable_ai_model(db=db, model_id=model_id, current_user=current_user)
    return configured_model_consumers(db, user=current_user, model_id=str(model_id))


# Both dates omitted means the last 7 days (model_detail_window). One bound
# the caller sends is kept, so a 30-day request still covers 30 days.
_MODEL_DETAIL_WINDOW_NOTE = (
    "When both start_date and end_date are omitted, the server uses the "
    "last 7 days. A bound the caller sends is kept."
)


@router.get(
    "/ai-models/{model_id}/summary",
    response_model=AIModelGatewayUsageSummaryResponse,
    summary="Get AI Model Usage Summary",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model_usage_summary(
    model_id: uuid.UUID,
    start_date: Optional[datetime] = Query(
        None,
        description="Lower bound of the summary window. " + _MODEL_DETAIL_WINDOW_NOTE,
    ),
    end_date: Optional[datetime] = Query(
        None,
        description="Upper bound of the summary window. " + _MODEL_DETAIL_WINDOW_NOTE,
    ),
    failed_since: Optional[datetime] = Query(
        None,
        description=(
            "Count this model's failures newer than this moment into "
            "failed_requests_since. The console passes the moment the model "
            "was marked fixed."
        ),
    ),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelGatewayUsageSummaryResponse:
    """Return model-scoped gateway usage totals for one AI model."""
    db_model = _get_readable_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    # Same omitted-date window as the two lists. All time sends no dates,
    # and the summary must not stay on 30 days while the lists cover 7.
    start_date, end_date = model_detail_window(start_date, end_date)
    return ModelGatewayUsageService(db).get_ai_model_summary(
        ai_model=db_model,
        account_id=current_user.account_id,
        start_date=start_date,
        end_date=end_date,
        failed_since=failed_since,
    )


@router.get(
    "/ai-models/{model_id}/runtime-sessions",
    response_model=AccountRuntimeSessionListResponse,
    summary="List AI Model Runtime Sessions",
    tags=["AI Models"],
)
@heavy_route
@require_permission("view_ai_models")
def list_ai_model_runtime_sessions(
    model_id: uuid.UUID,
    query: Optional[str] = Query(None, min_length=1),
    session_source_type: Optional[str] = Query(None),
    status: str = Query("all", pattern="^(all|active|ended)$"),
    start_date: Optional[datetime] = Query(
        None,
        description=("Lower bound of the session window. " + _MODEL_DETAIL_WINDOW_NOTE),
    ),
    end_date: Optional[datetime] = Query(
        None,
        description=("Upper bound of the session window. " + _MODEL_DETAIL_WINDOW_NOTE),
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AccountRuntimeSessionListResponse:
    """List runtime sessions that used one durable AI model."""
    db_model = _get_readable_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    account = _get_current_account(db=db, current_user=current_user)
    # No dates means the last 7 days. The console sends the range it shows.
    start_date, end_date = model_detail_window(start_date, end_date)
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
    start_date: Optional[datetime] = Query(
        None,
        description=(
            "Lower bound of the interaction window. " + _MODEL_DETAIL_WINDOW_NOTE
        ),
    ),
    end_date: Optional[datetime] = Query(
        None,
        description=(
            "Upper bound of the interaction window. " + _MODEL_DETAIL_WINDOW_NOTE
        ),
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AccountGatewayUsageSearchResponse:
    """List indexed gateway interactions scoped to one AI model."""
    db_model = _get_readable_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    account = _get_current_account(db=db, current_user=current_user)
    # No dates means the last 7 days. The console sends the range it shows.
    start_date, end_date = model_detail_window(start_date, end_date)
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


# Same key the gateway reads (openai_gateway.CODEX_UPSTREAM_WS_ACCOUNT_SETTING).
CODEX_UPSTREAM_WS_SETTING = "codex_upstream_websocket"


def _apply_codex_ws_override(db_model: Any, obj_in: Dict[str, Any]) -> Dict[str, Any]:
    """Fold ``codex_upstream_websocket`` into ``meta_data`` (#1524).

    The flag merges into the meta_data being written (the request's, or the
    stored one when the request carries none), so setting it never drops
    other keys such as the gateway block. ``None`` removes the override.
    """
    obj_in = dict(obj_in)
    value = obj_in.pop("codex_upstream_websocket")
    provider = (
        str(obj_in.get("provider_name") or db_model.provider_name or "").strip().lower()
    )
    if provider != "openai-codex" and value is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="codex_upstream_websocket applies only to openai-codex models",
        )
    base = obj_in.get("meta_data")
    if not isinstance(base, dict):
        base = db_model.meta_data if isinstance(db_model.meta_data, dict) else {}
    meta = dict(base)
    if value is None:
        meta.pop(CODEX_UPSTREAM_WS_SETTING, None)
    else:
        meta[CODEX_UPSTREAM_WS_SETTING] = value
    obj_in["meta_data"] = meta
    return obj_in


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
    ensure_permission_in_oss(db, current_user, "edit_ai_models")
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )

    obj_in = ai_model_in.dict(exclude_unset=True)
    if "codex_upstream_websocket" in obj_in:
        obj_in = _apply_codex_ws_override(db_model, obj_in)
    try:
        updated_model = crud_ai_model.update(
            db=db,
            db_obj=db_model,
            obj_in=obj_in,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return updated_model


def _owner_flow_use_state(
    db: Session, db_model: AIModel, current_user: User
) -> AIModelOwnerFlowUseRead:
    """Describe the owner-flow opt-in from the current user's point of view."""
    from datetime import timedelta

    from preloop.services.subscription_flow_use import (
        owner_flow_use_enabled,
        subscription_owner_user_id,
    )

    applicable = bool(getattr(db_model, "is_principal_bound_oauth", False))
    if not applicable:
        return AIModelOwnerFlowUseRead(
            applicable=False, enabled=False, can_manage=False
        )
    owner = subscription_owner_user_id(db, db_model)
    can_manage = owner is not None and owner == str(current_user.id)
    usage = None
    if can_manage:
        end = datetime.now(timezone.utc)
        usage = AIModelOwnerFlowUsage(
            **crud_api_usage.owner_flow_subscription_usage(
                db,
                account_id=current_user.account_id,
                ai_model_id=db_model.id,
                start=end - timedelta(days=30),
                end=end,
            )
        )
    return AIModelOwnerFlowUseRead(
        applicable=True,
        enabled=owner_flow_use_enabled(db, db_model),
        can_manage=can_manage,
        usage=usage,
    )


@router.get(
    "/ai-models/{model_id}/owner-flow-use",
    response_model=AIModelOwnerFlowUseRead,
    summary="Get Owner Flow Subscription Use",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model_owner_flow_use(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelOwnerFlowUseRead:
    """Whether hosted flows owned by the subscription's owner may use it."""
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user, view=True
    )
    return _owner_flow_use_state(db, db_model, current_user)


@router.put(
    "/ai-models/{model_id}/owner-flow-use",
    response_model=AIModelOwnerFlowUseRead,
    summary="Set Owner Flow Subscription Use",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def set_ai_model_owner_flow_use(
    model_id: uuid.UUID,
    body: AIModelOwnerFlowUseUpdate,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelOwnerFlowUseRead:
    """Let (or stop) the subscription owner's own flows use this model.

    Only the user who owns the subscription (the owner of the managed agent
    bound to it) may change this. Account admins cannot set it for another
    user: a flow may spend a subscription only with its owner's consent.
    """
    from preloop.services.subscription_flow_use import subscription_owner_user_id

    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user, view=True
    )
    if not bool(getattr(db_model, "is_principal_bound_oauth", False)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only subscription (OAuth) models have this setting",
        )
    if subscription_owner_user_id(db, db_model) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the subscription's owner can change this setting",
        )
    from preloop.utils.audit import log_config_change

    was_enabled = db_model.owner_flow_use_user_id is not None
    crud_ai_model.set_owner_flow_use(
        db, db_obj=db_model, user_id=current_user.id if body.enabled else None
    )
    log_config_change(
        db,
        user=current_user,
        config_type="ai_model_owner_flow_use",
        action="updated",
        old_value={"ai_model_id": str(db_model.id), "enabled": was_enabled},
        new_value={"ai_model_id": str(db_model.id), "enabled": body.enabled},
    )
    return _owner_flow_use_state(db, db_model, current_user)


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
    ensure_permission_in_oss(db, current_user, "delete_ai_models")
    _get_account_ai_model(db=db, model_id=model_id, current_user=current_user)

    crud_ai_model.remove(db=db, id=model_id)

    # No content returned for HTTP 204
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/ai-models/{model_id}/pricing",
    response_model=AIModelPricingResponse,
    summary="Get AI Model Pricing",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def get_ai_model_pricing(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelPricingResponse:
    """Return the effective price for one model and where it came from."""
    db_model = _get_readable_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    return get_effective_pricing(
        db, account_id=current_user.account_id, ai_model=db_model
    )


@router.post(
    "/ai-models/{model_id}/pricing/fetch",
    response_model=AIModelPriceQuote,
    summary="Fetch AI Model Pricing From Provider",
    tags=["AI Models"],
)
@require_permission("edit_ai_models")
def fetch_ai_model_pricing(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelPriceQuote:
    """Read this model's published price from its provider, without saving it.

    A price decides what every past and future request cost, so it is never
    written by a fetch: the numbers come back for a person to confirm, and
    saving one stays with the price override endpoints.
    """
    ensure_permission_in_oss(db, current_user, "edit_ai_models")
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user
    )
    try:
        return fetch_provider_pricing(db_model)
    except PriceFetchUnsupportedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except PriceFetchUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


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
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "create_ai_models")
    )
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


def _credential_last_refresh(ai_model: AIModel) -> Optional[datetime]:
    """Return when Preloop last wrote the model's credential bundle.

    The secret row's ``last_verified_at`` is set on import, on every CLI push,
    and on every server-side refresh. The column is stored without a zone and
    holds UTC, so a naive value is tagged as UTC.

    Args:
        ai_model: Account AI model whose credential secret is inspected.

    Returns:
        The timestamp in UTC, or None when the model has no secret row or the
        row was never written with a timestamp.
    """
    secret = getattr(ai_model, "credentials_secret", None)
    value = getattr(secret, "last_verified_at", None) if secret else None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_expires_ms(payload: Dict) -> Optional[int]:
    """Return the bundle's ``expires`` epoch milliseconds when it is usable.

    Args:
        payload: Decoded structured credential payload.

    Returns:
        A positive integer, or None when the field is absent or malformed.
    """
    expires_raw = payload.get("expires")
    if isinstance(expires_raw, bool):
        return None
    if isinstance(expires_raw, (int, float)) and expires_raw > 0:
        return int(expires_raw)
    return None


@router.get(
    "/ai-models/{model_id}/credentials/marker",
    response_model=AIModelCredentialMarkerResponse,
    summary="Read Subscription OAuth Rotation Marker",
    tags=["AI Models"],
)
@require_permission("view_ai_models")
def read_ai_model_credential_marker(
    model_id: uuid.UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> AIModelCredentialMarkerResponse:
    """Return the rotation marker of a stored subscription-OAuth bundle.

    Subscription OAuth grants (Claude Code, Codex) use single-use refresh
    tokens. When both the operator's laptop and Preloop hold a copy, the CLI
    keeps them on one lineage: it pushes a newer local bundle, and pulls
    Preloop's bundle through ``POST /ai-models/{model_id}/credentials/export``
    when Preloop's copy is newer. This read tells the CLI which side is newer
    without downloading tokens on every Codex permission-hook call.

    The response carries no token material: ``expires`` is the stored
    access-token expiry in epoch milliseconds and moves forward on every
    rotation, ``last_refresh`` is when Preloop last wrote the bundle (import,
    CLI push, or server-side refresh), ``credentials_status`` is ``error``
    when the last server-side refresh failed, and ``account_id`` names the
    provider account the bundle belongs to. This read never refreshes the
    bundle, so it cannot rotate the grant by itself. API-key credentials are
    refused with 400.
    """
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user, view=True
    )
    service = get_secret_service()
    resolved = service.resolve_ai_model_credentials(
        db_model, db=db, allow_refresh=False
    )
    if (
        resolved is None
        or resolved.credential_type not in PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only subscription OAuth credentials have a rotation marker",
        )
    payload = resolved.payload or {}
    secret = getattr(db_model, "credentials_secret", None)
    status_raw = getattr(secret, "status", None) if secret else None
    return AIModelCredentialMarkerResponse(
        credential_type=resolved.credential_type,
        expires=_coerce_expires_ms(payload),
        last_refresh=_credential_last_refresh(db_model),
        credentials_status=status_raw if isinstance(status_raw, str) else None,
        account_id=str(payload.get("account_id") or "").strip() or None,
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
    local login before the Preloop-held credential is removed, and from the
    Codex permission hook and ``preloop agents sync-credentials`` to pull
    Preloop's copy back into the local login when
    ``GET /ai-models/{model_id}/credentials/marker`` shows it is newer. A
    stored bundle that is about to expire is refreshed before it is returned.
    ``last_refresh`` is when Preloop last wrote the bundle. Every export is
    written to the audit log without token material. API-key credentials are
    never exportable.
    """
    ensure_permission_in_oss(db, current_user, "edit_ai_models")
    db_model = _get_account_ai_model(
        db=db, model_id=model_id, current_user=current_user, view=True
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
    expires = _coerce_expires_ms(payload)
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
        last_refresh=_credential_last_refresh(db_model),
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
    (``aws_bearer_token_bedrock`` or IAM access keys and optional session
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
        "aws_bearer_token_bedrock",
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
    if isinstance(region, str) and region.strip():
        auth["aws_region_name"] = region.strip()
    if not auth.get("aws_bearer_token_bedrock") and (
        not auth.get("aws_access_key_id") or not auth.get("aws_secret_access_key")
    ):
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
    if typed_aws and set(typed_aws) <= {"aws_region_name"} and stored_aws:
        # Editing the discovery region must not discard the stored credentials.
        aws_auth = {**stored_aws, **typed_aws}
    else:
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

    Only present fields are forwarded. A blank region is absent, so editing
    with stored credentials preserves the saved region until a value is supplied.
    """
    if request_in is None:
        return None
    auth = {
        key: getattr(request_in, key)
        for key in (
            "aws_bearer_token_bedrock",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_region_name",
        )
        if getattr(request_in, key) is not None
    }
    region = auth.get("aws_region_name")
    if isinstance(region, str):
        if region.strip():
            auth["aws_region_name"] = region.strip()
        else:
            auth.pop("aws_region_name")
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
            warnings=result.warnings,
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
