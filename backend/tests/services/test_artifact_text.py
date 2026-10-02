"""Plain text extraction for transcript and document artifacts (#1082)."""

from __future__ import annotations

import json

from preloop.services import artifact_text as at
from preloop.services.session_search_index import chunk_spans, chunk_text

VTT = """WEBVTT
Kind: captions
Language: en

NOTE
Header note that must not be indexed.

intro
00:00:00.000 --> 00:00:05.000
<v Receiving Lead>Reporting a damaged pallet &amp; torn cartons.

00:01:05.500 --> 00:01:10.000 align:start
<v.loud Shift Supervisor>Open a <b>claim</b>
with the carrier.
"""

SRT = """1
00:00:01,250 --> 00:00:03,000
First line

2
01:00:02,000 --> 01:00:04,000
Second <i>line</i>
"""


def test_vtt_drops_headers_notes_and_timings_and_keeps_cue_starts():
    out = at.extract_text("transcript", "text/vtt", VTT.encode())
    assert out is not None and not out.truncated
    assert [(c.start, c.text) for c in out.cues] == [
        (0.0, "Receiving Lead: Reporting a damaged pallet & torn cartons."),
        (65.5, "Shift Supervisor: Open a claim with the carrier."),
    ]
    joined = " ".join(c.text for c in out.cues)
    assert "-->" not in joined and "NOTE" not in joined and "Kind" not in joined


def test_srt_cues():
    out = at.extract_text("transcript", "application/x-subrip", SRT.encode())
    assert [(c.start, c.text) for c in out.cues] == [
        (1.25, "First line"),
        (3602.0, "Second line"),
    ]


def test_json_segments_list_and_object_forms():
    segments = [
        {"start": 1.5, "end": 3, "speaker": "Ana", "text": "damaged pallet"},
        {"start": "00:00:04.000", "end": 5, "text": "second"},
    ]
    for body in (segments, {"segments": segments}):
        out = at.extract_text(
            "transcript", "application/json", json.dumps(body).encode()
        )
        assert [(c.start, c.text) for c in out.cues] == [
            (1.5, "Ana: damaged pallet"),
            (4.0, "second"),
        ]


def test_json_transcript_without_segments_is_pretty_printed():
    out = at.extract_text("transcript", "application/json", b'{"note":"x"}')
    assert out.cues == () and out.text == '{\n  "note": "x"\n}'


def test_documents_plain_markdown_json_and_pdf():
    assert at.extract_text("document", "text/markdown", b"# Summary\n").text == (
        "# Summary"
    )
    assert at.extract_text("document", "text/plain; charset=utf-8", b"a").text == "a"
    assert at.extract_text(
        "document", "application/json", b'[1,{"a":"\xc3\xa4"}]'
    ).text == ('[\n  1,\n  {\n    "a": "ä"\n  }\n]')
    assert at.extract_text("document", "application/pdf", b"%PDF-1.7") is None
    assert at.extract_text("screenshot", "image/png", b"\x89PNG") is None


def test_plain_text_is_capped_at_one_mib():
    data = ("a" * 1023 + "\n").encode() * 3 * 1024
    out = at.extract_text("transcript", "text/plain", data)
    assert out.truncated
    assert len(out.text.encode()) <= at.TEXT_CAP_BYTES
    assert len(out.text.encode()) > at.TEXT_CAP_BYTES - 1024


def test_cues_are_capped_at_one_mib():
    cue = "00:00:01.000 --> 00:00:02.000\n" + "word " * 100 + "\n\n"
    out = at.extract_text(
        "transcript", "text/vtt", ("WEBVTT\n\n" + cue * 5000).encode()
    )
    assert out.truncated
    assert sum(len(c.text.encode()) + 1 for c in out.cues) <= at.TEXT_CAP_BYTES


def test_chunk_spans_offsets_point_at_the_chunk_text():
    text = " ".join(f"w{i}" for i in range(2000))
    spans = chunk_spans(text)
    assert [chunk for _o, chunk in spans] == chunk_text(text)
    for offset, chunk in spans:
        assert text[offset : offset + len(chunk)] == chunk
    assert len(chunk_spans(text * 50, max_chunks=200)) == 200
