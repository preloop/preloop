from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Literal, Optional, Tuple
import gc
import logging
import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text as sa_text
import openai
import os
import json
from datetime import datetime, UTC

from preloop.services.aux_model_retry import call_with_aux_retry
from preloop.services.model_credentials import (
    AuxApiKeyMissingError,
    build_aux_openai_client,
    get_aux_openai_sdk_extra_kwargs,
    resolve_model_call_credentials,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.schemas.issue_duplicate import (
    IssueDuplicate as IssueDuplicateSchema,
    IssueDuplicateAiStatus,
    IssueDuplicateAiError,
    IssueDuplicateSuggestionRequest,
    IssueDuplicateSuggestionResponse,
    IssueDuplicateResolutionRequest,
    IssueDuplicateResolutionResponse,
    IssueDuplicateVerdictBatchRequest,
    IssueDuplicateVerdictBatchResponse,
    IssueDuplicateVerdictItem,
)
from preloop.models.crud import crud_issue_duplicate

from preloop.schemas.issue import IssueResponse, IssueUpdate

from fastapi import Query
from preloop.models.crud import (
    crud_issue_embedding,
    crud_embedding_model,
    crud_issue,
    crud_ai_model,
)
from preloop.models.db.session import get_db_session as get_db

from preloop.schemas.duplicates import (
    ProjectDuplicatesResponse,
    DuplicateIssuePair,
)
from preloop.schemas.issue_duplicate import (
    IssueDuplicateProjectStats,
    IssueDuplicateStats,
    IssueDuplicateUpdate,
    IssueDuplicate,
)
from preloop.models.models.account import Account
from preloop.models.models.user import User  # Import Account model
from preloop.models.models.project import Project
from preloop.models.models.issue import Issue

from .issues import update_issue

from preloop.api.auth import get_current_active_user  # Import user dependency
from preloop.config import get_settings, Settings
from preloop.api.common import (
    load_duplicates_prompts_config,
    get_accessible_projects,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/issue-duplicates/confirmed",
    response_model=List[IssueDuplicateSchema],
    tags=["Issue Duplicates"],
)
def get_duplicate_issues(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Retrieve confirmed duplicate issues.
    """
    duplicates = crud_issue_duplicate.get_multi(
        db,
        skip=skip,
        limit=limit,
        decision="duplicate",
        account_id=current_user.account_id,
    )
    return duplicates


@router.get(
    "/issue-duplicates/ai-status",
    response_model=IssueDuplicateAiStatus,
    tags=["Issue Duplicates"],
)
def get_issue_duplicate_ai_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> IssueDuplicateAiStatus:
    """Report whether a default AI model is configured for verdicts."""
    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        return IssueDuplicateAiStatus(configured=False, model_name=None)
    return IssueDuplicateAiStatus(
        configured=True,
        model_name=default_model.name or default_model.model_identifier,
    )


@router.get(
    "/issue-duplicates/check",
    response_model=IssueDuplicateSchema,
    tags=["Issue Duplicates"],
    responses={
        422: {
            "model": IssueDuplicateAiError,
            "description": (
                "No default AI model. `detail.code` is `no_default_ai_model`. "
                "`detail` is this object, not a string."
            ),
        },
        500: {
            "model": IssueDuplicateAiError,
            "description": (
                "The model call failed. `detail.code` is `ai_model_error`. "
                "`detail` is this object, not a string."
            ),
        },
    },
)
def check_or_create_issue_duplicate(
    *,
    db: Session = Depends(get_db),
    issue1_id: str,
    issue2_id: str,
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
) -> Any:
    if issue1_id == issue2_id:
        raise HTTPException(status_code=400, detail="Issue IDs cannot be the same.")

    existing_duplicate = crud_issue_duplicate.get_by_issue_ids(
        db, issue1_id=issue1_id, issue2_id=issue2_id, account_id=current_user.account_id
    )
    if existing_duplicate:
        logger.info(
            f"Found existing duplicate entry for issues {issue1_id} and {issue2_id}."
        )
        return IssueDuplicate.model_validate(existing_duplicate)

    logger.info(
        f"No existing duplicate entry for issues {issue1_id} and {issue2_id}. Proceeding with AI model analysis."
    )

    issue1 = crud_issue.get(db, id=issue1_id, account_id=current_user.account_id)
    issue2 = crud_issue.get(db, id=issue2_id, account_id=current_user.account_id)

    if not issue1 or not issue2:
        missing_ids_str = []
        if not issue1:
            missing_ids_str.append(str(issue1_id))
        if not issue2:
            missing_ids_str.append(str(issue2_id))
        detail = f"Issue(s) not found: {', '.join(missing_ids_str)}."
        logger.warning(detail)
        raise HTTPException(status_code=404, detail=detail)

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        logger.error("No default active AI model configured.")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_default_ai_model",
                "message": "No default AI model configured. Set one under Models.",
            },
        )

    return _classify_and_store(
        db,
        issue1=issue1,
        issue2=issue2,
        default_model=default_model,
        settings=settings,
    )


def _classify_and_store(
    db: Session,
    *,
    issue1: Issue,
    issue2: Issue,
    default_model: Any,
    settings: Settings,
) -> Any:
    """Ask the default model about one pair and store its verdict.

    Shared by ``GET /issue-duplicates/check`` and the background checks the
    batch endpoint starts. Raises the same ``HTTPException``s the single
    check has always returned.
    """
    issue1_id, issue2_id = issue1.id, issue2.id
    logger.info(f"Using AI model '{default_model.model_identifier}'.")

    prompts_config = load_duplicates_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get("duplicate_classification_v1")
    if not prompt_data or "system" not in prompt_data or "user" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Duplicate classification prompt not configured."
        )

    system_prompt = prompt_data["system"]
    user_prompt_template = prompt_data["user"]

    prompt_text = user_prompt_template.format(
        issue1_id=issue1.id,
        issue1_title=issue1.title or "N/A",
        issue1_description=issue1.description or "No description provided.",
        issue2_id=issue2.id,
        issue2_title=issue2.title or "N/A",
        issue2_description=issue2.description or "No description provided.",
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]

    llm_response_text = ""
    try:
        creds_kwargs = resolve_model_call_credentials(default_model, db=db)
        try:
            # The model's own endpoint, as the other two calls in this file
            # do. Without it a default model served anywhere but OpenAI
            # (OpenRouter, a gateway, a self-hosted server) was sent to
            # api.openai.com with its own key, and every check answered 500
            # (#1588).
            client = build_aux_openai_client(
                openai,
                default_model,
                creds_kwargs,
                include_api_base=True,
                static_key_fallback=os.getenv("OPENAI_API_KEY"),
                timeout=30.0,
                max_retries=0,
            )
        except AuxApiKeyMissingError:
            logger.error(
                f"OpenAI API key not found for model {default_model.model_identifier} or environment variable."
            )
            raise HTTPException(
                status_code=500, detail="OpenAI API key not configured."
            ) from None
        aux_extras = get_aux_openai_sdk_extra_kwargs(
            default_model,
            call_site_kwargs={
                "model": default_model.model_identifier,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
        )

        response = call_with_aux_retry(
            lambda: client.chat.completions.create(
                model=default_model.model_identifier,
                messages=messages,
                response_format={"type": "json_object"},
                **aux_extras,
            ),
            operation_name="issue_duplicate_check",
            provider=getattr(default_model, "provider_name", None),
        )
        llm_response_text = response.choices[0].message.content.strip()
        logger.info(
            "AI model response for issues %s, %s: chars=%s",
            issue1_id,
            issue2_id,
            len(llm_response_text),
        )

        response_obj = json.loads(llm_response_text)

        decision_word = response_obj.get("classification", "").upper()
        reason = response_obj.get("reason")
        suggestion = response_obj.get("suggestion")

        if decision_word == "DUPLICATE":
            parsed_status = "duplicate"
        elif decision_word == "OVERLAPPING":
            parsed_status = "overlapping"
        elif decision_word == "UNRELATED":
            parsed_status = "unrelated"
        else:
            logger.warning(
                f"AI model returned unexpected status: '{decision_word}'. Defaulting to 'undecided'."
            )
            parsed_status = "undecided"
            reason = llm_response_text

        duplicate_create_data = IssueDuplicate(
            issue1_id=issue1.id,
            issue2_id=issue2.id,
            decision=parsed_status,
            ai_model_id=default_model.id,
            ai_model_name=default_model.model_identifier,
            reason=reason,
            suggestion=suggestion,
        )

        new_duplicate_entry = crud_issue_duplicate.create(
            db, obj_in=duplicate_create_data.model_dump()
        )
        logger.info(
            f"Created new IssueDuplicate entry ID {new_duplicate_entry.id} for issues {issue1_id}, {issue2_id} with status '{parsed_status}'."
        )
        return new_duplicate_entry

    except openai.APIError as e:
        logger.error(
            f"OpenAI API call for model '{default_model.model_identifier}' failed: {type(e).__name__}"
        )
        raise HTTPException(
            status_code=500,
            detail={"code": "ai_model_error", "message": "AI model API error"},
        )
    except ModelGatewayAPIError as exc:
        logger.warning(
            "AI model call for issue duplicate check did not recover: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail="AI model is rate limited; retry later.",
            headers=exc.response_headers(),
        )
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during AI model invocation for model '{default_model.model_identifier}': {type(e).__name__}"
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ai_model_error",
                "message": f"AI model processing error: {str(e)}",
            },
        )


# --- Batched verdicts (one call per Duplicates page) -------------------------
#
# ``GET /issue-duplicates/check`` answers one pair and, for a new pair, waits
# for the model: 10 to 14 s per pair on prod, with a request thread and a
# pooled connection held the whole time. The console sent one per row. The
# batch endpoint below answers every pair on a page from stored verdicts in a
# single query and hands new pairs to a small background pool; the console
# asks again for the pending ones until they are done.

_VERDICT_FAILURE_TTL_SECONDS = 300.0
# Checks queued or running, per account and per process. Past either cap a
# pair stays ``pending`` and is not queued; the console asks again, and the
# pair is queued once a slot frees. This bounds the model spend one client
# can start, which the old two-request client pool bounded by itself.
_VERDICT_MAX_INFLIGHT_PER_ACCOUNT = 20
_VERDICT_MAX_INFLIGHT = 200
_VERDICT_FAILURES_MAX = 2000
# Advisory-lock namespace for "this pair is being checked". The in-process
# bookkeeping covers one API process; the lock covers every replica.
_VERDICT_LOCK_NAMESPACE = 0x1D0B
_verdict_lock = threading.Lock()
_verdict_inflight: Dict[Tuple[str, str, str], Future] = {}
_verdict_failures: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
_verdict_executor: Optional[ThreadPoolExecutor] = None


def _verdict_concurrency() -> int:
    raw = os.getenv("ISSUE_DUPLICATE_VERDICT_CONCURRENCY", "2")
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _get_verdict_executor() -> ThreadPoolExecutor:
    global _verdict_executor
    with _verdict_lock:
        if _verdict_executor is None:
            _verdict_executor = ThreadPoolExecutor(
                max_workers=_verdict_concurrency(),
                thread_name_prefix="issue-duplicate-verdict",
            )
        return _verdict_executor


def _pair_key(account_id: Any, a: Any, b: Any) -> Tuple[str, str, str]:
    low, high = sorted((str(a), str(b)))
    return (str(account_id), low, high)


def _open_verdict_session() -> Session:
    """A session of its own: the request's session is gone by then."""
    from preloop.models.db.session import get_session_factory

    return get_session_factory()()


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        if exc.status_code == 429:
            return "rate_limited"
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("code"):
            return str(detail["code"])
        if "not configured" in str(detail):
            return "not_configured"
    return "ai_model_error"


def _run_verdict_check(
    key: Tuple[str, str, str],
    account_id: Any,
    issue1_id: Any,
    issue2_id: Any,
    settings: Settings,
) -> None:
    """Background body: claim the pair across replicas, then check it."""
    try:
        with _pair_check_claim(key) as claimed:
            if claimed:
                _check_claimed_pair(key, account_id, issue1_id, issue2_id, settings)
            # Not claimed: another API process is checking this pair. Its
            # verdict is stored and returned on a later poll.
    except Exception as exc:  # the claim itself failed (database unavailable)
        logger.warning(
            "Could not claim duplicate verdict %s: %s", key, type(exc).__name__
        )
        _remember_failure(key, "ai_model_error")
    finally:
        with _verdict_lock:
            _verdict_inflight.pop(key, None)


def _lock_ids(key: Tuple[str, str, str]) -> Tuple[int, int]:
    digest = hashlib.blake2b("|".join(key).encode(), digest_size=4).digest()
    return _VERDICT_LOCK_NAMESPACE, int.from_bytes(digest, "big", signed=True)


@contextmanager
def _pair_check_claim(key: Tuple[str, str, str]) -> Iterator[bool]:
    """Hold a Postgres advisory lock on ``key`` for the length of a check.

    The lock lives on a dedicated autocommit connection. It is never left on
    a pooled request connection, and no transaction stays open while the
    model answers. Yields False when another process holds it.
    """
    from preloop.models.db.session import get_engine

    namespace, pair = _lock_ids(key)
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        claimed = bool(
            conn.execute(
                sa_text("SELECT pg_try_advisory_lock(:ns, :pair)"),
                {"ns": namespace, "pair": pair},
            ).scalar()
        )
        try:
            yield claimed
        finally:
            if claimed:
                conn.execute(
                    sa_text("SELECT pg_advisory_unlock(:ns, :pair)"),
                    {"ns": namespace, "pair": pair},
                )


def _check_claimed_pair(
    key: Tuple[str, str, str],
    account_id: Any,
    issue1_id: Any,
    issue2_id: Any,
    settings: Settings,
) -> None:
    """Check one pair unless someone stored it meanwhile; never raises."""
    db = _open_verdict_session()
    try:
        if crud_issue_duplicate.get_by_issue_ids(
            db, issue1_id=issue1_id, issue2_id=issue2_id, account_id=account_id
        ):
            return
        issue1 = crud_issue.get(db, id=issue1_id, account_id=account_id)
        issue2 = crud_issue.get(db, id=issue2_id, account_id=account_id)
        model = crud_ai_model.get_default_active_model(db, account_id=account_id)
        if not issue1 or not issue2 or not model:
            return
        _classify_and_store(
            db, issue1=issue1, issue2=issue2, default_model=model, settings=settings
        )
    except Exception as exc:  # recorded for the next poll, never raised
        logger.warning(
            "Background duplicate verdict for %s failed: %s", key, type(exc).__name__
        )
        _remember_failure(key, _failure_code(exc))
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover - close must not mask the result
            logger.debug("Closing the verdict session failed", exc_info=True)


def _schedule_verdict_check(
    key: Tuple[str, str, str],
    account_id: Any,
    issue1_id: Any,
    issue2_id: Any,
    settings: Settings,
) -> None:
    """Queue a background check for ``key`` unless one is queued or capped."""
    with _verdict_lock:
        if key in _verdict_inflight:
            return
        if len(_verdict_inflight) >= _VERDICT_MAX_INFLIGHT:
            return
        if (
            sum(1 for other in _verdict_inflight if other[0] == key[0])
            >= _VERDICT_MAX_INFLIGHT_PER_ACCOUNT
        ):
            return
        # Reserve the slot before submitting so a fast worker's cleanup
        # cannot run ahead of the bookkeeping.
        _verdict_inflight[key] = Future()
    try:
        future = _get_verdict_executor().submit(
            _run_verdict_check, key, account_id, issue1_id, issue2_id, settings
        )
    except Exception:
        with _verdict_lock:
            _verdict_inflight.pop(key, None)
        raise
    with _verdict_lock:
        if key in _verdict_inflight:
            _verdict_inflight[key] = future


def _remember_failure(key: Tuple[str, str, str], code: str) -> None:
    """Record a failed check, sweeping expired entries once the map is large."""
    now = time.monotonic()
    with _verdict_lock:
        if len(_verdict_failures) >= _VERDICT_FAILURES_MAX:
            for stale in [
                k
                for k, (_, at) in _verdict_failures.items()
                if now - at > _VERDICT_FAILURE_TTL_SECONDS
            ]:
                del _verdict_failures[stale]
            if len(_verdict_failures) >= _VERDICT_FAILURES_MAX:
                _verdict_failures.clear()
        _verdict_failures[key] = (code, now)


def _recent_failure(key: Tuple[str, str, str]) -> Optional[str]:
    with _verdict_lock:
        failure = _verdict_failures.get(key)
        if failure is None:
            return None
        code, at = failure
        if time.monotonic() - at > _VERDICT_FAILURE_TTL_SECONDS:
            _verdict_failures.pop(key, None)
            return None
        return code


@router.post(
    "/issue-duplicates/verdicts",
    response_model=IssueDuplicateVerdictBatchResponse,
    tags=["Issue Duplicates"],
    summary="AI verdicts for a page of duplicate pairs",
)
def get_issue_duplicate_verdicts(
    *,
    body: IssueDuplicateVerdictBatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
) -> IssueDuplicateVerdictBatchResponse:
    """Stored verdicts for every pair at once; new pairs are checked in the background.

    Answers from the database only, so it returns in milliseconds. A pair
    without a stored verdict comes back ``pending`` (a background check is
    running) or ``no_model`` (no default AI model, nothing was started).
    A failed check is reported as ``failed`` for five minutes, or until the
    caller sends ``retry: true``. Ask again with the pending pairs to pick
    up their verdicts.
    """
    account_id = current_user.account_id
    requested = [(p.issue1_id, p.issue2_id) for p in body.pairs]

    stored = {
        _pair_key(account_id, row.issue1_id, row.issue2_id): row
        for row in crud_issue_duplicate.get_many_by_pairs(
            db, pairs=requested, account_id=account_id
        )
    }
    missing = [
        pair
        for pair in requested
        if pair[0] != pair[1] and _pair_key(account_id, *pair) not in stored
    ]

    owned: set[str] = set()
    default_model = None
    if missing:
        owned = crud_issue.get_owned_ids(
            db, ids=list({i for pair in missing for i in pair}), account_id=account_id
        )
        default_model = crud_ai_model.get_default_active_model(
            db, account_id=account_id
        )

    items: List[IssueDuplicateVerdictItem] = []
    for issue1_id, issue2_id in requested:
        key = _pair_key(account_id, issue1_id, issue2_id)
        item = IssueDuplicateVerdictItem(
            issue1_id=issue1_id, issue2_id=issue2_id, state="pending"
        )
        row = stored.get(key)
        if row is not None:
            item.state = "done"
            item.verdict = IssueDuplicate.model_validate(row)
        elif issue1_id == issue2_id or not {str(issue1_id), str(issue2_id)} <= owned:
            item.state = "not_found"
        elif default_model is None:
            item.state = "no_model"
        else:
            if body.retry:
                with _verdict_lock:
                    _verdict_failures.pop(key, None)
            code = _recent_failure(key)
            if code is not None:
                item.state = "failed"
                item.error_code = code
            elif body.analyze:
                _schedule_verdict_check(key, account_id, issue1_id, issue2_id, settings)
        items.append(item)

    return IssueDuplicateVerdictBatchResponse(
        items=items, pending=sum(1 for item in items if item.state == "pending")
    )


@router.patch(
    "/issue-duplicates/propose-resolution",
    response_model=IssueDuplicateSchema,
    tags=["Issue Duplicates"],
)
def propose_issue_duplicate_resolution(
    resolution: IssueDuplicateSuggestionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Propose a resolution for an issue duplicate."""
    existing_duplicate = crud_issue_duplicate.get_by_issue_ids(
        db,
        issue1_id=resolution.issue1_id,
        issue2_id=resolution.issue2_id,
        account_id=current_user.account_id,
    )
    if not existing_duplicate:
        raise HTTPException(status_code=404, detail="Duplicate entry not found.")

    # Check if the user has access to the project containing the issues
    # Raises an exception if not
    project_id = existing_duplicate.issue1.project_id
    accessible_projects = get_accessible_projects(db, current_user, [project_id])
    if not accessible_projects:
        raise HTTPException(
            status_code=403,
            detail="User does not have access to the project containing the issues.",
        )

    if resolution.resolution == "merge":
        if not all(
            [
                resolution.resulting_issue1_id,
                resolution.merged_title,
                resolution.merged_description,
            ]
        ):
            raise HTTPException(
                status_code=400,
                detail="Merge resolution requires resulting_issue1_id, merged_title, and merged_description.",
            )
        issue_to_update = crud_issue.get(
            db=db, id=resolution.resulting_issue1_id, account_id=current_user.account_id
        )
        if not issue_to_update:
            raise HTTPException(status_code=404, detail="Resulting issue not found.")
        update_data = {
            "title": resolution.merged_title,
            "description": resolution.merged_description,
        }
        crud_issue.update(db=db, db_obj=issue_to_update, obj_in=update_data)

    elif resolution.resolution == "deconflict":
        if not all(
            [
                resolution.deconflicted_title1,
                resolution.deconflicted_description1,
                resolution.deconflicted_title2,
                resolution.deconflicted_description2,
            ]
        ):
            raise HTTPException(
                status_code=400,
                detail="Deconflict resolution requires titles and descriptions for both issues.",
            )

        issue1_to_update = crud_issue.get(
            db=db, id=resolution.issue1_id, account_id=current_user.account_id
        )
        issue2_to_update = crud_issue.get(
            db=db, id=resolution.issue2_id, account_id=current_user.account_id
        )
        if not issue1_to_update or not issue2_to_update:
            raise HTTPException(status_code=404, detail="One or both issues not found.")

        update_data1 = {
            "title": resolution.deconflicted_title1,
            "description": resolution.deconflicted_description1,
        }
        crud_issue.update(db=db, db_obj=issue1_to_update, obj_in=update_data1)

        update_data2 = {
            "title": resolution.deconflicted_title2,
            "description": resolution.deconflicted_description2,
        }
        crud_issue.update(db=db, db_obj=issue2_to_update, obj_in=update_data2)

    db_duplicate = crud_issue_duplicate.update_resolution(
        db=db,
        issue1_id=resolution.issue1_id,
        issue2_id=resolution.issue2_id,
        resolution_in=resolution,
    )

    if not db_duplicate:
        raise HTTPException(status_code=404, detail="Issue duplicate not found.")

    return db_duplicate


@router.patch(
    "/issue-duplicates/execute-resolution",
    response_model=IssueDuplicateResolutionResponse,
    tags=["Issue Duplicates"],
)
async def execute_issue_duplicate_resolution(
    resolution: IssueDuplicateResolutionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Execute the resolution for a pair of duplicate issues."""
    issue_a = crud_issue.get(
        db=db, id=resolution.issue1_id, account_id=current_user.account_id
    )
    issue_b = crud_issue.get(
        db=db, id=resolution.issue2_id, account_id=current_user.account_id
    )

    if not issue_a or not issue_b:
        raise HTTPException(status_code=404, detail="One or both issues not found")

    resolution_type = resolution.resolution

    # Map the detailed resolution to a canonical string value.
    resolution_map = {
        "close_a": "closed",
        "close_b": "closed",
        "merge_a_to_b": "merged",
        "merge_b_to_a": "merged",
        "deconflict": "deconflicted",
        "not_a_duplicate": "not_a_duplicate",
    }
    db_resolution_value = resolution_map.get(resolution_type)

    if db_resolution_value is None:
        raise HTTPException(
            status_code=400, detail=f"Invalid resolution type: {resolution_type}"
        )

    if resolution_type == "close_a":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Closed as duplicate of issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                comment=f"Issue {issue_a.key} was closed as a duplicate of this issue."
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "close_b":
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Closed as duplicate of issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                comment=f"Issue {issue_b.key} was closed as a duplicate of this issue."
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "merge_a_to_b":
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_2_title,
                description=resolution.resulting_issue_2_description,
                comment=f"Merged content from issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Merged into and closed as duplicate of issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "merge_b_to_a":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_1_title,
                description=resolution.resulting_issue_1_description,
                comment=f"Merged content from issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Merged into and closed as duplicate of issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "deconflict":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_1_title,
                description=resolution.resulting_issue_1_description,
                comment=f"Deconflicted with issue {issue_b.key}. Title and description updated.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_2_title,
                description=resolution.resulting_issue_2_description,
                comment=f"Deconflicted with issue {issue_a.key}. Title and description updated.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "not_a_duplicate":
        # No action needed for the issues themselves, only for the duplicate record.
        pass

    else:
        raise HTTPException(status_code=400, detail="Invalid resolution type")

    # Update the issue_duplicate record
    duplicate_record = crud_issue_duplicate.get_by_issue_ids(
        db=db,
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        account_id=current_user.account_id,
    )
    if duplicate_record:
        update_data = IssueDuplicateUpdate(
            resolution=db_resolution_value,
            resolution_at=datetime.now(UTC),
            resolution_reason=resolution.resolution_reason,
        )
        crud_issue_duplicate.update(
            db=db, db_obj=duplicate_record, obj_in=update_data.dict(exclude_unset=True)
        )
    else:
        # This case should ideally not be reached if the frontend is behaving correctly.
        raise HTTPException(status_code=404, detail="Issue duplicate record not found")

    return IssueDuplicateResolutionResponse(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution=resolution.resolution,
    )


def _find_issue_duplicates_logic(
    db: Session,
    current_user: Account,
    accessible_projects: List[Project],
    similarity_threshold: float,
    limit: int,
    skip: int,
    limit_per_issue: int,
    status: Optional[str],
    resolution: Optional[str] = None,
    max_issues_per_project: int = 5000,  # New parameter with reasonable default
    max_total_pairs: int = 50000,  # New parameter to prevent memory overflow
) -> Tuple[List[DuplicateIssuePair], str]:
    """Shared logic to find potential duplicate issues within specified projects."""
    active_models = crud_embedding_model.get_active(db)
    if not active_models:
        logger.error(
            "similarity search requested, but no active embedding model found."
        )
        raise HTTPException(
            status_code=500,
            detail="similarity search cannot be performed: No active embedding model configured.",
        )
    model = active_models[0]

    all_duplicates_pairs: List[DuplicateIssuePair] = []
    reported_pairs = set()
    processed_issues = 0  # Track total processed issues

    for project in accessible_projects:
        # Limit issues per project to prevent memory overload using CRUD layer
        project_issues = crud_issue.get_for_project_with_embeddings(
            db,
            project_id=project.id,
            status=status if status and status != "all" else None,
            limit=max_issues_per_project,
            account_id=current_user.account_id,
        )

        for issue in project_issues:
            processed_issues += 1

            # Early exit if we have enough pairs
            if len(all_duplicates_pairs) >= max_total_pairs:
                logger.warning(
                    f"Reached maximum pairs limit ({max_total_pairs}), stopping early"
                )
                break

            query_embedding_vector = next(
                (
                    emb.embedding
                    for emb in issue.embeddings
                    if emb.embedding_model_id == model.id
                ),
                None,
            )

            # Skip if no embedding found
            if query_embedding_vector is None:
                continue

            try:
                similar_issue_score_tuples: List[Tuple[Issue, float]] = (
                    crud_issue_embedding.similarity_search(
                        db=db,
                        model_id=model.id,
                        query_vector=query_embedding_vector,
                        limit=min(limit_per_issue + 1, 20),  # Cap similarity results
                        project_ids=[project.id],
                        embedding_type="issue",
                        similarity=similarity_threshold,
                        status=status if status and status != "all" else None,
                        account_id=current_user.account_id,
                    )
                )
            except Exception as e:
                logger.error(
                    f"Similarity search failed for issue {issue.id}: {type(e).__name__}"
                )
                continue

            for similar_issue_obj, score in similar_issue_score_tuples:
                if similar_issue_obj.id == issue.id:
                    continue

                # Early exit if we have enough pairs
                if len(all_duplicates_pairs) >= max_total_pairs:
                    break

                id1_str = str(issue.id)
                id2_str = str(similar_issue_obj.id)
                pair_key = frozenset([id1_str, id2_str])

                if pair_key not in reported_pairs:
                    try:
                        duplicate_record = crud_issue_duplicate.get_by_issue_ids(
                            db,
                            issue1_id=id1_str,
                            issue2_id=id2_str,
                            account_id=current_user.account_id,
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to get duplicate record: {type(e).__name__}"
                        )
                        continue

                    record_resolution = (
                        duplicate_record.resolution if duplicate_record else None
                    )

                    # Apply resolution filter
                    if resolution and resolution != "all":
                        is_resolved = bool(record_resolution)
                        if resolution == "resolved" and not is_resolved:
                            continue
                        if resolution == "unresolved" and is_resolved:
                            continue

                    try:
                        duplicate_pair = DuplicateIssuePair(
                            issue1=IssueResponse(
                                id=str(issue.id),
                                external_id=issue.external_id or "",  # Handle None
                                key=issue.key or "",  # Handle None
                                organization="",
                                project="",
                                url=issue.external_url or "",
                                created_at=issue.created_at,
                                updated_at=issue.updated_at,
                                title=issue.title or "",  # Handle None
                                description=issue.description or "",
                                status=issue.status or "",
                                priority=issue.priority or "",
                                author="",
                                assignees=[],
                                labels=[],
                                comments=[],
                                project_id=project.id,
                            ),
                            issue2=IssueResponse(
                                id=str(similar_issue_obj.id),
                                external_id=similar_issue_obj.external_id
                                or "",  # Handle None
                                key=similar_issue_obj.key or "",  # Handle None
                                organization="",
                                project="",
                                url=similar_issue_obj.external_url or "",
                                created_at=similar_issue_obj.created_at,
                                updated_at=similar_issue_obj.updated_at,
                                title=similar_issue_obj.title or "",  # Handle None
                                description=similar_issue_obj.description or "",
                                status=similar_issue_obj.status or "",
                                priority=similar_issue_obj.priority or "",
                                author="",
                                assignees=[],
                                labels=[],
                                comments=[],
                                project_id=project.id,
                            ),
                            similarity=score,
                            resolution=record_resolution,
                        )
                        all_duplicates_pairs.append(duplicate_pair)
                        reported_pairs.add(pair_key)
                    except Exception as e:
                        logger.error(
                            f"Error creating issue response for duplicate pair: {type(e).__name__}"
                        )
                        continue

            del similar_issue_score_tuples

            # Sparse garbage collection - only every 1000 processed issues
            if processed_issues % 1000 == 0:
                gc.collect()

        # Early exit if we have enough pairs
        if len(all_duplicates_pairs) >= max_total_pairs:
            break

        del project_issues

    all_duplicates_pairs.sort(key=lambda x: x.similarity, reverse=True)
    paginated_duplicates = all_duplicates_pairs[skip : skip + limit]
    return paginated_duplicates, str(model.id)


@router.get("/issue-duplicates", response_model=ProjectDuplicatesResponse)
def find_issue_duplicates(
    project_ids: Optional[List[str]] = Query(
        None,
        description=(
            "Optional list of project IDs to search within. "
            "If not provided, searches across all accessible projects."
        ),
    ),
    limit: int = Query(
        5,
        ge=1,
        description="Maximum number of duplicates to return.",
    ),
    skip: int = Query(
        0, ge=0, description="Number of duplicates to skip for pagination."
    ),
    similarity_threshold: float = Query(
        0.7,
        ge=0.0,
        le=1.0,
        description="Similarity threshold for considering issues as duplicates.",
    ),
    limit_per_issue: int = Query(
        5,
        ge=1,
        le=20,
        description="Maximum number of duplicates to find for each issue.",
    ),
    status: Literal["opened", "closed", "all"] = Query(
        "opened", description="Filter issues by status."
    ),
    resolution: Literal["resolved", "unresolved", "all"] = Query(
        "all", description="Filter by resolution status."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Finds potential duplicate issues within specified projects.
    """
    accessible_projects = get_accessible_projects(
        db=db, current_user=current_user, project_ids=project_ids
    )

    paginated_duplicates, model_id_used = _find_issue_duplicates_logic(
        db=db,
        current_user=current_user,
        accessible_projects=accessible_projects,
        similarity_threshold=similarity_threshold,
        limit=limit,
        skip=skip,
        limit_per_issue=limit_per_issue,
        status=status,
        resolution=resolution,
    )

    return ProjectDuplicatesResponse(
        project_ids=[str(p.id) for p in accessible_projects],
        model_id_used=model_id_used,
        threshold_used=similarity_threshold,
        duplicates=paginated_duplicates,
    )


@router.get("/project-duplicate-stats", response_model=IssueDuplicateStats)
def get_projects_duplicate_stats(
    project_ids: Optional[List[str]] = Query(
        None,
        description="A list of project IDs to filter the statistics by. If not provided, stats for all accessible projects will be returned.",
    ),
    similarity_threshold: float = Query(
        0.95,
        ge=0.0,
        le=1.0,
        description="Similarity threshold for considering issues as duplicates.",
    ),
    status: Optional[str] = Query(None, description="Filter issues by status."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get statistics about duplicate issues for specified projects.
    """
    accessible_projects = get_accessible_projects(
        db=db, current_user=current_user, project_ids=project_ids
    )

    issue_counts = crud_issue.get_issue_counts_per_project(
        db,
        project_ids=[str(p.id) for p in accessible_projects],
        account_id=current_user.account_id,
    )

    duplicate_issue_list, _ = _find_issue_duplicates_logic(
        db=db,
        current_user=current_user,
        accessible_projects=accessible_projects,
        similarity_threshold=similarity_threshold,
        limit=1000,  # A large enough number to get all duplicates for stats
        skip=0,
        limit_per_issue=100,  # A large enough number
        status=status,
    )

    stats: Dict[str, IssueDuplicateProjectStats] = {
        str(project.id): IssueDuplicateProjectStats(
            project_id=project.id, project_name=project.name, total=0, duplicates=0
        )
        for project in accessible_projects
    }

    for pid, data in issue_counts.items():
        pid_str = str(pid)
        if pid_str in stats:
            stats[pid_str].total = data.get("total", 0)

    # Since a duplicate pair contains two issues, we need to count unique issues involved
    duplicate_issues_per_project = {p.id: set() for p in accessible_projects}
    for pair in duplicate_issue_list:
        duplicate_issues_per_project[pair.issue1.project_id].add(pair.issue1.id)
        duplicate_issues_per_project[pair.issue2.project_id].add(pair.issue2.id)

    for pid, issues in duplicate_issues_per_project.items():
        pid_str = str(pid)
        stats[pid_str].duplicates = len(issues)

    return IssueDuplicateStats(projects=stats)


@router.post(
    "/ai-suggestion",
    response_model=IssueDuplicateSuggestionResponse,
    responses={
        422: {
            "model": IssueDuplicateAiError,
            "description": (
                "No default AI model. `detail.code` is `no_default_ai_model`. "
                "`detail` is this object, not a string."
            ),
        },
        500: {
            "model": IssueDuplicateAiError,
            "description": (
                "The model call failed. `detail.code` is `ai_model_error`. "
                "`detail` is this object, not a string."
            ),
        },
    },
)
def get_resolution_suggestion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    issue1_id: str = Body(...),
    issue2_id: str = Body(...),
    resolution: str = Body(...),
    settings: Settings = Depends(get_settings),
):
    """Generate a suggestion for resolving a duplicate issue pair."""
    issue1 = crud_issue.get(db, id=issue1_id, account_id=current_user.account_id)
    issue2 = crud_issue.get(db, id=issue2_id, account_id=current_user.account_id)

    if not issue1 or not issue2:
        raise HTTPException(status_code=404, detail="One or both issues not found")

    # Authorization check
    project_id = issue1.project_id
    accessible_projects = get_accessible_projects(db, current_user, [project_id])
    if not accessible_projects:
        raise HTTPException(
            status_code=403,
            detail="User does not have access to the project containing the issues.",
        )

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_default_ai_model",
                "message": "No default AI model configured. Set one under Models.",
            },
        )

    logger.info(f"Using AI model '{default_model.model_identifier}'.")

    prompts_config = load_duplicates_prompts_config(settings.PROMPTS_FILE)
    if resolution == "merged":
        prompt_data = prompts_config.get("merge_issues_v1")
    elif resolution == "deconflicted":
        prompt_data = prompts_config.get("deconflict_issues_v1")
    else:
        raise HTTPException(
            status_code=400,
            detail="Suggestions are only available for 'merged' or 'deconflicted' resolutions.",
        )

    if not prompt_data or "system" not in prompt_data or "user" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Duplicate resolution prompt not configured."
        )

    system_prompt = prompt_data["system"]
    user_prompt_template = prompt_data["user"]

    prompt_text = user_prompt_template.format(
        title1=issue1.title,
        description1=issue1.description,
        title2=issue2.title,
        description2=issue2.description,
    )

    creds_kwargs = resolve_model_call_credentials(default_model, db=db)
    try:
        client = build_aux_openai_client(
            openai,
            default_model,
            creds_kwargs,
            include_api_base=True,
            static_key_fallback=os.getenv("OPENAI_API_KEY"),
            timeout=30.0,
            max_retries=0,
        )
    except AuxApiKeyMissingError:
        logger.error(
            f"OpenAI API key not found for model {default_model.model_identifier} or environment variable."
        )
        raise HTTPException(
            status_code=500, detail="OpenAI API key not configured."
        ) from None
    dup_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]
    aux_extras = get_aux_openai_sdk_extra_kwargs(
        default_model,
        call_site_kwargs={
            "model": default_model.model_identifier,
            "messages": dup_messages,
            "response_format": {"type": "json_object"},
        },
    )

    try:
        llm_response = call_with_aux_retry(
            lambda: client.chat.completions.create(
                model=default_model.model_identifier,
                messages=dup_messages,
                response_format={"type": "json_object"},
                **aux_extras,
            ),
            operation_name="issue_duplicate_resolution_suggestion",
            provider=getattr(default_model, "provider_name", None),
        )
        suggestion_data = json.loads(llm_response.choices[0].message.content)

        # Ensure 'explanation' is present, providing a default if it's missing.
        explanation = suggestion_data.pop("explanation", "")
        if not explanation:
            logger.warning("AI suggestion response missing 'explanation' field.")

        return IssueDuplicateSuggestionResponse(
            explanation=explanation, **suggestion_data
        )
    except openai.APIError as e:
        logger.error(f"OpenAI API call failed: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ai_model_error",
                "message": "Failed to get suggestion from AI model.",
            },
        )
    except ModelGatewayAPIError as exc:
        logger.warning("AI suggestion call did not recover: %s", type(exc).__name__)
        raise HTTPException(
            status_code=exc.status_code,
            detail="AI model is rate limited; retry later.",
            headers=exc.response_headers(),
        )
