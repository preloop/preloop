"""The API docs pages must not depend on a third-party host to render.

The Swagger UI and ReDoc bundles are vendored under
``backend/preloop/static/vendor`` and served from the API origin, so an
air-gapped install can render ``/docs/api`` and ``/docs/redoc``. These tests
fail if an external reference is reintroduced or the vendored bytes drift.
"""

import hashlib
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app

VENDOR_DIR = Path(__file__).resolve().parents[2] / "preloop" / "static" / "vendor"

# An absolute (``https://host``) or protocol-relative (``//host``) reference.
_EXTERNAL_REFERENCE = re.compile(r"(?:https?:)?//[A-Za-z0-9]")

# Page path -> local asset that the rendered HTML must reference.
_DOCS_ASSETS = {
    "/docs/api": (
        "/static/vendor/swagger-ui-bundle.js",
        "/static/vendor/swagger-ui.css",
    ),
    "/docs/redoc": ("/static/vendor/redoc.standalone.js",),
}


@pytest.fixture(scope="module")
def docs_app() -> FastAPI:
    """One application serves every assertion; none of them mutate it."""
    return create_app()


@pytest.mark.parametrize("path", sorted(_DOCS_ASSETS))
def test_docs_pages_reference_only_local_assets(docs_app: FastAPI, path: str) -> None:
    """The generated docs HTML must not point at any external host."""
    response = TestClient(docs_app).get(path)

    assert response.status_code == 200
    for asset in _DOCS_ASSETS[path]:
        assert asset in response.text
    externally_referenced = _EXTERNAL_REFERENCE.findall(response.text)
    assert not externally_referenced, (
        f"{path} references an external asset: {externally_referenced}"
    )


@pytest.mark.parametrize("path", sorted(_DOCS_ASSETS))
def test_docs_assets_are_served_from_the_api_origin(
    docs_app: FastAPI, path: str
) -> None:
    """Every asset the docs page references is reachable under /static."""
    client = TestClient(docs_app)

    for asset in _DOCS_ASSETS[path]:
        response = client.get(asset)
        assert response.status_code == 200
        assert response.content, f"{asset} served an empty body"


def test_vendored_assets_match_recorded_hashes() -> None:
    """The vendored bundles stay byte-identical to the pinned versions."""
    recorded = {}
    for line in (VENDOR_DIR / "SHA256SUMS").read_text().splitlines():
        digest, filename = line.split()
        recorded[filename] = digest

    assert recorded, "SHA256SUMS must list the vendored assets"

    for filename, digest in recorded.items():
        actual = hashlib.sha256((VENDOR_DIR / filename).read_bytes()).hexdigest()
        assert actual == digest, f"{filename} does not match SHA256SUMS"
