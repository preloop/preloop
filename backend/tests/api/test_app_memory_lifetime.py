"""Application factories must not retain earlier applications in route caches."""

import gc
import weakref

import pytest
from fastapi.testclient import TestClient

from preloop.api.app import create_app


def test_application_can_be_collected_after_factory_returns() -> None:
    """Cached route callable classification must not hold its application."""
    references = [weakref.ref(create_app()) for _ in range(3)]

    gc.collect()

    assert all(reference() is None for reference in references)


@pytest.mark.parametrize("path", ["/docs/api", "/docs/redoc"])
def test_documentation_uses_the_serving_application(path: str) -> None:
    """Two applications retain their own documentation configuration."""
    first = create_app()
    second = create_app()
    first.title = "First application"
    second.title = "Second application"
    first.openapi_url = "/first/openapi.json"
    second.openapi_url = "/second/openapi.json"

    for app, title, url in [
        (first, first.title, first.openapi_url),
        (second, second.title, second.openapi_url),
    ]:
        response = TestClient(app).get(path)
        assert response.status_code == 200
        assert title in response.text
        assert url in response.text


@pytest.mark.parametrize("path", ["/api/v1/openapi.yaml", "/api/v1/spec"])
def test_yaml_documentation_uses_the_serving_application(path: str) -> None:
    """Schema aliases resolve the application attached to the request."""
    app = create_app()
    app.openapi = lambda: {"info": {"title": "Custom application schema"}}

    response = TestClient(app).get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-yaml")
    assert "Custom application schema" in response.text
