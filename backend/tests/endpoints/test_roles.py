"""The role catalog is owned by the commercial RBAC plugin."""

from fastapi.testclient import TestClient


def test_core_has_no_roles_endpoint(client: TestClient) -> None:
    """A core account cannot fetch a commercial role catalog."""
    assert client.get("/api/v1/roles").status_code == 404
