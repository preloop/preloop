"""Retention settings, legal holds and the period export.

Three surfaces that only make sense together. The settings say how long
records are kept, the holds say which records are exempt while a matter is
open, and the export is how a period leaves the platform before retention
catches up with it.

Every handler here is a plain ``def``. They all hold a synchronous session,
and FastAPI dispatches sync handlers on the anyio threadpool, so a wait for a
pool connection blocks a worker thread rather than the event loop
(``tests/api/test_event_loop_pool_wait.py`` ratchets that count).

Permissions reuse ``view_policies`` / ``manage_policies`` for settings and
holds, and ``view_audit_logs`` for the export, which is a bulk read of the
audit trail. A new permission would need seeding in the EE role matrix, which
is not part of this change; the same choice was made for outbound webhooks.

The export streams a tar built in memory. That is deliberate and bounded:
``RETENTION_EXPORT_MAX_ROWS`` per record class, and going over is a 413
telling the caller to narrow the period rather than a truncated archive
somebody later mistakes for the whole period.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.config import settings
from preloop.models.crud import crud_audit_log
from preloop.models.crud import legal_hold as crud_legal_hold
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.retention import (
    LegalHoldCreate,
    LegalHoldRead,
    LegalHoldRelease,
    RetentionClassRead,
    RetentionPurgePreview,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
)
from preloop.services import retention_policy
from preloop.services.legal_hold import (
    HoldOutcome,
    LegalHoldError,
    hold_summary,
    place_hold,
    release_hold,
)
from preloop.services.retention_export import (
    PeriodExportError,
    audit_period_export,
    build_period_export,
)
from preloop.services.retention_purge import count_purgeable
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retention", tags=["Retention"])

VIEW_PERMISSION = "view_policies"
MANAGE_PERMISSION = "manage_policies"
EXPORT_PERMISSION = "view_audit_logs"

AUDIT_ACTION_SETTINGS = "retention_settings_updated"

#: One year of days. A period export is a compliance artifact, not a data
#: dump: a caller who wants five years takes five archives, and each one stays
#: small enough to hash and store.
MAX_PERIOD_DAYS = 366


def _settings_payload(account: Account) -> RetentionSettingsRead:
    """Resolved retention plus the deployment facts that constrain it."""
    resolved = retention_policy.resolve_all(account.meta_data)
    return RetentionSettingsRead(
        floor_days=retention_policy.floor_days(),
        default_days=retention_policy.default_days(),
        max_days=retention_policy.MAX_RETENTION_DAYS,
        classes=[
            RetentionClassRead(
                record_class=record_class,
                label=retention_policy.RECORD_CLASS_LABELS[record_class],
                days=setting.days,
                source=setting.source,
                floored=setting.floored,
            )
            for record_class, setting in resolved.items()
        ],
        purge_enabled=bool(settings.retention_purge_enabled),
        purge_dry_run=bool(settings.retention_purge_dry_run),
        purge_window_utc=settings.retention_purge_window_utc or None,
        evidence_payload_hours=int(settings.flow_evidence_retention_hours),
    )


def _hold_read(outcome_or_hold, flagged: Optional[dict] = None) -> LegalHoldRead:
    """Render a hold row, optionally with what a mutation just changed."""
    if isinstance(outcome_or_hold, HoldOutcome):
        summary = hold_summary(outcome_or_hold.hold)
        flagged = outcome_or_hold.flagged
    else:
        summary = hold_summary(outcome_or_hold)
    return LegalHoldRead(**summary, flagged=flagged)


def _parse_day(value: str, field: str) -> datetime:
    """Accept a date or a full timestamp, always land in UTC."""
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} is required (YYYY-MM-DD)",
        )
    try:
        if len(raw) == 10:
            parsed = datetime.combine(
                datetime.strptime(raw, "%Y-%m-%d").date(), time.min
            )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be YYYY-MM-DD or an ISO 8601 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@router.get("/settings", response_model=RetentionSettingsRead)
@require_permission(VIEW_PERMISSION)
def get_retention_settings(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Return this account's retention per record class, and the floor."""
    return _settings_payload(account)


