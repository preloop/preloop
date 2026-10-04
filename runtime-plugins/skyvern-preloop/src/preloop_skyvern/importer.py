"""Import one Skyvern task into a Preloop runtime session.

Each Skyvern step becomes one browser step with ``source="skyvern"`` and
``source_step_id = "<task_id>:<step_id>"``, so a re-import is counted as
duplicates. The step's action screenshot (or, failing that, the
screenshot the model saw) rides on the step. HAR files and Playwright
traces are deposited as ``trace`` artifacts and the video as
``recording``, each with an ``Idempotency-Key`` derived from the Skyvern
artifact id. Typed text is never copied.
"""

from __future__ import annotations

import base64
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from preloop_skyvern.preloop_api import (
    DepositUnavailableError,
    PreloopClient,
    StepTotals,
)
from preloop_skyvern.skyvern_api import SkyvernClient

logger = logging.getLogger("preloop_skyvern")

SOURCE = "skyvern"
#: Preloop's per-step screenshot limit (RUNTIME_SESSION_SCREENSHOT_MAX_BYTES).
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
ARTIFACT_MAX_BYTES = 200 * 1024 * 1024

ACTION_MAP = {
    "click": "click",
    "checkbox": "click",
    "input_text": "type",
    "upload_file": "type",
    "keypress": "type",
    "select_option": "select",
    "scroll": "scroll",
    "wait": "wait",
    "extract": "extract",
    "complete": "done",
    "terminate": "done",
    "goto_url": "navigate",
    "reload_page": "navigate",
}
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "terminated", "timed_out", "canceled"}
)
_SCREENSHOT_TYPES = ("screenshot_action", "screenshot_llm")
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


@dataclass
class ImportReport:
    """What one import stored, repeated or skipped."""

    task_id: str
    steps: StepTotals = field(default_factory=StepTotals)
    screenshots: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _image_type(data: bytes) -> str | None:
    for magic, content_type in _IMAGE_MAGIC:
        if data.startswith(magic):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # Skyvern stores UTC
    return parsed.isoformat()


