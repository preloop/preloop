"""Extension content for review: collector upload, console read, account switch.

A collector that already sent a discovery report asks which content hashes the
account lacks (``POST /agents/extension-content/missing``) and uploads only
those (``POST /agents/extension-content``). Text is redacted on the device.
The server recomputes each inventory hash from the uploaded manifest, checks
unredacted text against its own hash, encrypts the document and stores it once
per account and hash.

Reading needs ``manage_agents``, the permission that gates approvals, and
writes one audit row per read. The account switch ("Collect extension content
for review", on by default) needs ``manage_policies``; turning it off deletes
what is stored.

No route here logs, echoes or puts in an error message any uploaded text or
file name. Validation errors are replaced by a fixed body for that reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Callable, Coroutine

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.endpoints.agent_discovery import (
    REPORT_PERMISSION,
    _require_report_access,
)
from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_extension_content
from preloop.models.crud.history_policy import (
    longest_history_promise,
    subscription_history_retention,
)
from preloop.models.db.session import get_db_session
from preloop.plugins.base import get_plugin_manager
from preloop.schemas.discovered_extension import HASH_PATTERN
from preloop.schemas.extension_content import (
    ContentKind,
    ContentPair,
    ContentQuery,
    ContentQueryResponse,
    ContentRejection,
    ContentUpload,
    ContentUploadResponse,
    ExtensionContentRead,
    ExtensionContentSettingsResponse,
    ExtensionContentSettingsUpdate,
)
from preloop.services import extension_content as policy
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

CONTENT_DISABLED = "extension_content_disabled"
READ_PERMISSION = "manage_agents"
SETTINGS_PERMISSION = "manage_policies"


class ContentRoute(APIRoute):
    """Keep uploaded text out of validation error responses.

    The default 422 body repeats the rejected input. For these routes that
    input can be extension text, so the error names the problem and nothing
    else.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap validation without retaining its input-bearing details."""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    detail={
                        "code": "invalid_request",
                        "message": "The request does not match the content schema.",
                    },
                    headers={"Cache-Control": "no-store"},
                ) from None

        return safe_handler


router = APIRouter(route_class=ContentRoute)


def _require_collector_access(
    db: Session, current_user: Any, account: models.Account, payload: Any
) -> None:
    """Enforce ``report_discovery`` and the credential's device binding.

    The discovery report route hands its payload to the report authorizer,
    which refuses a device-bound credential that names another device's
    fingerprint. The content routes run the same authorizer on their own
    payload: a credential bound to device A cannot ask about, or upload
    content for, device B.

    Raises:
        HTTPException: 403 without the permission or for another device's
            fingerprint, 422 when a bound credential names no fingerprint,
            503 when the credential is bound and nothing can enforce it.
    """
    _require_report_access(db, current_user)
    key = getattr(current_user, "_auth_api_key", None)
    context = getattr(key, "context_data", None)
    bound = isinstance(context, dict) and "device_report" in context
    authorizer = get_plugin_manager().get_service("discovery_report_authorizer")
    if authorizer is None:
        if bound:
            # A credential with persisted identity restrictions must not
            # become an unrestricted uploader when its plugin is absent.
            raise HTTPException(
                status_code=503, detail="Report authorization unavailable"
            )
        return
    if bound and getattr(payload, "workstation_fingerprint", None) is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": "A device credential must name its workstation.",
            },
        )
    authorizer.authorize_report(
        db, account_id=account.id, current_user=current_user, payload=payload
    )


def _retention_days(db: Session, account: models.Account) -> int:
    plan_days = longest_history_promise(
        account.subscription_history_retention_days,
        subscription_history_retention(db, account_id=account.id),
    )
    return policy.retention_days(plan_days)


@router.post("/agents/extension-content/missing", response_model=ContentQueryResponse)
@require_permission(REPORT_PERMISSION)
def query_missing_content(
    payload: ContentQuery,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ContentQueryResponse:
    """Tell a collector whether to send content and which hashes are lacking.

    When the account has turned collection off the answer is ``collect:
    false`` with reason ``account_opt_out`` and an empty list, so a collector
    sends nothing and can say why.
    """
    _require_collector_access(db, current_user, account, payload)
    retention = _retention_days(db, account)
    if not policy.is_enabled(account):
        return ContentQueryResponse(
            collect=False,
            reason="account_opt_out",
            missing=[],
            retention_days=retention,
        )
    missing = crud_extension_content.missing(
        db,
        account_id=account.id,
        pairs=[(item.extension_kind, item.content_hash) for item in payload.items],
    )
    db.commit()
    return ContentQueryResponse(
        collect=True,
        missing=[
            ContentPair(extension_kind=kind, content_hash=content_hash)  # type: ignore[arg-type]
            for kind, content_hash in missing
        ],
        retention_days=retention,
    )


@router.post(
    "/agents/extension-content",
    response_model=ContentUploadResponse,
    responses={
        409: {"description": "extension_content_disabled: the account opted out"},
        413: {"description": "The batch holds more text than one upload may"},
    },
)
@require_permission(REPORT_PERMISSION)
def upload_content(
    payload: ContentUpload,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ContentUploadResponse:
    """Store redacted extension content for hashes the inventory reports.

    Each item is checked on its own: a rejected item is listed with a fixed
    reason code and the rest of the batch is still stored.
    """
    _require_collector_access(db, current_user, account, payload)
    # Under the account lock: an opt-out cannot slip in between this check and
    # the commit, so nothing is stored for an account that turned it off.
    account = crud_extension_content.lock_account(db, account_id=account.id)
    if not policy.is_enabled(account):
        raise HTTPException(
            status_code=409,
            detail={
                "code": CONTENT_DISABLED,
                "message": (
                    "This account does not collect extension content. "
                    "Nothing was stored."
                ),
            },
        )
    total = sum(
        len((entry.text or "").encode("utf-8"))
        for item in payload.items
        for entry in item.files
    ) + sum(
        len((hook.command or "").encode("utf-8"))
        + len((hook.matcher or "").encode("utf-8"))
        for item in payload.items
        for hook in item.hooks
    )
    if total > policy.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "upload_too_large", "message": "Send smaller batches."},
        )
    reported = crud_extension_content.reported_pairs(
        db,
        account_id=account.id,
        workstation_fingerprint=payload.workstation_fingerprint,
        pairs=[(item.extension_kind, item.content_hash) for item in payload.items],
    )
    used = crud_extension_content.account_bytes(db, account_id=account.id)
    stored = kept = 0
    rejected: list[ContentRejection] = []

    def reject(item: Any, reason: str) -> None:
        rejected.append(
            ContentRejection(
                extension_kind=item.extension_kind,
                content_hash=item.content_hash,
                reason=reason,
            )
        )

    for item in payload.items:
        if (item.extension_kind, item.content_hash) not in reported:
            reject(item, policy.REJECT_NOT_REPORTED)
            continue
        try:
            verified = policy.verify_item(item.model_dump(mode="json"))
            was_stored, byte_delta = crud_extension_content.store(
                db,
                account_id=account.id,
                extension_kind=item.extension_kind,
                content_hash=item.content_hash,
                item=verified,
                workstation_fingerprint=payload.workstation_fingerprint,
                collector_version=payload.collector_version,
                redaction_rules=payload.redaction_rules,
                available_bytes=policy.MAX_ACCOUNT_BYTES - used,
            )
        except policy.ContentRejectedError as exc:
            reject(item, exc.code)
            continue
        used += byte_delta
        if was_stored:
            stored += 1
        else:
            kept += 1
    db.commit()
    return ContentUploadResponse(stored=stored, kept=kept, rejected=rejected)


@router.get(
    "/agents/extension-content/{extension_kind}/{content_hash}",
    response_model=ExtensionContentRead,
    responses={404: {"description": "No content stored for this hash"}},
)
@require_permission(READ_PERMISSION)
def read_content(
    response: Response,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    extension_kind: ContentKind = Path(),
    content_hash: str = Path(pattern=HASH_PATTERN),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionContentRead:
    """Read the stored content of one hash. Every read is audited.

    The audit row names the reader, the kind and the hash. It holds no text
    and no file name.
    """
    ensure_permission_in_oss(db, current_user, READ_PERMISSION)
    response.headers["Cache-Control"] = "no-store"
    row = crud_extension_content.get(
        db,
        account_id=account.id,
        extension_kind=extension_kind,
        content_hash=content_hash,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No content stored for this hash")
    try:
        document = crud_extension_content.document(row)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Stored content cannot be decrypted"
        ) from exc
    result = ExtensionContentRead(
        extension_kind=row.extension_kind,  # type: ignore[arg-type]
        content_hash=row.content_hash,
        files=document.get("files") or [],
        hooks=document.get("hooks") or [],
        matcher_sets=document.get("matcher_sets") or [],
        withheld=document.get("withheld"),
        verification=row.verification,  # type: ignore[arg-type]
        redacted=row.redacted,
        redaction_count=row.redaction_count,
        withheld_count=row.withheld_count,
        stored_bytes=row.stored_bytes,
        redaction_rules=row.redaction_rules,
        collector_version=row.collector_version,
        first_stored_at=row.created_at,
        last_reported_at=row.last_reported_at,
        retention_days=_retention_days(db, account),
    )
    crud_audit_log.log_action(
        db,
        account_id=account.id,
        user_id=current_user.id,
        action=policy.AUDIT_READ,
        resource_type="extension_content",
        resource_id=row.content_hash,
        status="success",
        details={"extension_kind": row.extension_kind},
        commit=False,
    )
    db.commit()
    return result


def _settings_response(
    db: Session, account: models.Account
) -> ExtensionContentSettingsResponse:
    resolved = policy.resolve(account.meta_data)
    count, size = crud_extension_content.summary(db, account_id=account.id)
    return ExtensionContentSettingsResponse(
        collect_enabled=resolved.collect_enabled,
        notice_pending=resolved.notice_acknowledged_at is None,
        stored_contents=count,
        stored_bytes=size,
        retention_days=_retention_days(db, account),
        updated_by_user_id=resolved.updated_by_user_id,
        updated_at=resolved.updated_at,
    )


@router.get(
    "/account/extension-content/settings",
    response_model=ExtensionContentSettingsResponse,
)
@require_permission("view_agents")
def get_content_settings(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionContentSettingsResponse:
    """Return the switch, whether the notice is pending, and what is stored."""
    ensure_permission_in_oss(db, current_user, "view_agents")
    return _settings_response(db, account)


@router.put(
    "/account/extension-content/settings",
    response_model=ExtensionContentSettingsResponse,
    responses={403: {"description": "Caller may not manage account policies"}},
)
@require_permission(SETTINGS_PERMISSION)
def update_content_settings(
    payload: ExtensionContentSettingsUpdate,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionContentSettingsResponse:
    """Turn collection on or off, or acknowledge the one-time notice.

    Turning collection off deletes every content stored for the account in
    the same transaction and records how many rows went in the audit row.
    Collectors are told on their next report and send nothing.
    """
    ensure_permission_in_oss(db, current_user, SETTINGS_PERMISSION)
    account = crud_extension_content.lock_account(db, account_id=account.id)
    before = policy.resolve(account.meta_data)
    now = datetime.now(UTC)
    updated = policy.apply_update(
        account.meta_data,
        collect_enabled=payload.collect_enabled,
        acknowledge_notice=payload.acknowledge_notice,
        user_id=current_user.id,
        now=now,
    )
    after = policy.resolve(updated)
    switched = before.collect_enabled != after.collect_enabled
    acknowledged = (
        before.notice_acknowledged_at is None
        and after.notice_acknowledged_at is not None
    )
    if not switched and not acknowledged:
        return _settings_response(db, account)
    crud_extension_content.save_settings(db, account=account, meta_data=updated)
    if switched:
        deleted = 0
        if not after.collect_enabled:
            deleted = crud_extension_content.delete_for_account(
                db, account_id=account.id
            )
        crud_audit_log.log_action(
            db,
            account_id=account.id,
            user_id=current_user.id,
            action=policy.AUDIT_SETTINGS_UPDATED,
            resource_type="account_settings",
            resource_id=policy.META_KEY,
            status="success",
            details={
                "changed": {
                    "collect_enabled": {
                        "from": before.collect_enabled,
                        "to": after.collect_enabled,
                    }
                },
                "deleted_contents": deleted,
            },
            commit=False,
        )
        if deleted:
            crud_audit_log.log_action(
                db,
                account_id=account.id,
                user_id=current_user.id,
                action=policy.AUDIT_DELETED,
                resource_type="extension_content",
                resource_id="account",
                status="success",
                details={"reason": "account_opt_out", "deleted_contents": deleted},
                commit=False,
            )
    else:
        crud_audit_log.log_action(
            db,
            account_id=account.id,
            user_id=current_user.id,
            action=policy.AUDIT_NOTICE_ACKNOWLEDGED,
            resource_type="account_settings",
            resource_id=policy.META_KEY,
            status="success",
            details={},
            commit=False,
        )
    db.commit()
    db.refresh(account)
    return _settings_response(db, account)
