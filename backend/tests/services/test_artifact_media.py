"""Per-kind media allowlist, content checks and standards shape mapping."""

from __future__ import annotations

import hashlib

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


SHAPE_SAMPLES = [
    ("screenshot", "image/png", b"\x89PNG\r\n\x1a\nxx"),
    ("audio", "audio/ogg", b"OggS\x00\x02"),
    ("transcript", "text/vtt", b"WEBVTT\n\nhallo"),
    ("trace", "application/zip", b"PK\x03\x04zz"),
]


def _payload(kind: str, content_type: str, data: bytes) -> shapes.ArtifactPayload:
    return shapes.ArtifactPayload(
        artifact_id="2b1f0e4a-0000-4000-8000-000000000001",
        kind=kind,
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        name=f"{kind}.bin",
        labels={"site": "heilbronn", "tags": ["call", "demo"]},
        producer="deposit_api",
        data=data,
        size_bytes=len(data),
    )


def _same(a: shapes.ArtifactPayload, b: shapes.ArtifactPayload) -> None:
    assert (b.content_type, b.sha256, b.name, b.kind, b.labels) == (
        a.content_type,
        a.sha256,
        a.name,
        a.kind,
        a.labels,
    )


@pytest.mark.parametrize(("kind", "content_type", "data"), SHAPE_SAMPLES)
def test_mcp_round_trip(kind: str, content_type: str, data: bytes) -> None:
    a = _payload(kind, content_type, data)
    block = shapes.to_mcp(a, uri="https://preloop.example/a/1")
    expected_type = {"screenshot": "image", "audio": "audio"}.get(kind, "resource")
    assert block["type"] == expected_type
    _same(a, shapes.from_mcp(block))
    link = shapes.to_mcp_link(a, uri="https://preloop.example/a/1")
    assert link["type"] == "resource_link" and link["size"] == len(data)
    _same(a, shapes.from_mcp(link))


@pytest.mark.parametrize(("kind", "content_type", "data"), SHAPE_SAMPLES)
def test_a2a_round_trip(kind: str, content_type: str, data: bytes) -> None:
    a = _payload(kind, content_type, data)
    artifact = shapes.to_a2a(a)
    assert artifact["parts"][0]["mediaType"] == content_type
    _same(a, shapes.from_a2a(artifact))
    _same(a, shapes.from_a2a(shapes.to_a2a(a, url="https://preloop.example/a/1")))


@pytest.mark.parametrize(("kind", "content_type", "data"), SHAPE_SAMPLES)
def test_otel_round_trip(kind: str, content_type: str, data: bytes) -> None:
    a = _payload(kind, content_type, data)
    part, attrs = shapes.to_otel(a, uri="https://preloop.example/a/1")
    assert part["type"] == "uri"
    assert part["modality"] == media.KIND_MODALITY[kind]
    assert attrs["preloop.artifact.kind"] == kind
    _same(a, shapes.from_otel(part, attrs))
    blob, blob_attrs = shapes.to_otel(a)
    assert blob["type"] == "blob"
    _same(a, shapes.from_otel(blob, blob_attrs))
