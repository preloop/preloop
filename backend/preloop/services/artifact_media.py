"""Per-kind media type allowlist and content checks for session artifacts.

Every artifact kind has a list of media types it accepts. For most types the
first bytes are compared with the format signature, so a payload that is not
the declared type is refused at store time and the byte route never serves
it under a misleading media type. ``generated_file`` accepts any media type
but refuses native executables (ELF, PE, Mach-O).

Error codes raised as ``ValueError``:

- ``artifact_kind_invalid``: the kind is unknown.
- ``artifact_media_type_invalid``: the media type is not allowed for the kind.
- ``artifact_content_mismatch``: the bytes do not match the media type, or a
  ``generated_file`` is an executable.
"""

from __future__ import annotations

import json
from collections.abc import Callable

ARTIFACT_KINDS: tuple[str, ...] = (
    "screenshot",
    "recording",
    "screencast",
    "audio",
    "transcript",
    "document",
    "generated_file",
    "trace",
)

# Kind to OTel GenAI ``Modality`` (image, video, audio, document).
KIND_MODALITY: dict[str, str] = {
    "screenshot": "image",
    "recording": "video",
    "screencast": "video",
    "audio": "audio",
    "transcript": "document",
    "document": "document",
    "generated_file": "document",
    "trace": "document",
}


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _is_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff")


def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _is_mpeg_audio(data: bytes) -> bool:
    if data.startswith(b"ID3"):
        return True
    # MPEG audio frame sync: eleven set bits.
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _is_ogg(data: bytes) -> bool:
    return data.startswith(b"OggS")


def _is_ebml(data: bytes) -> bool:
    return data.startswith(b"\x1a\x45\xdf\xa3")


def _is_iso_bmff(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] == b"ftyp"


def _is_flac(data: bytes) -> bool:
    return data.startswith(b"fLaC")


def _is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _is_zip(data: bytes) -> bool:
    return data.startswith(b"PK\x03\x04")


_Check = Callable[[bytes], bool]

# Screenshots keep the signature check that browser steps already apply.
# Recordings are checked for media type only here: their upload route (#887)
# owns the container check, and rows written before this change keep loading.
_ALLOWED: dict[str, dict[str, _Check | None]] = {
    "screenshot": {"image/png": None, "image/jpeg": None, "image/webp": None},
    "recording": {"video/webm": None, "video/mp4": None},
    "screencast": {"video/webm": _is_ebml, "video/mp4": _is_iso_bmff},
    "audio": {
        "audio/mpeg": _is_mpeg_audio,
        "audio/wav": _is_wav,
        "audio/ogg": _is_ogg,
        "audio/webm": _is_ebml,
        "audio/mp4": _is_iso_bmff,
        "audio/flac": _is_flac,
    },
    "transcript": {
        "text/plain": _is_utf8,
        "text/vtt": _is_utf8,
        "application/x-subrip": _is_utf8,
        "application/json": _is_json,
    },
    "document": {
        "text/plain": _is_utf8,
        "text/markdown": _is_utf8,
        "application/json": _is_json,
        "application/pdf": _is_pdf,
    },
    "trace": {"application/zip": _is_zip},
}

IMAGE_SIGNATURES: dict[str, _Check] = {
    "image/png": _is_png,
    "image/jpeg": _is_jpeg,
    "image/webp": _is_webp,
}

_EXECUTABLE_MAGIC: tuple[bytes, ...] = (
    b"\x7fELF",
    b"MZ",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # Mach-O universal (also Java class files)
)


def normalize_media_type(content_type: str) -> str:
    """Lower-case a media type and drop parameters such as ``charset``."""
    return content_type.split(";", 1)[0].strip().lower()


def is_declared_image(content_type: str, data: bytes) -> bool:
    """Return True when ``data`` starts with the signature of ``content_type``."""
    check = IMAGE_SIGNATURES.get(normalize_media_type(content_type))
    return bool(check and check(data))


def allowed_media_types(kind: str) -> tuple[str, ...] | None:
    """Return the media types a kind accepts, or None when any type is accepted.

    Raises:
        ValueError: ``artifact_kind_invalid`` for an unknown kind.
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError("artifact_kind_invalid")
    if kind == "generated_file":
        return None
    return tuple(_ALLOWED[kind])


def check_content(kind: str, content_type: str, data: bytes) -> str:
    """Validate a payload against its kind and declared media type.

    Args:
        kind: Artifact kind.
        content_type: Declared media type. Parameters are ignored.
        data: Plaintext bytes.

    Returns:
        The normalized media type to store.

    Raises:
        ValueError: ``artifact_kind_invalid``, ``artifact_media_type_invalid``
            or ``artifact_content_mismatch``.
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError("artifact_kind_invalid")
    media_type = normalize_media_type(content_type)
    if not media_type or "/" not in media_type:
        raise ValueError("artifact_media_type_invalid")
    if kind == "generated_file":
        if data.startswith(_EXECUTABLE_MAGIC):
            raise ValueError("artifact_content_mismatch")
        return media_type
    allowed = _ALLOWED[kind]
    if media_type not in allowed:
        raise ValueError("artifact_media_type_invalid")
    check = allowed[media_type]
    if check is not None and not check(data):
        raise ValueError("artifact_content_mismatch")
    return media_type
