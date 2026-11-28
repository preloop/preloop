"""REST access to a flow's persistent state, for owners and the console.

Runs use the ``get_flow_state`` / ``put_flow_state`` MCP tools. These routes
let a person inspect what a flow remembers, correct a value, or reset it.
Keys under the reserved ``preloop.`` prefix can be read and deleted (reset)
here but not written: their shape belongs to the platform feature that owns
them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_flow
from preloop.models.crud import flow_state as crud_flow_state
from preloop.models.db.session import get_db_session as get_db
from preloop.plugins.account_hooks import VISIBLE_FLOW, filter_viewable
from preloop.schemas.flow_state import FlowStateEntry, FlowStateList, FlowStatePutBody
from preloop.services.flow_state_tools import entry
from preloop.utils.permissions import require_permission

router = APIRouter()

KEY_PATH = Path(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _flow(db: Session, user: models.User, flow_id: uuid.UUID) -> models.Flow:
    """Load a flow the user may see, or 404.

    Args:
        db: Database session.
        user: Authenticated user.
        flow_id: Flow id.

    Returns:
        The flow.

    Raises:
        HTTPException: 404 when missing or not visible.
    """
    flow = crud_flow.get(db=db, id=flow_id, account_id=user.account_id)
    if not flow or not filter_viewable(db, user, VISIBLE_FLOW, [flow]):
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.get("/flows/{flow_id}/state", response_model=FlowStateList)
@require_permission("view_flows")
def list_flow_state(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: models.User = Depends(get_current_active_user),
) -> FlowStateList:
    """List the keys a flow stores, without their values."""
    flow = _flow(db, current_user, flow_id)
    rows = crud_flow_state.list_states(db, account_id=flow.account_id, flow_id=flow.id)
    return FlowStateList(keys=[entry(row, with_value=False) for row in rows])


@router.get("/flows/{flow_id}/state/{key}", response_model=FlowStateEntry)
@require_permission("view_flows")
def get_flow_state(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    key: str = KEY_PATH,
    current_user: models.User = Depends(get_current_active_user),
) -> FlowStateEntry:
    """Read one stored value."""
    flow = _flow(db, current_user, flow_id)
    row = crud_flow_state.get_state(
        db, account_id=flow.account_id, flow_id=flow.id, key=key
    )
    if row is None:
        raise HTTPException(status_code=404, detail="State key not found")
    return entry(row)


@router.put("/flows/{flow_id}/state/{key}", response_model=FlowStateEntry)
@require_permission("edit_flows")
def put_flow_state(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    body: FlowStatePutBody,
    key: str = KEY_PATH,
    current_user: models.User = Depends(get_current_active_user),
) -> FlowStateEntry:
    """Create or replace one value (not for reserved keys)."""
    flow = _flow(db, current_user, flow_id)
    if crud_flow_state.is_reserved(key):
        raise HTTPException(
            status_code=403,
            detail="Keys under 'preloop.' are written by the platform; "
            "delete the key to reset it.",
        )
    try:
        row = crud_flow_state.put_state(
            db,
            account_id=flow.account_id,
            flow_id=flow.id,
            key=key,
            value=body.value,
            expected_version=body.expected_version,
        )
    except crud_flow_state.FlowStateError as exc:
        db.rollback()
        status = 409 if exc.code == "flow_state_version_conflict" else 422
        raise HTTPException(status_code=status, detail=f"{exc.code}: {exc}") from None
    return entry(row)


@router.delete("/flows/{flow_id}/state/{key}", status_code=204)
@require_permission("edit_flows")
def delete_flow_state(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    key: str = KEY_PATH,
    current_user: models.User = Depends(get_current_active_user),
) -> Response:
    """Delete (reset) one value, reserved keys included."""
    flow = _flow(db, current_user, flow_id)
    if not crud_flow_state.delete_state(
        db, account_id=flow.account_id, flow_id=flow.id, key=key
    ):
        raise HTTPException(status_code=404, detail="State key not found")
    return Response(status_code=204)
