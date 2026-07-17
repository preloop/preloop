"""Stitch every recorded segment of a run into ONE continuous mp4.

Reads run-summary.json for chronological step order, builds a title card per
step (name, status, duration, timestamp), inserts each step's footage
(browser video, rendered terminal cast) or a log-tail card for terminal-less
steps, normalizes everything to 1920x1080/30fps/x264, and concatenates.

Output: <run-id>-oss-full-run.mp4 in the run artifact dir.

Follows the media/framework compositing conventions (render_cast.py /
video4_see_them create_video*.py): Preloop dark palette, ffmpeg concat of
uniformly encoded segments.

Usage: python lib/compose_video.py  (env: RIG_RUN_DIR)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riglib  # noqa: E402

W, H = 1920, 1080
FPS = 30
BG = "#212632"
FG = "#E6EDF3"
ACCENT = "#30C9E8"
GREEN = "#26D962"
RED = "#FF5D5D"
AMBER = "#F2A93B"
MENLO = "/System/Library/Fonts/Menlo.ttc"
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"

ENCODE = [
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-crf",
    "20",
    "-r",
    str(FPS),
    "-an",
]


def ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True)


def png_to_clip(png: Path, out: Path, seconds: float) -> None:
    ffmpeg(["-loop", "1", "-t", f"{seconds}", "-i", str(png), *ENCODE, str(out)])


def normalize(src: Path, out: Path) -> None:
    ffmpeg(
        [
            "-i",
            str(src),
            "-vf",
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={BG},fps={FPS}",
            *ENCODE,
            str(out),
        ]
    )


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def title_card(step: dict, run_id: str, out_png: Path) -> None:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    status = step["status"]
    color = {"pass": GREEN, "skip": AMBER}.get(status, RED)

    draw.text((120, 340), f"Step {step['id']}", fill=ACCENT, font=font(HELVETICA, 44))
    draw.text(
        (120, 410), step["name"].replace("-", " "), fill=FG, font=font(HELVETICA, 96)
    )
    draw.text((120, 560), status.upper(), fill=color, font=font(MENLO, 48))
    draw.text(
        (340, 566),
        f"{step['duration_s']:.0f}s   {step.get('finished_at', '')[:19]}Z",
        fill="#9aa3b2",
        font=font(MENLO, 40),
    )
    if step.get("notes"):
        note = step["notes"].splitlines()[0][:110]
        draw.text((120, 680), note, fill="#9aa3b2", font=font(HELVETICA, 34))
    draw.text(
        (120, H - 120),
        f"preloop e2e rig - {run_id}",
        fill="#5c667a",
        font=font(MENLO, 28),
    )
    img.save(out_png)


def log_card(step: dict, log_path: Path, out_png: Path) -> None:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.text(
        (100, 60),
        f"{step['id']} {step['name']} - log",
        fill=ACCENT,
        font=font(HELVETICA, 42),
    )
    lines: list[str] = []
    if log_path.exists():
        lines = log_path.read_text(errors="replace").splitlines()
    tail = [ln[:150] for ln in lines if ln.strip()][-30:]
    mono = font(MENLO, 24)
    y = 160
    for ln in tail:
        draw.text((100, y), ln, fill=FG, font=mono)
        y += 30
    img.save(out_png)


def main() -> None:
    run = riglib.run_dir()
    summary = json.loads((run / "run-summary.json").read_text())
    run_id = run.name
    work = run / "state" / "compose"
    work.mkdir(parents=True, exist_ok=True)

    segments: list[Path] = []

    def add_card(name: str, render) -> None:
        png = work / f"{name}.png"
        clip = work / f"{name}.mp4"
        render(png)
        png_to_clip(png, clip, 3.0)
        segments.append(clip)

    steps = sorted(summary["steps"], key=lambda s: s["id"])
    for step in steps:
        sid = step["id"]
        add_card(f"{sid}-title", lambda p, s=step: title_card(s, run_id, p))

        content: list[Path] = []
        # Browser footage recorded by this step.
        video_state = riglib.load_state(f"video-{sid}.json")
        if video_state and Path(video_state["path"]).exists():
            norm = work / f"{sid}-browser.mp4"
            normalize(Path(video_state["path"]), norm)
            content.append(norm)
        # Terminal cast rendered by this step.
        for cast_mp4 in sorted((run / "casts").glob(f"{sid}-*.mp4")):
            content.append(cast_mp4)  # already 1920x1080/30fps/x264

        if not content:
            # Terminal-less step: show the log tail.
            logs = sorted((run / "logs").glob(f"{sid}-*.log")) + sorted(
                (run / "logs").glob(f"{sid}-*.txt")
            )
            log_path = logs[0] if logs else run / "logs" / "missing.log"
            png = work / f"{sid}-log.png"
            clip = work / f"{sid}-log.mp4"
            log_card(step, log_path, png)
            png_to_clip(png, clip, 5.0)
            content.append(clip)

        segments.extend(content)

    # Closing card.
    def closing(png: Path) -> None:
        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)
        color = GREEN if summary.get("result") == "pass" else RED
        draw.text((120, 420), "Run complete", fill=FG, font=font(HELVETICA, 96))
        draw.text(
            (120, 580),
            str(summary.get("result", "?")).upper(),
            fill=color,
            font=font(MENLO, 56),
        )
        img.save(png)

    add_card("zz-closing", closing)

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segments))
    out = run / f"{run_id}-oss-full-run.mp4"
    ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(out),
        ]
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    duration = float(probe.stdout.strip())
    riglib.log(f"full-run video: {out} ({duration:.0f}s, {len(segments)} segments)")


if __name__ == "__main__":
    main()
