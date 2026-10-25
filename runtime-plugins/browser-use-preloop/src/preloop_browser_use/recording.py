"""Build a time-faithful GIF from step screenshots, with no text overlays.

Browser Use's stock GIF uses a fixed 3 s per frame (and a title frame), so
wall-clock offsets cannot truthfully seek it. This export keeps elapsed
step times, rounded to GIF's 10 ms timebase. Pillow is an optional extra.
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any

from preloop_browser_use.convert import resolve_screenshot_path


def history_gif(
    steps: list[dict[str, Any]], started_at: datetime | None, path: Path
) -> dict[str, Any] | None:
    """Write completed screenshots on their observed timebase, or write nothing."""
    from PIL import Image

    images, offsets = [], []
    total_pixels = 0
    for raw in steps[:5000]:
        step = resolve_screenshot_path(dict(raw))
        shot = step.get("screenshot")
        if not shot or (started_at is not None and not step.get("occurred_at")):
            continue
        if started_at is None:
            offset = len(images) * 3000
        else:
            stamp = datetime.fromisoformat(step["occurred_at"])
            offset = round((stamp - started_at).total_seconds() * 100) * 10
        if offset < 0 or (offsets and offset <= offsets[-1]["offset_ms"]):
            continue
        with Image.open(io.BytesIO(base64.b64decode(shot["data_base64"]))) as image:
            # Bound generated frame memory; a history export is a preview.
            image.thumbnail((1280, 720))
            total_pixels += image.width * image.height
            if total_pixels > 32_000_000:
                raise ValueError("history GIF exceeds the decoded frame budget")
            images.append(image.convert("RGB"))
        offsets.append({"source_step_id": step["source_step_id"], "offset_ms": offset})
    if not images:
        return None
    # A blank initial frame keeps t=0 at the start of capture, instead of
    # showing a finished action before it happened.
    positions = [entry["offset_ms"] for entry in offsets]
    if positions[0] > 0:
        images.insert(0, Image.new("RGB", images[0].size))
        positions.insert(0, 0)
    durations = [b - a for a, b in zip(positions, positions[1:], strict=False)] + [1000]
    if any(duration > 655350 for duration in durations):
        raise ValueError("GIF frame delay exceeds its timebase")
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=False,
    )
    if started_at is None:
        return None
    return {
        "started_at": started_at.isoformat(),
        "clock": "agent",
        "duration_ms": sum(durations),
        "step_offsets": offsets,
    }
