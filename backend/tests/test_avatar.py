"""Tests for user avatar functionality.

Covers upload validation, precedence rules, and per-provider SSO claim mapping.
"""

import io
import pytest
from unittest.mock import MagicMock

from preloop.services.avatar import (
    AvatarValidationError,
    extract_sso_avatar_url,
    process_avatar,
    validate_content_type,
    validate_size,
    MAX_PIXELS,
    MAX_UPLOAD_BYTES,
    SSO_AVATAR_CLAIMS,
)


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------


class TestValidateContentType:
    def test_accepts_png(self):
        validate_content_type("image/png")

    def test_accepts_jpeg(self):
        validate_content_type("image/jpeg")

    def test_accepts_webp(self):
        validate_content_type("image/webp")

    def test_accepts_gif(self):
        validate_content_type("image/gif")

    def test_rejects_svg(self):
        with pytest.raises(AvatarValidationError, match="Unsupported"):
            validate_content_type("image/svg+xml")

    def test_rejects_octet_stream(self):
        with pytest.raises(AvatarValidationError, match="Unsupported"):
            validate_content_type("application/octet-stream")


class TestValidateSize:
    def test_accepts_small_file(self):
        validate_size(b"x" * 1024)

    def test_rejects_oversized_file(self):
        with pytest.raises(AvatarValidationError, match="too large"):
            validate_size(b"x" * (MAX_UPLOAD_BYTES + 1))


