"""Per-kind media allowlist, content checks and standards shape mapping."""

from __future__ import annotations

import pytest

from preloop.services import artifact_media as media
from preloop.services import artifact_shapes as shapes

# One valid sample per (kind, media type). Bytes are synthetic.
VALID: list[tuple[str, str, bytes]] = [
    ("screenshot", "image/png", b"\x89PNG\r\n\x1a\n...."),
    ("recording", "video/webm", b"\x1a\x45\xdf\xa3...."),
    ("screencast", "video/webm", b"\x1a\x45\xdf\xa3...."),
    ("screencast", "video/mp4", b"\x00\x00\x00\x18ftypmp42"),
    ("audio", "audio/mpeg", b"ID3\x04\x00...."),
    ("audio", "audio/mpeg", b"\xff\xfb\x90\x00"),
    ("audio", "audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
    ("audio", "audio/ogg", b"OggS\x00\x02"),
    ("audio", "audio/webm", b"\x1a\x45\xdf\xa3...."),
    ("audio", "audio/mp4", b"\x00\x00\x00\x18ftypM4A "),
    ("audio", "audio/flac", b"fLaC\x00\x00"),
    ("transcript", "text/plain", "Guten Tag, Herr Müller".encode()),
    ("transcript", "text/vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello"),
    ("transcript", "application/x-subrip", b"1\n00:00:00,000 --> 00:00:01,000\nhi"),
    ("transcript", "application/json", b'{"segments": [{"text": "hi"}]}'),
    ("document", "text/plain", b"notes"),
    ("document", "text/markdown", b"# Title"),
    ("document", "application/json", b"[1, 2]"),
    ("document", "application/pdf", b"%PDF-1.7\n..."),
    ("generated_file", "text/csv", b"a,b\n1,2"),
    ("generated_file", "application/octet-stream", b"\x00\x01\x02"),
    ("trace", "application/zip", b"PK\x03\x04...."),
]

# Declared type with bytes that do not match it.
MISMATCH: list[tuple[str, str, bytes]] = [
    ("screenshot", "image/png", b"\xff\xd8\xff-jpeg"),
    ("screenshot", "image/jpeg", b"\x89PNG\r\n\x1a\n"),
    ("screenshot", "image/webp", b"RIFF\x00\x00\x00\x00WAVE"),
    ("recording", "video/webm", b"not-webm"),
    ("recording", "video/mp4", b"\x1a\x45\xdf\xa3"),
    ("screencast", "video/webm", b"not-a-webm"),
    ("screencast", "video/mp4", b"\x00\x00\x00\x18nope"),
    ("audio", "audio/mpeg", b"RIFF....WAVE"),
    ("audio", "audio/wav", b"RIFF\x00\x00\x00\x00AVI "),
    ("audio", "audio/ogg", b"fLaC"),
    ("audio", "audio/webm", b"OggS"),
    ("audio", "audio/mp4", b"\x1a\x45\xdf\xa3"),
    ("audio", "audio/flac", b"ID3"),
    ("transcript", "text/plain", b"\xff\xfe\x00bad utf8 \xc3"),
    ("transcript", "application/json", b"{not json"),
    ("document", "application/pdf", b"<html>"),
    ("document", "application/json", b"{"),
    ("trace", "application/zip", b"%PDF-1.7"),
]


@pytest.mark.parametrize(("kind", "content_type", "data"), VALID)
def test_valid_payload_is_accepted(kind: str, content_type: str, data: bytes) -> None:
    assert media.check_content(kind, content_type, data) == content_type


@pytest.mark.parametrize(("kind", "content_type", "data"), MISMATCH)
def test_wrong_magic_is_a_content_mismatch(
    kind: str, content_type: str, data: bytes
) -> None:
    with pytest.raises(ValueError, match="artifact_content_mismatch"):
        media.check_content(kind, content_type, data)


@pytest.mark.parametrize(
    ("kind", "content_type"),
    [
        ("screenshot", "image/gif"),
        ("recording", "video/quicktime"),
        ("audio", "video/webm"),
        ("transcript", "application/pdf"),
        ("document", "text/html"),
        ("trace", "application/json"),
        ("generated_file", "not-a-media-type"),
    ],
)
def test_type_outside_the_kind_allowlist_is_refused(
    kind: str, content_type: str
) -> None:
    with pytest.raises(ValueError, match="artifact_media_type_invalid"):
        media.check_content(kind, content_type, b"PK\x03\x04")


@pytest.mark.parametrize(
    "magic",
    [
        b"\x7fELF\x02\x01",
        b"MZ\x90\x00",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
    ],
)
def test_executable_generated_file_is_refused(magic: bytes) -> None:
    with pytest.raises(ValueError, match="artifact_content_mismatch"):
        media.check_content("generated_file", "application/octet-stream", magic)


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="artifact_kind_invalid"):
        media.check_content("hologram", "text/plain", b"x")


def test_parameters_are_dropped_and_type_lowercased() -> None:
    assert (
        media.check_content("transcript", "Text/Plain; charset=utf-8", b"hi")
        == "text/plain"
    )


def test_every_kind_has_a_modality() -> None:
    assert set(media.KIND_MODALITY) == set(media.ARTIFACT_KINDS)
    assert set(media.KIND_MODALITY.values()) <= {"image", "video", "audio", "document"}


def test_shapes_mapping_agrees_with_the_kind_table() -> None:
    """``artifact_shapes`` (from #1109) owns the wire mapping and has its own
    round-trip tests. Pin that its kinds and modalities match this table, so a
    kind added here cannot silently map to the wrong OTel modality."""
    for kind in media.ARTIFACT_KINDS:
        assert shapes.modality_for(kind) == media.KIND_MODALITY[kind]
    for content_type in ("image/png", "audio/wav", "video/mp4", "text/vtt",
                         "text/markdown", "application/zip"):
        assert shapes.infer_kind(content_type) in media.ARTIFACT_KINDS
