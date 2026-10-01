"""Output shapes, data validity and determinism of the warehouse-sim tools."""

import base64
import hashlib
import io
import json
import re
import wave

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from scripts.fixtures.warehouse_sim import data, server

TS = r"(?:(\d{2,}):)?([0-5]\d):([0-5]\d)\.(\d{3})"
CUE_TIMING = re.compile(rf"^{TS} --> {TS}(?: .*)?$")


def _secs(groups):
    h, m, s, ms = groups
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text):
    """Small strict WebVTT parser: header, NOTE blocks, ordered cues."""
    assert text.startswith("WEBVTT"), "missing WEBVTT signature"
    blocks = [b for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    header = blocks[0].split("\n")
    assert header[0] == "WEBVTT" or header[0].startswith(("WEBVTT ", "WEBVTT\t"))
    assert all("-->" not in line for line in header)
    cues, last_start = [], -1.0
    for block in blocks[1:]:
        lines = block.split("\n")
        if lines[0].startswith("NOTE"):
            assert all("-->" not in line for line in lines)
            continue
        if "-->" not in lines[0]:
            lines = lines[1:]  # optional cue identifier
        m = CUE_TIMING.match(lines[0])
        assert m, f"bad cue timing {lines[0]!r}"
        start, end = _secs(m.groups()[:4]), _secs(m.groups()[4:])
        assert end > start and start >= last_start
        last_start = start
        payload = "\n".join(lines[1:]).strip()
        assert payload and "-->" not in payload
        cues.append((start, end, payload))
    assert cues, "no cues"
    return cues


def dump(block):
    return block.model_dump(mode="json", by_alias=True, exclude_none=True)


@pytest.mark.parametrize("site,shift", data.all_transcripts())
def test_every_transcript_is_valid_webvtt(site, shift):
    cues = parse_vtt(data.read_transcript(site, shift))
    assert len(cues) >= 3


def test_six_transcripts_two_sites_two_languages():
    pairs = data.all_transcripts()
    assert len(pairs) == 6 and {s for s, _ in pairs} == {"nord", "sued"}
    langs = {data.SUMMARIES[p][0] for p in pairs}
    assert langs == {"de", "en"}
    for p in pairs:
        lang = data.SUMMARIES[p][0]
        assert f"Language: {lang}" in data.read_transcript(*p)


@pytest.mark.parametrize("site,shift", data.all_transcripts())
def test_get_transcript_shape(site, shift):
    text, res = [dump(b) for b in server.get_transcript(site, shift)]
    assert text["type"] == "text" and site in text["text"]
    assert res["type"] == "resource"
    assert res["resource"]["uri"] == f"warehouse-sim://transcripts/{site}/{shift}.vtt"
    assert res["resource"]["mimeType"] == "text/vtt"
    assert res["resource"]["text"] == data.read_transcript(site, shift)
    assert "blob" not in res["resource"]


def test_get_transcript_rejects_unknown_site_and_shift():
    with pytest.raises(ToolError):
        server.get_transcript("west", "early")
    with pytest.raises(ToolError):
        server.get_transcript("nord", "../sued/early")


def test_list_workflows_shape():
    (block,) = [dump(b) for b in server.list_workflows("sued")]
    assert block["type"] == "text"
    wfs = json.loads(block["text"])["workflows"]
    assert [w["workflow_id"] for w in wfs] == ["picking-route", "goods-receipt"]
    for w in wfs:
        path = data.HERE / w["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == w["sha256"]
        assert "<bpmn:process" in path.read_text()


def test_propose_workflow_change_is_deterministic_and_validated():
    args = ("sued", "picking-route", "- aisle 3\n+ aisle 11", "Complaint sued/early")
    (a,) = server.propose_workflow_change(*args)
    (b,) = server.propose_workflow_change(*args)
    body = json.loads(dump(a)["text"])
    assert dump(a) == dump(b) and body["change_id"].startswith("chg-")
    assert body["status"] == "proposed" and body["justification_received"] is True
    # Behind Preloop the justification argument is consumed by the firewall
    # and not forwarded, so its absence must not fail the call.
    (c,) = server.propose_workflow_change("sued", "picking-route", "- a\n+ b")
    assert json.loads(dump(c)["text"])["justification_received"] is False
    with pytest.raises(ToolError):
        server.propose_workflow_change("sued", "picking-route", "  ", "why")
    with pytest.raises(ToolError):
        server.propose_workflow_change("sued", "nope", "x", "why")


def test_create_task_shape():
    (block,) = [dump(b) for b in server.create_task("nord", "Check mirror", "Aisle 14")]
    assert block["type"] == "text"
    assert json.loads(block["text"])["task_id"].startswith("task-")
    with pytest.raises(ToolError):
        server.create_task("nord", " ", "x")


@pytest.mark.parametrize("site,shift", data.all_transcripts())
def test_get_audio_shape_and_size(site, shift):
    (block,) = [dump(b) for b in server.get_audio(site, shift)]
    assert set(block) >= {"type", "data", "mimeType"}
    assert block["type"] == "audio" and block["mimeType"] == "audio/wav"
    raw = base64.b64decode(block["data"], validate=True)
    assert len(raw) < 200 * 1024
    with wave.open(io.BytesIO(raw)) as w:
        assert w.getnchannels() == 1 and w.getnframes() > 0


def test_audio_clips_are_distinct():
    hashes = {
        hashlib.sha256(data.audio_wav(*p)).hexdigest() for p in data.all_transcripts()
    }
    assert len(hashes) == 6


@pytest.mark.parametrize("site,shift", data.all_transcripts())
def test_transcribe_audio_is_deterministic_for_every_ref_form(site, shift):
    clip = data.audio_wav(site, shift)
    refs = [
        f"warehouse-sim://audio/{site}/{shift}.wav",
        f"{site}/{shift}",
        dump(server.get_audio(site, shift)[0])["data"],
        hashlib.sha256(clip).hexdigest(),
    ]
    expected = [dump(b) for b in server.get_transcript(site, shift)]
    for ref in refs:
        first = [dump(b) for b in server.transcribe_audio(ref)]
        second = [dump(b) for b in server.transcribe_audio(ref)]
        assert first == second == expected


def test_transcribe_audio_rejects_unknown_refs():
    for ref in ("", "nord/noon", base64.b64encode(b"RIFFnope").decode(), "0" * 64):
        with pytest.raises(ToolError):
            server.transcribe_audio(ref)
