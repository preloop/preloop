"""Fleet Map routing, validation and report-credential authorization."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints.fleet_map import router
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.schemas.fleet_map import FleetMapResponse


def test_route_validates_bounds_and_returns_schema() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    with patch(
        "preloop.api.endpoints.fleet_map.build_fleet_map",
        return_value=FleetMapResponse(),
    ) as build:
        with TestClient(app) as client:
            for limit in (0, 49, 1001):
                assert client.get(f"/api/v1/fleet/map?limit={limit}").status_code == 422
            build.assert_not_called()
            response = client.get("/api/v1/fleet/map")
            assert response.status_code == 200
            assert response.json()["schema_version"] == 1
            assert response.json()["nodes"] == []
            assert build.call_args.kwargs["user"] is user


def test_route_forwards_the_activity_window() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    with patch(
        "preloop.api.endpoints.fleet_map.build_fleet_map",
        return_value=FleetMapResponse(),
    ) as build:
        with TestClient(app) as client:
            assert (
                client.get("/api/v1/fleet/map?active_within_minutes=0").status_code
                == 422
            )
            assert client.get("/api/v1/fleet/map").status_code == 200
            assert build.call_args.kwargs["active_within_minutes"] is None
            response = client.get("/api/v1/fleet/map?active_within_minutes=168")
            assert response.status_code == 200
            assert build.call_args.kwargs["active_within_minutes"] == 168
