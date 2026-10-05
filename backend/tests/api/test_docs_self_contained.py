"""The API docs pages must not depend on a third-party host to render.

The Swagger UI and ReDoc bundles are vendored under
``backend/preloop/static/vendor`` and served from the API origin, so an
air-gapped install can render ``/docs/api`` and ``/docs/redoc``. These tests
fail if an external reference is reintroduced or the vendored bytes drift.
"""

import hashlib
import re
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "preloop"
VENDOR_DIR = PACKAGE_DIR / "static" / "vendor"

# An absolute (``https://host``) or protocol-relative (``//host``) reference.
_EXTERNAL_REFERENCE = re.compile(r"(?:https?:)?//[A-Za-z0-9]")

# ReDoc 2.0.0 fetches this logo from inside the bundle and hides the image
# on error. It is not part of the generated HTML, and get_redoc_html cannot
# override it.
_REDOC_LOGO_URL = "https://cdn.redoc.ly/redoc/logo-mini.svg"

# Page path -> local asset that the rendered HTML must reference.
_DOCS_ASSETS = {
    "/docs/api": (
        "/static/vendor/swagger-ui-bundle.js",
        "/static/vendor/swagger-ui.css",
        "/static/vendor/favicon.png",
    ),
    "/docs/redoc": (
        "/static/vendor/redoc.standalone.js",
        "/static/vendor/favicon.png",
    ),
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


def test_redoc_bundle_has_one_known_off_origin_logo() -> None:
    """The pinned ReDoc bundle keeps its single hidden default logo URL."""
    bundle = (VENDOR_DIR / "redoc.standalone.js").read_text()

    assert bundle.count(_REDOC_LOGO_URL) == 1
    assert 'alt:"redocly logo",onError:' in bundle


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


def _packaged_static_paths() -> set[str]:
    """Resolve the declared package-data globs to ``preloop``-relative paths.

    Returns:
        The package-relative POSIX paths that
        ``[tool.setuptools.package-data]`` would include.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools_config = config.get("tool", {}).get("setuptools", {})
    assert "package-data" in setuptools_config, (
        "pyproject.toml must declare [tool.setuptools.package-data] so a "
        "non-editable wheel ships the vendored docs assets"
    )

    packaged: set[str] = set()
    for package, patterns in setuptools_config["package-data"].items():
        if package != "preloop" and not package.startswith("preloop."):
            continue
        package_dir = PACKAGE_DIR.parent / package.replace(".", "/")
        for pattern in patterns:
            for candidate in package_dir.glob(pattern):
                if candidate.is_file():
                    packaged.add(candidate.relative_to(PACKAGE_DIR).as_posix())
    return packaged


def test_vendored_assets_are_declared_in_package_data() -> None:
    """A non-editable wheel ships every vendored docs asset.

    The setuptools build backend only exists inside pip's isolated build
    environment, so this resolves the declared ``package-data`` globs against
    the source tree instead of building a wheel. It fails if the rule stops
    covering a vendored file (``static/*`` would miss ``static/vendor/``);
    ``test_vendored_assets_match_recorded_hashes`` guards the bytes packaged.
    """
    vendored = {
        path.relative_to(PACKAGE_DIR).as_posix()
        for path in VENDOR_DIR.rglob("*")
        if path.is_file()
    }
    assert vendored, "no vendored docs assets found to package"

    missing = sorted(vendored - _packaged_static_paths())
    assert not missing, (
        "[tool.setuptools.package-data] would drop vendored docs assets "
        f"from a non-editable wheel: {missing}"
    )
