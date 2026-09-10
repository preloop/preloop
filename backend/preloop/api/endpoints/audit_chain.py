"""The audit chain surface and the account's signing keys (#558).

Two routers, one file, because they are one feature: the chain is what the
signatures anchor to and the keys are what makes a checkpoint mean anything
outside this database.

The design choice worth naming is ``/audit/chain/segment``. A verification
endpoint that returns only its own verdict is worth exactly the trust the
caller already places in the server, which for a tamper evidence feature is
the wrong amount. The segment endpoint hands back the canonical payload and
the stored hashes so ``preloop audit verify`` recomputes every hash locally
and can disagree with us.

Permissions follow #559 rather than inventing a fourth one: ``view_audit_logs``
for reading the chain, since it is the audit trail, and ``manage_policies``
for rotating a key, since that is an account wide security setting. A new
permission would need seeding in the EE role matrix, which is not part of
this change.

Handlers are plain ``def``: they hold a synchronous session, and FastAPI
dispatches sync handlers on the anyio threadpool, so a wait for a pool
connection blocks a worker thread rather than the event loop.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.crud import crud_audit_log
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.audit_chain import (
    ChainCheckpointRead,
    ChainSegmentRead,
    ChainStatusRead,
    ChainVerifyRead,
    SigningKeyListRead,
    SigningKeyRotateRead,
)
from preloop.services import audit_chain, record_signing
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["Audit chain"])
signing_router = APIRouter(prefix="/signing", tags=["Signing keys"])

VIEW_PERMISSION = "view_audit_logs"
MANAGE_PERMISSION = "manage_policies"

AUDIT_ACTION_VERIFY = "audit_chain_verified"
AUDIT_ACTION_KEY_ROTATED = "signing_key_rotated"

#: Documented once, served with the keys, so a verifier never has to read our
#: source to know what a signature covers.
SIGNED_BYTES_FORMAT = "preloop.signature/v1\\n<payload_type>\\n<digest>\\n<signed_at>"


@router.get("/chain/status", response_model=ChainStatusRead)
@require_permission(VIEW_PERMISSION)
def get_chain_status(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Head, purge floor, sealing lag and the newest signed checkpoint."""
    return audit_chain.chain_status(db, account_id=account.id)


@router.get("/chain/verify", response_model=ChainVerifyRead)
@require_permission(VIEW_PERMISSION)
def verify_chain(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start_seq: Optional[int] = Query(
        None, ge=1, description="First sequence to check. Defaults to the purge floor."
    ),
    end_seq: Optional[int] = Query(
        None, ge=1, description="Last sequence to check. Defaults to the head."
    ),
    max_rows: Optional[int] = Query(
        None, ge=1, description="Cut the range at this many rows and say so."
    ),
):
    """Walk a range of the chain and report the first break.

    The range matters: rows below the purge floor are gone under a stated
    retention policy and rows written since the last seal are not chained
    yet. Claiming to verify either would be a lie of scope.

    The verification is itself audited. A read of the audit trail that leaves
    no trace is a gap in the audit trail.
    """
    report = audit_chain.verify_chain(
        db,
        account_id=account.id,
        start_seq=start_seq,
        end_seq=end_seq,
        max_rows=max_rows,
    )
    try:
        crud_audit_log.log_action(
            db,
            account_id=account.id,
            user_id=current_user.id,
            action=AUDIT_ACTION_VERIFY,
            resource_type="audit_chain",
            resource_id="verify",
            status="success"
            if report["status"] != audit_chain.STATUS_BROKEN
            else "failure",
            details={
                "chain_status": report["status"],
                "start_seq": report["start_seq"],
                "end_seq": report["end_seq"],
                "checked_rows": report["checked_rows"],
                "first_break": report["first_break"],
                "checkpoint_failures": len(report["checkpoint_failures"]),
            },
        )
    except Exception:
        db.rollback()
        logger.error("Failed to audit a chain verification", exc_info=True)
    return report


@router.get("/chain/segment", response_model=ChainSegmentRead)
@require_permission(VIEW_PERMISSION)
def get_chain_segment(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    after_seq: int = Query(0, ge=0, description="Exclusive lower bound"),
    limit: int = Query(500, ge=1, le=1000),
):
    """Canonical payloads and stored hashes, for a client side walk."""
    return audit_chain.chain_segment(
        db, account_id=account.id, after_seq=after_seq, limit=limit
    )


@router.get("/chain/checkpoints", response_model=list[ChainCheckpointRead])
@require_permission(VIEW_PERMISSION)
def list_chain_checkpoints(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    after_seq: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
):
    """Signed checkpoints in sequence order.

    Worth keeping your own copies of. A checkpoint you stored last quarter is
    the only thing here that a rewritten chain cannot reproduce.
    """
    return audit_chain.list_checkpoints(
        db, account_id=account.id, after_seq=after_seq, limit=limit
    )


@signing_router.get("/keys", response_model=SigningKeyListRead)
@require_permission(VIEW_PERMISSION)
def list_signing_keys(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Public halves of every key this account has held.

    Retired keys stay listed: every signature they made is still valid, and a
    verifier holding a bundle from last year needs the key that signed it.
    """
    keys = record_signing.list_keys(db, account_id=account.id)
    active = next((key for key in keys if key.retired_at is None), None)
    return {
        "active_key_id": active.key_id if active else None,
        "signature_schema": record_signing.SIGNATURE_SCHEMA,
        "signed_bytes_format": SIGNED_BYTES_FORMAT,
        "keys": [record_signing.public_key_summary(key) for key in keys],
    }


@signing_router.post(
    "/keys/rotate",
    response_model=SigningKeyRotateRead,
    status_code=status.HTTP_201_CREATED,
)
@require_permission(MANAGE_PERMISSION)
def rotate_signing_key(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Retire the active key and mint its replacement.

    Signatures made by the retired key keep verifying. Rotation that
    invalidated them would revoke the customer's own evidence, which is the
    opposite of the point.
    """
    try:
        retired, active = record_signing.rotate_key(
            db, account_id=account.id, user_id=current_user.id
        )
    except Exception as exc:
        db.rollback()
        logger.error("Signing key rotation failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not rotate the signing key",
        ) from exc
    try:
        crud_audit_log.log_action(
            db,
            account_id=account.id,
            user_id=current_user.id,
            action=AUDIT_ACTION_KEY_ROTATED,
            resource_type="signing_key",
            resource_id=active.key_id,
            status="success",
            details={
                "retired_key_id": retired.key_id if retired else None,
                "new_key_id": active.key_id,
                "algorithm": active.algorithm,
            },
        )
    except Exception:
        db.rollback()
        logger.error("Failed to audit a signing key rotation", exc_info=True)
    return {
        "retired": record_signing.public_key_summary(retired) if retired else None,
        "active": record_signing.public_key_summary(active),
    }
