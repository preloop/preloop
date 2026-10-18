"""Single bounded read for the Fleet Map canvas."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.schemas.fleet_map import FleetMapResponse
from preloop.services.fleet_map import build_fleet_map
from preloop.utils.permissions import require_permission

router = APIRouter()


@router.get("/fleet/map", response_model=FleetMapResponse, tags=["Fleet"])
@require_permission("view_device_fleet")
def read_fleet_map(
    current_user: Annotated[models.User, Depends(get_current_active_user)],
    limit: int = Query(500, ge=50, le=1000),
    db: Session = Depends(get_db_session),
) -> FleetMapResponse:
    """Read the selected organization's visible fleet and model dependencies."""
    return build_fleet_map(db, user=current_user, limit=limit)