def _actions(step: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    output = step.get("output") or {}
    pairs = []
    for pair in output.get("actions_and_results") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            action, results = pair
            if isinstance(action, dict):
                pairs.append((action, list(results or [])))
    return pairs


def step_to_browser_step(
    step: dict[str, Any],
    *,
    task: dict[str, Any],
    index: int,
    screenshot: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    """Map one Skyvern step to a ``BrowserStepIn`` JSON object."""
    pairs = _actions(step)
    first = next(
        (a for a, _ in pairs if a.get("action_type") not in (None, "null_action")),
        pairs[0][0] if pairs else {},
    )
    action_type = str(first.get("action_type") or "")
    reasoning = first.get("reasoning") or first.get("intention")
    # Only the exception type: the message can quote the typed input.
    errors = [
        r.get("exception_type") or "action_failed"
        for _, results in pairs
        for r in results
        if isinstance(r, dict) and r.get("success") is False
    ]
    status = str(step.get("status") or "")
    if status == "completed" and not errors:
        outcome = "success"
    elif status in ("failed", "canceled") or errors:
        outcome = "failed"
    else:
        outcome = "unknown"

    extra: dict[str, Any] = {
        "skyvern_task_id": task.get("task_id"),
        "skyvern_step_id": step.get("step_id"),
        "skyvern_order": step.get("order"),
        "skyvern_retry_index": step.get("retry_index"),
        "skyvern_actions": [str(a.get("action_type")) for a, _ in pairs][:20],
    }
    if errors:
        extra["error"] = str(errors[0])[:500]

    body: dict[str, Any] = {
        "source": SOURCE,
        "source_step_id": f"{task.get('task_id')}:{step.get('step_id')}"[:200],
        "step_index": index,
        "action": ACTION_MAP.get(action_type, "other"),
        "status": outcome,
        "extra": extra,
    }
    request = task.get("request") or {}
    if index == 0 and isinstance(request.get("url"), str):
        body["url"] = request["url"][:2048]
    if first.get("element_id"):
        body["target"] = f"skyvern element {first['element_id']}"[:1024]
    if reasoning:
        body["reasoning"] = str(reasoning)[:8000]
    occurred = _iso(step.get("modified_at")) or _iso(step.get("created_at"))
    if occurred:
        body["occurred_at"] = occurred
    if screenshot is not None:
        content_type, data = screenshot
        body["screenshot"] = {
            "content_type": content_type,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
    return body


def _pick_screenshot(
    skyvern: SkyvernClient, artifacts: list[dict[str, Any]], report: ImportReport
) -> tuple[str, bytes] | None:
    for wanted in _SCREENSHOT_TYPES:
        for artifact in artifacts:
            if artifact.get("artifact_type") != wanted:
                continue
            data = skyvern.download(artifact, max_bytes=SCREENSHOT_MAX_BYTES)
            content_type = _image_type(data) if data else None
            if content_type:
                return content_type, data
            report.skipped.append(f"{artifact.get('artifact_id')}: screenshot")
    return None


def _as_trace_zip(name: str, data: bytes) -> bytes:
    """Wrap a HAR (JSON) in a zip; Preloop stores traces as application/zip."""
    if data.startswith(b"PK\x03\x04"):
        return data
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return buffer.getvalue()


def _deposit_files(
    skyvern: SkyvernClient,
    preloop: PreloopClient,
    task_id: str,
    artifacts: list[dict[str, Any]],
    report: ImportReport,
) -> None:
    for artifact in artifacts:
        kind_name = artifact.get("artifact_type")
        if kind_name not in ("har", "trace", "recording"):
            continue
        artifact_id = str(artifact.get("artifact_id"))
        data = skyvern.download(artifact, max_bytes=ARTIFACT_MAX_BYTES)
        if not data:
            report.skipped.append(f"{artifact_id}: {kind_name} download failed")
            continue
        if kind_name == "recording":
            webm = data.startswith(b"\x1a\x45\xdf\xa3")
            kind, name = "recording", f"skyvern-{task_id}.{'webm' if webm else 'mp4'}"
            content_type = "video/webm" if webm else "video/mp4"
        else:
            kind, content_type = "trace", "application/zip"
            inner = f"skyvern-{task_id}.har" if kind_name == "har" else "trace.zip"
            name = f"skyvern-{task_id}-{kind_name}.zip"
            data = _as_trace_zip(inner, data)
        stored = preloop.deposit(
            kind=kind,
            name=name,
            content_type=content_type,
            data=data,
            idempotency_key=f"skyvern:{task_id}:{artifact_id}",
            labels={"tags": ["skyvern", kind_name]},
        )
        if stored is None:
            report.skipped.append(f"{artifact_id}: {kind_name} refused")
        else:
            report.artifacts.append(stored)


def import_task(
    task_id: str,
    *,
    skyvern: SkyvernClient,
    preloop: PreloopClient,
    include_files: bool = True,
) -> ImportReport:
    """Fetch a Skyvern task and write it to the Preloop session.

    Safe to repeat: steps dedupe on ``source_step_id`` and artifact
    deposits replay on their ``Idempotency-Key``.
    """
    report = ImportReport(task_id=task_id)
    task = skyvern.get_task(task_id)
    steps = skyvern.get_steps(task_id)
    browser_steps = []
    files: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        artifacts = skyvern.get_step_artifacts(task_id, str(step.get("step_id")))
        shot = _pick_screenshot(skyvern, artifacts, report)
        report.screenshots += shot is not None
        browser_steps.append(
            step_to_browser_step(step, task=task, index=index, screenshot=shot)
        )
        files.extend(artifacts)
    if browser_steps:
        report.steps = preloop.post_steps(browser_steps)
    if include_files:
        try:
            _deposit_files(skyvern, preloop, task_id, files, report)
        except DepositUnavailableError:
            logger.info(
                "Skipping HAR, trace and recording: this Preloop server has no "
                "artifact deposit API (preloop/preloop#1080)"
            )
            report.skipped.append("files: artifact deposit API unavailable (#1080)")
    return report
