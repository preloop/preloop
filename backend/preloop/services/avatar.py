"""Avatar image processing for user profile images.

Handles validation, EXIF stripping, re-encoding, and resizing of uploaded
profile images. Produces a bounded square PNG stored as a base64 data URI.
"""

import base64
import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Constraints
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB raw upload limit
MAX_DIMENSION = 256  # Output image bound (px)
# Upper bound on the decoded pixel count. A 5 MB upload can hold a highly
# compressed image that expands to hundreds of MB of RGBA buffer, so the pixel
# count is bounded from the header before anything is decoded.
MAX_PIXELS = 4096 * 4096
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

_TOO_MANY_PIXELS = f"Image dimensions too large. Maximum: {MAX_PIXELS} total pixels"

# SSO provider claim keys
SSO_AVATAR_CLAIMS = {
    "google": "picture",
    "github": "avatar_url",
    "gitlab": "avatar_url",
}


class AvatarValidationError(Exception):
    """Raised when an uploaded avatar fails validation."""


def validate_content_type(content_type: str) -> None:
    """Raise if the content type is not an accepted image type."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise AvatarValidationError(
            f"Unsupported image type: {content_type}. "
            f"Accepted: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}"
        )


def validate_size(data: bytes) -> None:
    """Raise if the raw upload exceeds the size limit."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarValidationError(
            f"Image too large ({len(data)} bytes). Maximum: {MAX_UPLOAD_BYTES} bytes"
        )


def process_avatar(data: bytes, content_type: str) -> str:
    """Validate, strip EXIF, resize to a bounded square, and return a data URI.

    Args:
        data: Raw image bytes.
        content_type: MIME type of the upload.

    Returns:
        A ``data:image/png;base64,...`` URI string.

    Raises:
        AvatarValidationError: On invalid input.
    """
    validate_content_type(content_type)
    validate_size(data)

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        raise AvatarValidationError(
            "Image processing unavailable (Pillow not installed)"
        )

    # Pillow only *warns* at its default MAX_IMAGE_PIXELS and raises at twice
    # that, so lower the limit as defence in depth and reject explicitly below.
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS

    try:
        img = Image.open(io.BytesIO(data))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AvatarValidationError(_TOO_MANY_PIXELS)
    except Exception:
        raise AvatarValidationError("Unable to decode image")

    # Image.open only parses the header, so the declared size is known here
    # without having decoded any pixel data yet. Reject oversized images before
    # convert() allocates a full RGBA buffer.
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise AvatarValidationError(_TOO_MANY_PIXELS)

    # Strip EXIF and other metadata by re-drawing onto a fresh canvas.
    try:
        img = img.convert("RGBA")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AvatarValidationError(_TOO_MANY_PIXELS)
    except Exception:
        raise AvatarValidationError("Unable to decode image")

    # Crop to center square then resize to bounded dimensions.
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > MAX_DIMENSION:
        img = img.resize((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    # Re-encode as PNG (lossless, strips any original metadata).
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def extract_sso_avatar_url(provider: str, user_info: dict) -> Optional[str]:
    """Extract the avatar URL from an SSO provider's user-info payload.

    Args:
        provider: OAuth provider name (google, github, gitlab).
        user_info: The decoded user-info/ID-token claims dict.

    Returns:
        The avatar URL string, or None if not present. Only ``https://`` URLs
        are accepted: the console renders this URL directly, so a plaintext
        ``http://`` avatar would be MITM-able. All three supported providers
        serve avatars over https.
    """
    claim_key = SSO_AVATAR_CLAIMS.get(provider)
    if not claim_key:
        return None
    url = user_info.get(claim_key)
    if isinstance(url, str) and url.startswith("https://"):
        return url
    return None
