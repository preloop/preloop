"""Plain text extraction for session artifacts (#1082).

Only the text kinds are read here: ``transcript`` (plain text, WebVTT, SRT or
JSON segments) and ``document`` (plain text, markdown, JSON). Everything else,
PDF included, has no extracted text and is indexed by name, labels and tool
only (``text_status="none"``). OCR, captions and transcription are later work.

Transcripts with timings come back as one line per cue, each line remembering
the cue start in seconds, so the indexer can stamp every chunk with the start
time of the cue it begins in. Speaker voice tags become a ``Speaker:`` prefix.

The extracted text is capped at :data:`TEXT_CAP_BYTES` of UTF-8. Past the cap
the first part is kept and ``truncated`` is set; the caller records that on
the artifact manifest as ``text_truncated``.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from preloop.services.artifact_media import normalize_media_type

#: Extracted text kept per artifact, in UTF-8 bytes.
TEXT_CAP_BYTES = 1024**2

TEXT_STATUS_NONE = "none"
TEXT_STATUS_EXTRACTED = "extracted"

KIND_TRANSCRIPT = "transcript"
KIND_DOCUMENT = "document"

_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
)
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
_TAG_RE = re.compile(r"</?[^>\n]{0,256}>")
_BLANK_LINES_RE = re.compile(r"\n\s*\n")

_TRANSCRIPT_TYPES = frozenset(
    {"text/plain", "text/vtt", "application/x-subrip", "application/json"}
)
_DOCUMENT_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})


@dataclass(frozen=True)
class CueLine:
    """One transcript cue rendered as a single line of text."""

    start: Optional[float]
    text: str


@dataclass(frozen=True)
class ExtractedText:
    """What :func:`extract_text` read out of an artifact.

    Exactly one of ``cues`` and ``text`` carries the content: ``cues`` for a
    timed transcript (one line each), ``text`` for everything else.
    """

    text: str = ""
    cues: Tuple[CueLine, ...] = field(default_factory=tuple)
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not any(c.text for c in self.cues)


def supports(kind: str, content_type: str) -> bool:
    """Whether this kind and media type has extractable text."""
    media = normalize_media_type(content_type or "")
    if kind == KIND_TRANSCRIPT:
        return media in _TRANSCRIPT_TYPES
    if kind == KIND_DOCUMENT:
        return media in _DOCUMENT_TYPES
    return False


def extract_text(kind: str, content_type: str, data: bytes) -> Optional[ExtractedText]:
    """Return the plain text of an artifact, or ``None`` when it has none.

    Raises nothing for malformed content: unparseable JSON is indexed as
    the raw text, and a cue file without cues is indexed as plain text.
    """
    if not supports(kind, content_type):
        return None
    media = normalize_media_type(content_type)
    raw = _decode(data)
    if kind == KIND_TRANSCRIPT and media in ("text/vtt", "application/x-subrip"):
        cues = parse_cues(raw)
        if cues:
            return _cap_cues(cues)
    if media == "application/json":
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _cap_text(raw)
        if kind == KIND_TRANSCRIPT:
            cues = json_segments(parsed)
            if cues:
                return _cap_cues(cues)
        return _cap_text(json.dumps(parsed, indent=2, ensure_ascii=False))
    return _cap_text(raw)


def parse_cues(raw: str) -> List[CueLine]:
    """Parse WebVTT or SRT into one line per cue.

    Header, ``NOTE``, ``STYLE`` and ``REGION`` blocks and cue identifiers are
    dropped: any block without a timing line is not a cue.
    """
    cues: List[CueLine] = []
    for block in _BLANK_LINES_RE.split(raw.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip("\n").split("\n")
        for index, line in enumerate(lines):
            match = _TIMING_RE.match(line)
            if match is None:
                continue
            text = _cue_payload(lines[index + 1 :])
            if text:
                cues.append(CueLine(start=_seconds(match.group("start")), text=text))
            break
    return cues


def json_segments(parsed: Any) -> List[CueLine]:
    """Read ``[{start, end, speaker?, text}]`` or ``{"segments": [...]}``."""
    segments = parsed.get("segments") if isinstance(parsed, dict) else parsed
    if not isinstance(segments, list) or not segments:
        return []
    cues: List[CueLine] = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            return []
        text = " ".join(segment["text"].split())
        speaker = segment.get("speaker")
        if isinstance(speaker, str) and speaker.strip():
            text = f"{speaker.strip()}: {text}"
        if text:
            cues.append(CueLine(start=_json_seconds(segment.get("start")), text=text))
    return cues


def _cue_payload(lines: Sequence[str]) -> str:
    joined = " ".join(line.strip() for line in lines if line.strip())
    joined = _VOICE_RE.sub(lambda m: f"{m.group(1).strip()}: ", joined, count=1)
    joined = _TAG_RE.sub("", joined)
    return " ".join(html.unescape(joined).split())


def _seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return round(total, 3)


def _json_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    if isinstance(value, str):
        try:
            return _seconds(value.strip())
        except ValueError:
            return None
    return None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").lstrip("\ufeff")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cap_text(text: str) -> ExtractedText:
    text = text.strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= TEXT_CAP_BYTES:
        return ExtractedText(text=text)
    head = encoded[:TEXT_CAP_BYTES].decode("utf-8", errors="ignore")
    return ExtractedText(text=head, truncated=True)


def _cap_cues(cues: Sequence[CueLine]) -> ExtractedText:
    kept: List[CueLine] = []
    used = 0
    for cue in cues:
        size = _utf8_len(cue.text) + 1
        if used + size > TEXT_CAP_BYTES:
            room = TEXT_CAP_BYTES - used
            if room > 1:
                head = cue.text.encode("utf-8")[: room - 1].decode(
                    "utf-8", errors="ignore"
                )
                if head:
                    kept.append(CueLine(start=cue.start, text=head))
            return ExtractedText(cues=tuple(kept), truncated=True)
        kept.append(cue)
        used += size
    return ExtractedText(cues=tuple(kept))
