"""Search query schema metadata preserves the existing HTTP behavior."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import comments, issues, search


@pytest.fixture
def search_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()
    user = MagicMock()
    db = MagicMock()
    for module in (comments, issues, search):
        app.include_router(module.router)
        app.dependency_overrides[module.get_db] = lambda: db
        app.dependency_overrides[module.get_current_active_user] = lambda: user
    for module in (comments, issues):
        monkeypatch.setattr(
            module.crud_tracker, "get_for_account", lambda *a, **kw: [MagicMock()]
        )
    issue_query = MagicMock()
    issue_query.filter.return_value = issue_query
    issue_query.limit.return_value = issue_query
    issue_query.all.return_value = []
    monkeypatch.setattr(
        issues.crud_issue, "get_for_trackers", lambda *a, **kw: issue_query
    )
    monkeypatch.setattr(search, "get_accessible_projects", lambda *a, **kw: [])
    monkeypatch.setattr(
        comments.crud_embedding_model, "get_active", lambda *a, **kw: []
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("path", "mode", "expected_status"),
    [
        ("/comments/search", None, 200),
        ("/comments/search", "fulltext", 200),
        ("/comments/search", "similarity", 500),
        ("/issues/search", None, 200),
        ("/issues/search", "fulltext", 200),
        ("/issues/search", "similarity", 500),
        ("/search", None, 501),
        ("/search", "fulltext", 501),
        ("/search", "similarity", 500),
    ],
)
def test_search_modes_reach_existing_handler_behavior(
    search_client: TestClient, path: str, mode: str | None, expected_status: int
) -> None:
    # Exercise real handlers: generic fulltext is unimplemented, and similarity
    # reports the missing embedding model instead of a parameter-validation error.
    params = {"query": "example"}
    if mode is not None:
        params["search_type"] = mode
    response = search_client.get(path, params=params)
    assert response.status_code == expected_status, response.text


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        (
            "/comments/search",
            "Invalid search_type specified. Must be 'fulltext' or 'similarity'.",
        ),
        (
            "/issues/search",
            "Invalid search type: unsupported. Must be 'fulltext' or 'similarity'.",
        ),
        (
            "/search",
            "Invalid search_type: 'unsupported'. Must be 'similarity' or 'fulltext'.",
        ),
    ],
)
def test_invalid_search_modes_preserve_400_response(
    search_client: TestClient, path: str, detail: str
) -> None:
    response = search_client.get(
        path, params={"query": "example", "search_type": "unsupported"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("value", [None, "newest", "legacy-value"])
def test_sort_preserves_existing_handler_behavior(
    search_client: TestClient, value: str | None
) -> None:
    params = {"query": "example", "search_type": "fulltext"}
    if value is not None:
        params["sort"] = value
    response = search_client.get("/search", params=params)
    assert response.status_code == 501
    assert "Full-text search is not implemented" in response.json()["detail"]


def test_similarity_without_query_preserves_comment_error(
    search_client: TestClient,
) -> None:
    response = search_client.get(
        "/comments/search", params={"search_type": "similarity"}
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Invalid search_type specified. Must be 'fulltext' or 'similarity'."
    }


@pytest.mark.parametrize("path", ["/comments/search", "/issues/search", "/search"])
def test_search_type_schema_lists_supported_values(
    search_client: TestClient, path: str
) -> None:
    parameters = search_client.get("/openapi.json").json()["paths"][path]["get"][
        "parameters"
    ]
    schema = next(p["schema"] for p in parameters if p["name"] == "search_type")
    assert schema["enum"] == ["fulltext", "similarity"]
    assert schema["default"] == "fulltext"


def test_search_sort_schema_preserves_choices(search_client: TestClient) -> None:
    parameters = search_client.get("/openapi.json").json()["paths"]["/search"]["get"][
        "parameters"
    ]
    schema = next(p["schema"] for p in parameters if p["name"] == "sort")
    assert schema["enum"] == ["newest"]
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_application_registers_search_routes() -> None:
    from preloop.api.app import create_app

    paths = create_app().openapi()["paths"]
    for path in ("/api/v1/comments/search", "/api/v1/issues/search", "/api/v1/search"):
        assert "get" in paths[path]
