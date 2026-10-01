"""Synthetic data for the warehouse-sim fixture: transcripts, workflows, audio.

Everything here is deterministic and offline. Audio clips are generated on
the fly (no binary files in the repo) so the same (site, shift) always
yields byte-identical WAV data and the same sha256.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
import struct
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
TRANSCRIPT_DIR = HERE / "transcripts"
BPMN_DIR = HERE / "bpmn"

SITES: Tuple[str, ...] = ("nord", "sued")
SHIFTS: Tuple[str, ...] = ("early", "late", "night")

TRANSCRIPT_URI = "warehouse-sim://transcripts/{site}/{shift}.vtt"
AUDIO_URI = "warehouse-sim://audio/{site}/{shift}.wav"
VTT_MIME = "text/vtt"
WAV_MIME = "audio/wav"

# One summary line per transcript, returned as the leading TextContent.
SUMMARIES: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("nord", "early"): ("de", "Shift handover: dock 5 queue, zone B at 80 percent"),
    ("nord", "late"): ("en", "Damaged pallet report: delivery 4711, lane Q2"),
    ("nord", "night"): ("de", "Forklift near miss in aisle 14, nobody hurt"),
    ("sued", "early"): ("de", "Picking-route complaint: aisle 3 and 11 back and forth"),
    ("sued", "late"): ("en", "Inventory recount: bin C-07-03 short by 12 units"),
    ("sued", "night"): ("en", "Carrier contact handover with name, email and phones"),
}

WORKFLOWS: Tuple[str, ...] = ("picking-route", "goods-receipt")

# Audio: 8 kHz, 16-bit mono, 1.5 s per clip, about 24 KB.
AUDIO_RATE = 8000
AUDIO_SECONDS = 1.5
_BASE_FREQ = {"nord": 440.0, "sued": 523.25}
_SHIFT_STEP = {"early": 1.0, "late": 1.25, "night": 1.5}


class FixtureError(ValueError):
    """Unknown site, shift, workflow or audio reference."""


def check_site(site: str) -> str:
    if site not in SITES:
        raise FixtureError(f"unknown site {site!r}; expected one of {list(SITES)}")
    return site


def check_shift(shift: str) -> str:
    if shift not in SHIFTS:
        raise FixtureError(f"unknown shift {shift!r}; expected one of {list(SHIFTS)}")
    return shift


def transcript_path(site: str, shift: str) -> Path:
    return TRANSCRIPT_DIR / check_site(site) / f"{check_shift(shift)}.vtt"


def read_transcript(site: str, shift: str) -> str:
    return transcript_path(site, shift).read_text(encoding="utf-8")


def all_transcripts() -> List[Tuple[str, str]]:
    return [(site, shift) for site in SITES for shift in SHIFTS]


def list_workflows(site: str) -> List[Dict[str, str]]:
    check_site(site)
    out = []
    for wf in WORKFLOWS:
        path = BPMN_DIR / site / f"{wf}.bpmn"
        out.append(
            {
                "workflow_id": wf,
                "site": site,
                "format": "bpmn",
                "path": f"bpmn/{site}/{wf}.bpmn",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return out


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:12]}"


def audio_wav(site: str, shift: str) -> bytes:
    """Deterministic short tone for (site, shift)."""
    freq = _BASE_FREQ[check_site(site)] * _SHIFT_STEP[check_shift(shift)]
    frames = int(AUDIO_RATE * AUDIO_SECONDS)
    pcm = b"".join(
        struct.pack(
            "<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / AUDIO_RATE))
        )
        for i in range(frames)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def audio_b64(site: str, shift: str) -> str:
    return base64.b64encode(audio_wav(site, shift)).decode("ascii")


def resolve_audio_ref(audio_ref: str) -> Tuple[str, str]:
    """Map an audio reference to (site, shift).

    Accepted forms: the clip URI ``warehouse-sim://audio/<site>/<shift>.wav``,
    ``<site>/<shift>``, the base64 ``data`` of an AudioContent returned by
    ``get_audio``, or the hex sha256 of the clip bytes.
    """
    ref = (audio_ref or "").strip()
    if not ref:
        raise FixtureError("audio_ref is empty")
    for site, shift in all_transcripts():
        if ref in (AUDIO_URI.format(site=site, shift=shift), f"{site}/{shift}"):
            return site, shift
    by_hash = _match_bytes(ref)
    if by_hash:
        return by_hash
    raise FixtureError(f"unknown audio_ref {ref[:80]!r}")


def _match_bytes(ref: str) -> Optional[Tuple[str, str]]:
    want_hash = ref.lower() if len(ref) == 64 else None
    raw: Optional[bytes] = None
    if want_hash is None:
        try:
            raw = base64.b64decode(ref, validate=True)
        except ValueError:
            return None
    for site, shift in all_transcripts():
        clip = audio_wav(site, shift)
        if raw is not None and raw == clip:
            return site, shift
        if want_hash and hashlib.sha256(clip).hexdigest() == want_hash:
            return site, shift
    return None
