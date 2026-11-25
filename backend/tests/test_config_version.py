"""Tests for release version loading."""

from importlib.metadata import PackageNotFoundError
from pathlib import Path
import pytest
from unittest.mock import patch

from preloop.config import _load_release_version


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ("1.1.0rc1", "1.1.0-rc.1"),
        ("1.1.0a2", "1.1.0-alpha.2"),
        ("1.1.0b3", "1.1.0-beta.3"),
        ("1.1.0-rc.1", "1.1.0-rc.1"),
        ("1.0.2", "1.0.2"),
    ],
)
def test_installed_version_uses_semver_for_clients(
    installed: str, expected: str
) -> None:
    """Wheel normalization must not leak an unparseable CLI version contract."""
    import preloop

    with (
        patch("preloop.config.version", return_value=installed),
        patch("preloop._package_version", return_value=installed),
    ):
        assert _load_release_version() == expected
        assert preloop._resolve_version() == expected


def test_load_release_version_uses_package_metadata_when_installed() -> None:
    """When installed via pip, version comes from package metadata."""
    with patch("preloop.config.version", return_value="0.8.0-beta.4"):
        assert _load_release_version() == "0.8.0-beta.4"


def test_load_release_version_reads_version_file_when_package_not_installed(
    tmp_path: Path,
) -> None:
    """When package metadata is unavailable, read the VERSION file (Docker/local dev)."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.8.0-beta.1\n", encoding="utf-8")

    with patch("preloop.config.version", side_effect=PackageNotFoundError("preloop")):
        assert _load_release_version(version_file=version_file) == "0.8.0-beta.1"


def test_load_release_version_uses_fallback_when_both_unavailable(
    tmp_path: Path,
) -> None:
    """When metadata and file are both unavailable, use default."""
    missing_file = tmp_path / "VERSION"

    with patch("preloop.config.version", side_effect=PackageNotFoundError("preloop")):
        assert (
            _load_release_version(default="9.9.9", version_file=missing_file) == "9.9.9"
        )


def test_product_version_prefers_the_edition_release_manifest(tmp_path: Path) -> None:
    """protects=footer shows the installed release, not the bundled OSS package;
    fails_when=an edition image reports the open-source version."""
    from preloop.config import _load_product_version

    manifest = tmp_path / "EE-RELEASE.json"
    manifest.write_text('{"version": "2.0.0-rc.1", "oss_version": "1.0.2"}')
    assert _load_product_version("1.0.2", manifest=manifest) == "2.0.0-rc.1"


def test_product_version_falls_back_to_server_version(tmp_path: Path) -> None:
    """protects=open-source images and unreadable manifests keep the package version."""
    from preloop.config import _load_product_version

    assert _load_product_version("1.0.2", manifest=tmp_path / "missing.json") == (
        "1.0.2"
    )
    broken = tmp_path / "EE-RELEASE.json"
    broken.write_text('{"version": 3}')
    assert _load_product_version("1.0.2", manifest=broken) == "1.0.2"
    broken.write_text("not json")
    assert _load_product_version("1.0.2", manifest=broken) == "1.0.2"


def test_features_report_product_version(db_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """protects=/features carries the release version for the console footer."""
    from preloop.api.endpoints import features

    monkeypatch.setattr(features, "PRODUCT_VERSION", "2.0.0-rc.1")
    data = features.get_features(db=db_session)
    assert data["product_version"] == "2.0.0-rc.1"
    assert data["server_version"]
