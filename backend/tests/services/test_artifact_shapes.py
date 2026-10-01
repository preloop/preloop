"""Tests for the MCP / A2A / OTel GenAI artifact shape mapping (#1078)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from preloop.services import artifact_shapes as shapes
from preloop.services.artifact_shapes import (
    ERROR_BLOCK_INVALID_BASE64,
    ERROR_BLOCK_TOO_LARGE,
    ERROR_BLOCK_TYPE_UNSUPPORTED,
    META_KEY,
    ArtifactPayload,
    from_a2a_part,
    from_mcp_content_block,
    from_otel_part,
    make_payload,
    modality_for,
    otel_span_attributes,
    to_a2a_artifact,
    to_a2a_part,
    to_mcp_content_block,
    to_otel_part,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x01" * 16
ZIP = b"PK\x03\x04" + bytes(range(64))
VTT = "WEBVTT\n\n00:00.000 --> 00:01.000\nhallo\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# MCP in
# --------------------------------------------------------------------------


def test_mcp_text_block_in():
    p = from_mcp_content_block({"type": "text", "text": "hello"}, kind=None)
    assert (p.kind, p.content_type, p.text, p.data) == (
        "document",
        "text/plain",
        "hello",
        None,
    )
    assert p.sha256 == sha(b"hello")


def test_mcp_image_block_in_infers_screenshot():
    p = from_mcp_content_block(
        {"type": "image", "data": b64(PNG), "mimeType": "image/png"}, kind=None
    )
    assert p.kind == "screenshot"
    assert p.content_type == "image/png"
    assert p.data == PNG
    assert p.sha256 == sha(PNG)


def test_mcp_audio_block_in_infers_audio():
    p = from_mcp_content_block(
        {"type": "audio", "data": b64(WAV), "mimeType": "audio/wav"}, kind=None
    )
    assert (p.kind, p.content_type, p.data) == ("audio", "audio/wav", WAV)


def test_mcp_resource_link_in():
    p = from_mcp_content_block(
        {
            "type": "resource_link",
            "uri": "https://x.test/trace.zip",
            "name": "trace.zip",
            "mimeType": "application/zip",
            "size": 68,
        },
        kind="trace",
    )
    assert (p.kind, p.name, p.uri, p.content_type) == (
        "trace",
        "trace.zip",
        "https://x.test/trace.zip",
        "application/zip",
    )
    assert p.data is None and p.text is None and p.sha256 is None


def test_mcp_embedded_text_resource_vtt_is_transcript():
    p = from_mcp_content_block(
        {
            "type": "resource",
            "resource": {
                "uri": "file:///call.vtt",
                "mimeType": "text/vtt",
                "text": VTT,
            },
        },
        kind=None,
    )
    assert (p.kind, p.text, p.uri) == ("transcript", VTT, "file:///call.vtt")


def test_mcp_embedded_srt_is_transcript():
    p = from_mcp_content_block(
        {
            "type": "resource",
            "resource": {
                "uri": "file:///c.srt",
                "mimeType": "application/x-subrip",
                "text": "1",
            },
        },
        kind=None,
    )
    assert p.kind == "transcript"


def test_mcp_embedded_blob_resource_is_generated_file():
    p = from_mcp_content_block(
        {
            "type": "resource",
            "resource": {
                "uri": "file:///o.zip",
                "mimeType": "application/zip",
                "blob": b64(ZIP),
            },
        },
        kind=None,
    )
    assert (p.kind, p.data, p.sha256) == ("generated_file", ZIP, sha(ZIP))


def test_mcp_meta_supplies_kind_labels_and_explicit_kind_wins():
    block = {
        "type": "image",
        "data": b64(PNG),
        "mimeType": "image/png",
        "_meta": {META_KEY: {"kind": "screencast", "labels": {"site": "HN"}}},
    }
    assert from_mcp_content_block(block, kind=None).kind == "screencast"
    assert from_mcp_content_block(block, kind=None).labels == {"site": "HN"}
    assert from_mcp_content_block(block, kind="document").kind == "document"


@pytest.mark.parametrize(
    "block",
    [
        {"type": "tool_use", "name": "x"},
        {"type": "text"},
        {"type": "resource", "resource": {"uri": "x"}},
        {"type": "resource_link"},
        "not a dict",
    ],
)
def test_mcp_unsupported_block(block):
    with pytest.raises(ValueError, match=ERROR_BLOCK_TYPE_UNSUPPORTED):
        from_mcp_content_block(block, kind=None)


@pytest.mark.parametrize("data", ["not base64!!", 42, None])
def test_mcp_invalid_base64(data):
    with pytest.raises(ValueError, match=ERROR_BLOCK_INVALID_BASE64):
        from_mcp_content_block(
            {"type": "image", "data": data, "mimeType": "image/png"}, kind=None
        )


def test_oversized_base64_is_refused_before_decoding(monkeypatch):
    calls = []
    monkeypatch.setattr(
        shapes.base64, "b64decode", lambda *a, **k: calls.append(a) or b""
    )
    encoded = "A" * (4 * ((10 + 2) // 3) + 4)
    for block in (
        {"type": "image", "data": encoded, "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "u", "blob": encoded}},
    ):
        with pytest.raises(ValueError, match=ERROR_BLOCK_TOO_LARGE):
            from_mcp_content_block(block, kind=None, max_bytes=10)
    with pytest.raises(ValueError, match=ERROR_BLOCK_TOO_LARGE):
        from_a2a_part({"raw": encoded}, max_bytes=10)
    with pytest.raises(ValueError, match=ERROR_BLOCK_TOO_LARGE):
        from_otel_part(
            {"type": "blob", "modality": "image", "content": encoded}, max_bytes=10
        )
    assert calls == []


def test_base64_at_limit_is_accepted():
    data = b"\x01" * 10
    p = from_mcp_content_block(
        {"type": "resource", "resource": {"uri": "u", "blob": b64(data)}},
        kind=None,
        max_bytes=10,
    )
    assert p.data == data


# --------------------------------------------------------------------------
# MCP out
# --------------------------------------------------------------------------


def _payload(**kw) -> ArtifactPayload:
    base = {"kind": None, "name": "n", "content_type": None, "labels": {"site": "HN"}}
    base.update(kw)
    return make_payload(**base)


def test_mcp_out_resource_link_when_uri_and_not_inline():
    p = _payload(data=PNG, content_type="image/png", name="shot.png")
    block = to_mcp_content_block(
        p,
        uri="https://p.test/a/1",
        inline=False,
        artifact_id="a1",
        producer="deposit_api",
    )
    assert block["type"] == "resource_link"
    assert block["uri"] == "https://p.test/a/1"
    assert block["name"] == "shot.png"
    assert block["mimeType"] == "image/png"
    assert block["size"] == len(PNG)
    assert block["_meta"][META_KEY] == {
        "artifact_id": "a1",
        "kind": "screenshot",
        "labels": {"site": "HN"},
        "sha256": sha(PNG),
        "producer": "deposit_api",
    }


def test_mcp_out_inline_image_and_audio():
    img = to_mcp_content_block(
        _payload(data=PNG, content_type="image/png"), uri=None, inline=True
    )
    assert (img["type"], img["data"], img["mimeType"]) == (
        "image",
        b64(PNG),
        "image/png",
    )
    assert img["_meta"][META_KEY]["kind"] == "screenshot"
    aud = to_mcp_content_block(
        _payload(data=WAV, content_type="audio/wav"), uri="u", inline=True
    )
    assert (aud["type"], aud["data"], aud["mimeType"]) == (
        "audio",
        b64(WAV),
        "audio/wav",
    )


def test_mcp_out_inline_plain_text():
    block = to_mcp_content_block(_payload(text="hi"), uri=None, inline=True)
    assert block["type"] == "text" and block["text"] == "hi"
    assert block["_meta"][META_KEY]["name"] == "n"


def test_mcp_out_inline_embedded_text_and_blob():
    t = to_mcp_content_block(
        _payload(text=VTT, content_type="text/vtt"), uri="u://v", inline=True
    )
    assert t == {
        "type": "resource",
        "resource": {"uri": "u://v", "mimeType": "text/vtt", "text": VTT},
        "_meta": t["_meta"],
    }
    b = to_mcp_content_block(
        _payload(data=ZIP, content_type="application/zip"), uri="u://z", inline=True
    )
    assert b["resource"]["blob"] == b64(ZIP)
    assert "_meta" in b and b["_meta"][META_KEY]["sha256"] == sha(ZIP)


def test_mcp_out_binary_without_uri_cannot_inline():
    with pytest.raises(ValueError, match=ERROR_BLOCK_TYPE_UNSUPPORTED):
        to_mcp_content_block(
            _payload(data=ZIP, content_type="application/zip"), uri=None, inline=True
        )


# --------------------------------------------------------------------------
# A2A
# --------------------------------------------------------------------------


def test_a2a_out_fields():
    raw = to_a2a_part(_payload(data=PNG, content_type="image/png", name="s.png"))
    assert raw["raw"] == b64(PNG)
    assert raw["filename"] == "s.png"
    assert raw["media_type"] == "image/png"
    assert raw["metadata"][META_KEY]["labels"] == {"site": "HN"}
    txt = to_a2a_part(_payload(text="hi"))
    assert txt["text"] == "hi" and "raw" not in txt
    url = to_a2a_part(
        _payload(uri="https://x.test/t.zip", content_type="application/zip")
    )
    assert url["url"] == "https://x.test/t.zip"


def test_a2a_in_fields():
    assert from_a2a_part({"raw": PNG, "media_type": "image/png"}).data == PNG
    assert (
        from_a2a_part({"raw": b64(PNG), "mediaType": "image/png"}).kind == "screenshot"
    )
    assert from_a2a_part({"text": "x", "filename": "a.txt"}).name == "a.txt"
    assert from_a2a_part({"url": "https://x.test/y"}).uri == "https://x.test/y"
    d = from_a2a_part({"data": {"b": 1, "a": [1]}})
    assert d.content_type == "application/json" and json.loads(d.text) == {
        "a": [1],
        "b": 1,
    }
    assert d.kind == "document"
    with pytest.raises(ValueError, match=ERROR_BLOCK_TYPE_UNSUPPORTED):
        from_a2a_part({"metadata": {}})


def test_a2a_artifact():
    a = to_a2a_artifact(
        "art-1",
        "run",
        [_payload(text="a"), _payload(data=PNG, content_type="image/png")],
    )
    assert a["artifact_id"] == "art-1" and a["name"] == "run"
    assert [("text" in p, "raw" in p) for p in a["parts"]] == [
        (True, False),
        (False, True),
    ]


# --------------------------------------------------------------------------
# OTel
# --------------------------------------------------------------------------


def test_otel_out_fields():
    p = _payload(data=PNG, content_type="image/png")
    assert to_otel_part(p) == {
        "type": "blob",
        "mime_type": "image/png",
        "modality": "image",
        "content": b64(PNG),
    }
    assert to_otel_part(p, uri="https://p.test/a/1") == {
        "type": "uri",
        "mime_type": "image/png",
        "modality": "image",
        "uri": "https://p.test/a/1",
    }
    attrs = otel_span_attributes(p)
    assert attrs["preloop.artifact.kind"] == "screenshot"
    assert json.loads(attrs["preloop.artifact.labels"]) == {"site": "HN"}


def test_otel_file_part_in():
    p = from_otel_part(
        {
            "type": "file",
            "mime_type": "application/pdf",
            "modality": "document",
            "file_id": "file-abc",
        },
        attributes={"preloop.artifact.labels": '{"site":"HN"}'},
    )
    assert p.uri is None and p.data is None and p.text is None
    assert p.labels == {"site": "HN", "provider_file_id": "file-abc"}
    assert p.kind == "generated_file"


def test_otel_modality_used_when_no_mime_type():
    p = from_otel_part({"type": "uri", "modality": "video", "uri": "u"})
    assert p.kind == "recording"


@pytest.mark.parametrize(
    "part", [{"type": "text", "content": "x"}, {"type": "file"}, {"type": "uri"}, []]
)
def test_otel_unsupported(part):
    with pytest.raises(ValueError, match=ERROR_BLOCK_TYPE_UNSUPPORTED):
        from_otel_part(part)


@pytest.mark.parametrize(
    "kind,ct,expected",
    [
        ("screenshot", "image/png", "image"),
        ("recording", "video/webm", "video"),
        ("screencast", None, "video"),
        ("audio", "audio/wav", "audio"),
        ("transcript", "text/vtt", "document"),
        ("document", "text/markdown", "document"),
        ("generated_file", "application/zip", "document"),
        ("trace", "application/zip", "document"),
        ("unknown_kind", "video/mp4", "video"),
        ("unknown_kind", "application/zip", "document"),
    ],
)
def test_modality_for(kind, ct, expected):
    assert modality_for(kind, ct) == expected


# --------------------------------------------------------------------------
# Round trips over every kind
# --------------------------------------------------------------------------

SAMPLES = [
    ("screenshot", "image/png", {"data": PNG}),
    ("recording", "video/webm", {"data": b"\x1a\x45\xdf\xa3" + b"\x02" * 30}),
    ("screencast", "image/jpeg", {"data": b"\xff\xd8\xff" + b"\x03" * 20}),
    ("audio", "audio/wav", {"data": WAV}),
    ("transcript", "text/vtt", {"text": VTT}),
    ("transcript", "text/plain", {"text": "speaker 1: hallo"}),
    ("document", "text/markdown", {"text": "# Report\n\nok"}),
    ("document", "application/json", {"text": '{"a":1}'}),
    ("generated_file", "application/zip", {"data": ZIP}),
    ("trace", "application/zip", {"data": ZIP}),
    ("trace", "application/zip", {"uri": "https://p.test/a/t"}),
]
LABEL_SETS = [{}, {"site": "Heilbronn", "retention_class": "short", "tags": ["a", "b"]}]


def _cases():
    for kind, ct, content in SAMPLES:
        for labels in LABEL_SETS:
            yield pytest.param(
                kind,
                ct,
                content,
                labels,
                id=f"{kind}-{ct}-{next(iter(content))}-{len(labels)}",
            )


def _key(p: ArtifactPayload):
    return (p.content_type, p.sha256, p.name, p.kind, p.labels)


def _orig(kind, ct, content, labels):
    sha256 = "f" * 64 if "uri" in content else None
    return make_payload(
        kind=kind,
        name=f"{kind}.bin",
        content_type=ct,
        labels=labels,
        sha256=sha256,
        **content,
    )


@pytest.mark.parametrize("kind,ct,content,labels", list(_cases()))
@pytest.mark.parametrize("inline", [True, False])
def test_round_trip_mcp(kind, ct, content, labels, inline):
    p = _orig(kind, ct, content, labels)
    uri = p.uri or "https://p.test/artifacts/1"
    back = from_mcp_content_block(
        to_mcp_content_block(p, uri=uri, inline=inline), kind=None
    )
    assert _key(back) == _key(p)
    # Also without a uri when the payload can be inlined.
    if p.uri is None and (p.text is not None or ct.split("/")[0] in ("image", "audio")):
        back = from_mcp_content_block(
            to_mcp_content_block(p, uri=None, inline=True), kind=None
        )
        assert _key(back) == _key(p)
        assert shapes.content_sha256(back.data, back.text) == p.sha256


@pytest.mark.parametrize("kind,ct,content,labels", list(_cases()))
def test_round_trip_a2a(kind, ct, content, labels):
    p = _orig(kind, ct, content, labels)
    back = from_a2a_part(json.loads(json.dumps(to_a2a_part(p))))
    assert _key(back) == _key(p)
    assert (back.data, back.text, back.uri) == (p.data, p.text, p.uri)


@pytest.mark.parametrize("kind,ct,content,labels", list(_cases()))
@pytest.mark.parametrize("with_uri", [True, False])
def test_round_trip_otel(kind, ct, content, labels, with_uri):
    p = _orig(kind, ct, content, labels)
    if not with_uri and p.uri:
        pytest.skip("a reference without bytes has no BlobPart form")
    uri = (p.uri or "https://p.test/artifacts/1") if with_uri else None
    part = to_otel_part(p, uri=uri)
    attrs = otel_span_attributes(p)
    back = from_otel_part(json.loads(json.dumps(part)), attributes=attrs)
    assert _key(back) == _key(p)
    assert part["modality"] == modality_for(kind, ct)


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_module_imports_without_db_settings_or_models():
    backend = Path(__file__).resolve().parents[2]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE", "POSTGRES", "DB_", "PRELOOP_"))
    }
    env["PYTHONPATH"] = str(backend)
    code = (
        "import sys, preloop.services.artifact_shapes as m\n"
        "bad = sorted(n for n in sys.modules if n.startswith(('preloop.models', 'preloop.config', 'sqlalchemy')))\n"
        "assert not bad, bad\n"
        "assert m.from_mcp_content_block({'type': 'text', 'text': 'x'}, kind=None).kind == 'document'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=backend,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# Review follow-ups (PR #1109)
# --------------------------------------------------------------------------


def test_otel_out_falls_back_to_payload_uri():
    p = _payload(uri="https://p.test/a/t", content_type="application/zip", kind="trace")
    assert to_otel_part(p) == {
        "type": "uri",
        "mime_type": "application/zip",
        "modality": "document",
        "uri": "https://p.test/a/t",
    }


@pytest.mark.parametrize("kind,ct,content,labels", list(_cases()))
@pytest.mark.parametrize("inline", [True, False])
def test_round_trip_mcp_keeps_uri(kind, ct, content, labels, inline):
    p = _orig(kind, ct, content, labels)
    uri = p.uri or "https://p.test/artifacts/1"
    back = from_mcp_content_block(to_mcp_content_block(p, uri=uri, inline=inline), kind=None)
    assert back.uri == uri


@pytest.mark.parametrize("ct", ["video/webm", "video/mp4"])
def test_video_without_kind_is_recording_with_video_modality(ct):
    p = from_a2a_part({"raw": b64(b"\x1a\x45\xdf\xa3"), "media_type": ct})
    assert p.kind == "recording"
    assert to_otel_part(p)["modality"] == "video"


def test_from_a2a_artifact_reads_proto_and_camel_names():
    a = to_a2a_artifact("art-1", "run", [_payload(text="a")])
    artifact_id, name, payloads = shapes.from_a2a_artifact(a)
    assert (artifact_id, name, [p.text for p in payloads]) == ("art-1", "run", ["a"])
    camel = {"artifactId": "art-2", "parts": [{"text": "b", "mediaType": "text/plain"}]}
    assert shapes.from_a2a_artifact(camel)[0] == "art-2"
    with pytest.raises(ValueError, match=ERROR_BLOCK_TYPE_UNSUPPORTED):
        shapes.from_a2a_artifact({"parts": []})


@pytest.mark.parametrize("data", [b"\xfb\xff\xfe", b"\xfb\xff", b"\xfb"])
def test_a2a_raw_accepts_urlsafe_unpadded_base64(data):
    encoded = base64.urlsafe_b64encode(data).decode().rstrip("=")
    assert from_a2a_part({"raw": encoded}).data == data


def test_mcp_data_stays_standard_base64_only():
    encoded = base64.urlsafe_b64encode(b"\xfb\xff\xfe").decode()
    with pytest.raises(ValueError, match=ERROR_BLOCK_INVALID_BASE64):
        from_mcp_content_block({"type": "image", "data": encoded, "mimeType": "image/png"}, kind=None)
