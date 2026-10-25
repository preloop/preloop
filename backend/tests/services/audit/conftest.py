"""Fixtures for the core audit API and collector tests."""

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import audit
from preloop.models import models
from preloop.models.db.session import get_db_session


@pytest.fixture
def audit_app(
    db_session: Session, test_user: models.User
) -> Generator[FastAPI, None, None]:
    app = create_app()
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    app.dependency_overrides[get_db_session] = lambda: db_session
    yield app


@pytest.fixture
def audit_client(audit_app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(audit_app) as client:
        yield client


@pytest.fixture(autouse=True)
def fresh_overview_cache() -> Generator[None, None, None]:
    audit.clear_overview_cache()
    yield
    audit.clear_overview_cache()
