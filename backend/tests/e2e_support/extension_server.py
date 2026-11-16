"""Loopback-only extension API rig with a synthetic identity and real PostgreSQL.

Only identity resolution is supplied by the fixture. Collection, report schema,
CRUD, transactions and approval endpoints are production code. No model provider
or external service is contacted. Run via playwright.extensions.config.ts.
"""

import os
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.endpoints import agent_discovery, extension_inventory
from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.db.session import get_db_session


def create_fixture_app() -> FastAPI:
    """Seed a synthetic account in an explicitly selected local test database."""
    if os.environ.get("PRELOOP_DISABLE_TELEMETRY") != "true":
        raise RuntimeError("Test telemetry must be disabled")
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"127.0.0.1", "localhost"} or "extension_inventory" not in (
        url.database or ""
    ):
        raise RuntimeError("Use an isolated local extension_inventory test database")
    sessions = get_db_session()
    session = next(sessions)
    try:
        account = crud_account.create(
            session, obj_in={"organization_name": "Example extension account"}
        )
        identity = uuid4().hex
        user = crud_user.create(
            session,
            obj_in={
                "account_id": account.id,
                "username": f"extension-{identity}",
                "email": f"extension-{identity}@example.com",
                "is_active": True,
                "email_verified": True,
                "hashed_password": "unused-fixture-password",
                "user_source": "local",
            },
        )
        user_id = user.id
        crud_account.update(
            session, db_obj=account, obj_in={"primary_user_id": user.id}
        )
        session.commit()
    finally:
        sessions.close()

    def fixture_user(
        authorization: Annotated[str, Header()],
        db: Session = Depends(get_db_session),
    ) -> models.User:
        """Resolve only the harness's public synthetic bearer identity."""
        if authorization != "Bearer synthetic-extension-identity":
            raise HTTPException(status_code=401)
        user = crud_user.get(db, id=user_id)
        if user is None:
            raise HTTPException(status_code=401)
        return user

    app = FastAPI()
    app.dependency_overrides[get_current_active_user] = fixture_user
    app.include_router(agent_discovery.router, prefix="/api/v1")
    app.include_router(extension_inventory.router, prefix="/api/v1")

    @app.get("/health")
    def health() -> dict[str, str]:
        """Signal readiness without exercising inventory."""
        return {"status": "ok"}

    @app.get("/api/v1/auth/users/me")
    def profile(user: models.User = Depends(fixture_user)) -> dict[str, object]:
        """Supply the account permissions consumed by the real console component."""
        return {
            "id": str(user.id),
            "username": user.username,
            "is_account_owner": True,
            "permissions": ["view_agents", "manage_agents"],
        }

    return app