@router.put("/settings", response_model=RetentionSettingsRead)
@require_permission(MANAGE_PERMISSION)
def update_retention_settings(
    payload: RetentionSettingsUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Set retention per record class, never below the six month floor.

    The body is the full desired state: a class the caller omits, or sets to
    null, goes back to the deployment default.
    """
    before = {
        record_class: setting.days
        for record_class, setting in retention_policy.resolve_all(
            account.meta_data
        ).items()
    }
    try:
        updated = retention_policy.set_retention(
            account.meta_data, values=payload.classes
        )
    except retention_policy.RetentionFloorError as exc:
        # 422 rather than 400: the value is well formed and refused on
        # substance, and the message names the floor so the console can show
        # it without a second round trip.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    account.meta_data = updated
    flag_modified(account, "meta_data")
    db.add(account)
    after = {
        record_class: setting.days
        for record_class, setting in retention_policy.resolve_all(updated).items()
    }
    changed = {
        record_class: {"from": before[record_class], "to": days}
        for record_class, days in after.items()
        if before.get(record_class) != days
    }
    crud_audit_log.log_action(
        db,
        account_id=account.id,
        user_id=current_user.id,
        action=AUDIT_ACTION_SETTINGS,
        resource_type="retention",
        resource_id="settings",
        status="success",
        details={"changed": changed, "floor_days": retention_policy.floor_days()},
        commit=False,
    )
    db.commit()
    db.refresh(account)
    return _settings_payload(account)


@router.get("/purge-preview", response_model=RetentionPurgePreview)
@require_permission(VIEW_PERMISSION)
def preview_retention_purge(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Count what the purge would remove today, without removing anything.

    Nobody should discover the effect of a retention setting by watching rows
    disappear. Rows under a legal hold are already excluded by the same
    filters the purge uses, so the count is the count.
    """
    now = datetime.now(UTC)
    rows = []
    total = 0
    for record_class, setting in retention_policy.resolve_all(
        account.meta_data
    ).items():
        cutoff = now - timedelta(days=setting.days)
        count = count_purgeable(
            db, account_id=account.id, record_class=record_class, cutoff=cutoff
        )
        total += count
        rows.append(
            {
                "record_class": record_class,
                "label": retention_policy.RECORD_CLASS_LABELS[record_class],
                "retention_days": setting.days,
                "cutoff": cutoff.isoformat(),
                "purgeable": count,
            }
        )
    return RetentionPurgePreview(
        account_id=account.id,
        purge_enabled=bool(settings.retention_purge_enabled),
        classes=rows,
        total=total,
    )


@router.get("/holds", response_model=List[LegalHoldRead])
@require_permission(VIEW_PERMISSION)
def list_legal_holds(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    active_only: bool = Query(True, description="Hide released holds"),
    resource_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """List this account's legal holds, newest first."""
    rows = crud_legal_hold.list_for_account(
        db,
        account_id=account.id,
        active_only=active_only,
        resource_type=resource_type,
        limit=limit,
    )
    return [_hold_read(row) for row in rows]


@router.post(
    "/holds", response_model=LegalHoldRead, status_code=status.HTTP_201_CREATED
)
@require_permission(MANAGE_PERMISSION)
def create_legal_hold(
    payload: LegalHoldCreate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Freeze one execution, approval or evidence pack until released."""
    try:
        outcome = place_hold(
            db,
            account_id=account.id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            reason=payload.reason,
            user_id=current_user.id,
        )
    except LegalHoldError as exc:
        db.rollback()
        codes = {
            "resource_not_found": status.HTTP_404_NOT_FOUND,
            "already_held": status.HTTP_409_CONFLICT,
        }
        raise HTTPException(
            status_code=codes.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        ) from exc
    return _hold_read(outcome)


@router.post("/holds/{hold_id}/release", response_model=LegalHoldRead)
@require_permission(MANAGE_PERMISSION)
def release_legal_hold(
    hold_id: UUID,
    payload: LegalHoldRelease,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Lift a hold. The record stays, with who lifted it and why."""
    try:
        outcome = release_hold(
            db,
            account_id=account.id,
            hold_id=hold_id,
            reason=payload.reason,
            user_id=current_user.id,
        )
    except LegalHoldError as exc:
        db.rollback()
        codes = {
            "hold_not_found": status.HTTP_404_NOT_FOUND,
            "already_released": status.HTTP_409_CONFLICT,
        }
        raise HTTPException(
            status_code=codes.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        ) from exc
    return _hold_read(outcome)


@router.post("/exports", response_class=Response)
@require_permission(EXPORT_PERMISSION)
def create_period_export(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start: str = Query(..., description="Period start, inclusive (YYYY-MM-DD)"),
    end: str = Query(..., description="Period end, exclusive (YYYY-MM-DD)"),
):
    """Build a tar.gz of one period: audit rows, approvals, receipts, holds.

    The archive carries ``manifest.json`` with a sha256 per member and a
    digest over the member list, in the same shape an evidence pack manifest
    uses, so one verifier covers both, plus ``signature.json``: a detached
    Ed25519 signature over the manifest bytes, checkable against the account's
    published public key.
    """
    period_start = _parse_day(start, "start")
    period_end = _parse_day(end, "end")
    if period_end <= period_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must be after start",
        )
    if (period_end - period_start).days > MAX_PERIOD_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"a period export covers at most {MAX_PERIOD_DAYS} days; "
                "take consecutive periods for a longer range"
            ),
        )
    try:
        export = build_period_export(
            db, account=account, start=period_start, end=period_end
        )
    except PeriodExportError as exc:
        codes = {
            "period_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
            "invalid_period": status.HTTP_400_BAD_REQUEST,
        }
        raise HTTPException(
            status_code=codes.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        ) from exc
    audit_period_export(
        db, account_id=account.id, user_id=current_user.id, export=export
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{export.filename}"',
        # The digest of the bytes served, so a caller can check the
        # download before they file it.
        "X-Preloop-Archive-Sha256": export.sha256,
        "X-Preloop-Members-Digest": str(export.manifest.get("members_digest") or ""),
        # What the signature covers, so a caller who has only the headers can
        # still tell which digest to check (#558).
        "X-Preloop-Manifest-Sha256": export.manifest_sha256,
    }
    if export.signature:
        headers["X-Preloop-Signature"] = str(export.signature.get("signature") or "")
        headers["X-Preloop-Signing-Key-Id"] = str(export.signature.get("key_id") or "")
        headers["X-Preloop-Signed-At"] = str(export.signature.get("signed_at") or "")
    return Response(
        content=export.archive,
        media_type="application/gzip",
        headers=headers,
    )