class TestProcessAvatar:
    def _make_png(self, width: int = 100, height: int = 100) -> bytes:
        """Create a minimal test PNG image."""
        from PIL import Image

        img = Image.new("RGBA", (width, height), (255, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_returns_data_uri(self):
        data = self._make_png()
        result = process_avatar(data, "image/png")
        assert result.startswith("data:image/png;base64,")

    def test_square_crop(self):
        """A non-square image is cropped to center square."""
        data = self._make_png(200, 100)
        result = process_avatar(data, "image/png")
        assert result.startswith("data:image/png;base64,")
        # Decode and check dimensions
        import base64
        from PIL import Image

        b64 = result.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert img.width == img.height

    def test_rejects_invalid_content_type(self):
        data = self._make_png()
        with pytest.raises(AvatarValidationError, match="Unsupported"):
            process_avatar(data, "text/plain")

    def test_rejects_oversized(self):
        with pytest.raises(AvatarValidationError, match="too large"):
            process_avatar(b"x" * (MAX_UPLOAD_BYTES + 1), "image/png")

    def test_rejects_corrupt_data(self):
        with pytest.raises(AvatarValidationError, match="Unable to decode"):
            process_avatar(b"not-an-image", "image/png")

    def test_strips_exif(self):
        """Processed images have no metadata besides essential PNG chunks."""
        from PIL import Image

        img = Image.new("RGB", (64, 64), "blue")
        buf = io.BytesIO()
        from PIL.ExifTags import Base as ExifBase

        exif = img.getexif()
        exif[ExifBase.Software] = "TestSuite"
        img.save(buf, format="JPEG", exif=exif.tobytes())
        raw_jpeg = buf.getvalue()

        result = process_avatar(raw_jpeg, "image/jpeg")
        # Output is PNG, re-open and verify no EXIF
        import base64

        b64 = result.split(",", 1)[1]
        out = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert not out.getexif(), "EXIF data should be stripped"

    def test_large_image_resized(self):
        """Images larger than MAX_DIMENSION are resized down."""
        data = self._make_png(1024, 1024)
        result = process_avatar(data, "image/png")
        import base64
        from PIL import Image

        b64 = result.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert img.width <= 256
        assert img.height <= 256


class TestDecompressionBomb:
    """The decoded pixel count is bounded before any full decode happens."""

    def _png_header(self, width: int, height: int) -> bytes:
        """A tiny PNG whose header declares ``width`` x ``height``.

        ``Image.open`` only parses the header, so a payload of a few hundred
        bytes is enough to exercise the pixel-count guard. If the guard ever
        regresses to checking dimensions after ``convert()``, these tests would
        try to allocate the full buffer instead of raising.
        """
        import struct
        import zlib

        def chunk(kind: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", crc)
            )

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
            + chunk(b"IEND", b"")
        )

    def test_payload_stays_tiny(self):
        """Guard the premise: the bomb payload is orders of magnitude smaller."""
        bomb = self._png_header(5000, 5000)
        assert len(bomb) < 1024
        assert 5000 * 5000 > MAX_PIXELS

    def test_rejects_bomb_above_pixel_limit(self):
        """25M declared pixels is rejected by the explicit pre-decode check.

        This size sits above ``MAX_PIXELS`` but below the 2x threshold where
        Pillow raises on its own, so it covers our own guard specifically.
        """
        bomb = self._png_header(5000, 5000)
        with pytest.raises(AvatarValidationError, match="too large"):
            process_avatar(bomb, "image/png")

    def test_rejects_extreme_bomb(self):
        """Pillow's own DecompressionBombError surfaces as a validation error."""
        bomb = self._png_header(30000, 30000)
        with pytest.raises(AvatarValidationError, match="too large"):
            process_avatar(bomb, "image/png")

    def test_pillow_pixel_limit_is_lowered(self):
        """Pillow's global bomb threshold is tightened to our own bound."""
        from PIL import Image

        img = Image.new("RGBA", (32, 32), (0, 128, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        process_avatar(buf.getvalue(), "image/png")

        assert Image.MAX_IMAGE_PIXELS == MAX_PIXELS

    def test_image_under_limit_still_processes(self):
        """The guard does not reject ordinary avatars."""
        from PIL import Image

        img = Image.new("RGBA", (512, 512), (0, 0, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = process_avatar(buf.getvalue(), "image/png")
        assert result.startswith("data:image/png;base64,")


class TestAvatarUploadReadLimit:
    """The upload endpoint must not buffer an oversized body before rejecting."""

    def test_chunked_read_rejects_over_max(self):
        import asyncio

        from fastapi import HTTPException

        from preloop.api.endpoints.account import _read_upload_with_limit

        class FakeUpload:
            def __init__(self, total: int) -> None:
                self.sent = 0
                self.total = total

            async def read(self, n: int = -1) -> bytes:
                if self.sent >= self.total:
                    return b""
                take = min(n if n > 0 else self.total, self.total - self.sent)
                self.sent += take
                return b"x" * take

        fake = FakeUpload(MAX_UPLOAD_BYTES + 1)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_read_upload_with_limit(fake, MAX_UPLOAD_BYTES))
        assert exc_info.value.status_code == 413
        assert fake.sent <= MAX_UPLOAD_BYTES + 1024 * 1024


class TestPillowDependencyDeclared:
    """Pillow is a declared runtime dependency, not a best-effort import."""

    def test_pillow_importable(self):
        from PIL import Image  # noqa: F401

    def test_pillow_declared_in_pyproject(self):
        """A clean ``pip install -e ".[dev]"`` must provide PIL."""
        import tomllib
        from pathlib import Path

        # backend/tests/test_avatar.py -> repo root
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        dependencies = data["project"]["dependencies"]

        assert any(dep.lower().startswith("pillow") for dep in dependencies), (
            "Pillow must be declared in [project].dependencies so the avatar "
            "service is not silently unavailable on a clean install."
        )

    @pytest.mark.parametrize(
        "lock",
        [
            "requirements/runtime.txt",
            ".github/requirements/app-dev.txt",
            ".github/requirements/runtime-plugin-test.txt",
        ],
    )
    def test_pillow_pinned_in_locks_compiled_from_pyproject(self, lock: str):
        """Declaring the dependency is not enough; the locks are what get installed.

        CI installs ``--require-hashes -r .github/requirements/app-dev.txt``
        and the image installs ``requirements/runtime.txt``, so a lock that was
        not recompiled after a pyproject change fails at import time with no
        hint about the cause.
        """
        from pathlib import Path

        # backend/tests/test_avatar.py -> repo root
        text = (Path(__file__).resolve().parents[2] / lock).read_text(encoding="utf-8")
        assert "\npillow==" in text, (
            f"{lock} is compiled from pyproject.toml but has no pillow pin. "
            "Recompile it with the uv command in its header comment."
        )


# ---------------------------------------------------------------------------
# SSO claim mapping
# ---------------------------------------------------------------------------


class TestExtractSsoAvatarUrl:
    def test_google_picture(self):
        url = extract_sso_avatar_url(
            "google", {"picture": "https://lh3.googleusercontent.com/photo"}
        )
        assert url == "https://lh3.googleusercontent.com/photo"

    def test_github_avatar_url(self):
        url = extract_sso_avatar_url(
            "github", {"avatar_url": "https://avatars.githubusercontent.com/u/123"}
        )
        assert url == "https://avatars.githubusercontent.com/u/123"

    def test_gitlab_avatar_url(self):
        url = extract_sso_avatar_url(
            "gitlab", {"avatar_url": "https://gitlab.com/uploads/-/avatar.png"}
        )
        assert url == "https://gitlab.com/uploads/-/avatar.png"

    def test_missing_claim_returns_none(self):
        url = extract_sso_avatar_url("google", {"email": "user@example.com"})
        assert url is None

    def test_unknown_provider_returns_none(self):
        url = extract_sso_avatar_url(
            "unknown", {"picture": "https://example.com/pic.jpg"}
        )
        assert url is None

    def test_non_http_value_returns_none(self):
        url = extract_sso_avatar_url("google", {"picture": "not-a-url"})
        assert url is None

    def test_plaintext_http_returns_none(self):
        """A plaintext avatar URL is MITM-able, so it is not accepted."""
        url = extract_sso_avatar_url("google", {"picture": "http://example.com/p.jpg"})
        assert url is None

    def test_scheme_lookalike_returns_none(self):
        """`startswith("http")` used to accept this; an explicit scheme does not."""
        for value in ("httpfoo", "httpfoo://example.com/p.jpg", "https:/example.com"):
            assert extract_sso_avatar_url("google", {"picture": value}) is None

    def test_non_string_claim_returns_none(self):
        url = extract_sso_avatar_url("google", {"picture": {"url": "https://x/y"}})
        assert url is None

    def test_claim_keys_documented(self):
        """All three providers have documented claim keys."""
        assert "google" in SSO_AVATAR_CLAIMS
        assert "github" in SSO_AVATAR_CLAIMS
        assert "gitlab" in SSO_AVATAR_CLAIMS


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


class TestAvatarPrecedence:
    """Test that manual upload > SSO > default via the CRUD helper."""

    def test_sso_sets_avatar(self):
        """set_avatar_from_sso sets url and source='sso' on a fresh user."""
        from preloop.models.crud.user import CRUDUser

        crud = CRUDUser.__new__(CRUDUser)
        user = MagicMock()
        user.avatar_source = None
        user.avatar_url = None
        db = MagicMock()

        crud.set_avatar_from_sso(
            db, user=user, avatar_url="https://example.com/pic.jpg", commit=False
        )
        assert user.avatar_url == "https://example.com/pic.jpg"
        assert user.avatar_source == "sso"

    def test_manual_upload_blocks_sso(self):
        """set_avatar_from_sso is a no-op when avatar_source is 'manual'."""
        from preloop.models.crud.user import CRUDUser

        crud = CRUDUser.__new__(CRUDUser)
        user = MagicMock()
        user.avatar_source = "manual"
        user.avatar_url = "data:image/png;base64,AAAA"
        db = MagicMock()

        crud.set_avatar_from_sso(
            db, user=user, avatar_url="https://example.com/new.jpg", commit=False
        )
        # Manual avatar is preserved
        assert user.avatar_url == "data:image/png;base64,AAAA"
        assert user.avatar_source == "manual"

    def test_sso_replaces_sso(self):
        """A new SSO URL overwrites a previous SSO URL."""
        from preloop.models.crud.user import CRUDUser

        crud = CRUDUser.__new__(CRUDUser)
        user = MagicMock()
        user.avatar_source = "sso"
        user.avatar_url = "https://old.example.com/pic.jpg"
        db = MagicMock()

        crud.set_avatar_from_sso(
            db, user=user, avatar_url="https://new.example.com/pic.jpg", commit=False
        )
        assert user.avatar_url == "https://new.example.com/pic.jpg"
        assert user.avatar_source == "sso"
