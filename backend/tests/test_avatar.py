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
