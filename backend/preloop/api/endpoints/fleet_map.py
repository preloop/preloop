"""Single bounded read for the Fleet Map canvas.

The three reads are open to every account member: authorization is per
source inside the services (``view_agents``, ``view_ai_models``,
``view_flows``, ``view_users``, ``view_device_fleet`` for devices, plus the
resource authorizer per row), so a member sees only what they may view and
a member who may view nothing gets an empty graph naming the sources in
``unavailable`` instead of a 403.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.schemas.fleet_map import (
    FleetCodeActivityResponse,
    FleetMapResponse,
    FleetUsageResponse,
)
from preloop.services.fleet_code_activity import build_code_activity
from preloop.services.fleet_map import build_fleet_map
from preloop.services.fleet_scope import parse_users
from preloop.services.fleet_usage import build_fleet_usage

router = APIRouter()


@router.get("/fleet/map", response_model=FleetMapResponse, tags=["Fleet"])
def read_fleet_map(
    current_user: Annotated[models.User, Depends(get_current_active_user)],
    limit: int = Query(500, ge=50, le=1000),
    active_within_minutes: int | None = Query(
        None,
        ge=1,
        le=60 * 24 * 366,
        description=(
            "Show only resources with activity in this many minutes: gateway "
            "requests for models, provider accounts and providers; sessions, "
            "requests or last-seen for agents; heartbeats for devices and "
            "runners; an active holding for people."
        ),
    ),
    include_idle: bool = Query(
        False,
        description="With a window, also return configured idle resources (idle=true).",
    ),
    users: str | None = Query(
        None,
        max_length=2000,
        description=(
            "Comma separated user ids (at most 50): scope the response to those "
            "people, their devices, the agents and runners they own or that run "
            "on those devices, the models, provider accounts and providers those "
            "agents use, and the work those agents touched. Flows are omitted."
        ),
    ),
    db: Session = Depends(get_db_session),
) -> FleetMapResponse:
    """Read the selected organization's visible fleet and model dependencies."""
    return build_fleet_map(
        db,
        user=current_user,
        limit=limit,
        active_within_minutes=active_within_minutes,
        include_idle=include_idle,
        user_ids=parse_users(users),
    )


@router.get(
    "/fleet/code-activity",
    response_model=FleetCodeActivityResponse,
    tags=["Fleet"],
)
def read_fleet_code_activity(
    current_user: Annotated[models.User, Depends(get_current_active_user)],
    active_within_minutes: int | None = Query(
        None,
        ge=1,
        le=60 * 24 * 366,
        description="Window in minutes; omitted reads the last 90 days.",
    ),
    users: str | None = Query(
        None,
        max_length=2000,
        description=(
            "Comma separated user ids (at most 50): scope the response to those "
            "people, their devices, the agents and runners they own or that run "
            "on those devices, the models, provider accounts and providers those "
            "agents use, and the work those agents touched. Flows are omitted."
        ),
    ),
    db: Session = Depends(get_db_session),
) -> FleetCodeActivityResponse:
    """Pull and merge requests opened, commented, reviewed and merged per node."""
    return build_code_activity(
        db,
        user=current_user,
        active_within_minutes=active_within_minutes,
        user_ids=parse_users(users),
    )


@router.get("/fleet/usage", response_model=FleetUsageResponse, tags=["Fleet"])
def read_fleet_usage(
    current_user: Annotated[models.User, Depends(get_current_active_user)],
    active_within_minutes: int | None = Query(
        None,
        ge=1,
        le=60 * 24 * 366,
        description="Window in minutes; omitted or above 90 days reads the last 90 days.",
    ),
    users: str | None = Query(
        None,
        max_length=2000,
        description=(
            "Comma separated user ids (at most 50): scope the response to those "
            "people, their devices, the agents and runners they own or that run "
            "on those devices, the models, provider accounts and providers those "
            "agents use, and the work those agents touched. Flows are omitted."
        ),
    ),
    db: Session = Depends(get_db_session),
) -> FleetUsageResponse:
    """Gateway request, token and estimated cost totals per model and provider node."""
    return build_fleet_usage(
        db,
        user=current_user,
        active_within_minutes=active_within_minutes,
        user_ids=parse_users(users),
    )
