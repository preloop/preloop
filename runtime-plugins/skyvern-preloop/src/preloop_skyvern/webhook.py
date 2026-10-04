"""Framework-neutral handler for Skyvern's task webhook.

Skyvern POSTs the task response JSON to ``webhook_callback_url`` when a
task ends, signed with ``x-skyvern-signature`` (hex HMAC-SHA256 of the raw
body keyed with the Skyvern API key). Mount ``handle_webhook`` in any web
framework; it returns an HTTP status and a JSON-able body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from preloop_skyvern.importer import TERMINAL_STATUSES, ImportReport, import_task
from preloop_skyvern.preloop_api import PreloopClient
from preloop_skyvern.skyvern_api import SkyvernClient, SkyvernError

logger = logging.getLogger("preloop_skyvern")

SIGNATURE_HEADER = "x-skyvern-signature"


def verify_signature(body: bytes, signature: str | None, api_key: str) -> bool:
    """Return True when ``signature`` is the HMAC-SHA256 of ``body``."""
    if not signature:
        return False
    expected = hmac.new(api_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def handle_webhook(
    body: bytes,
    headers: Mapping[str, str],
    *,
    skyvern: SkyvernClient,
    skyvern_api_key: str,
    preloop_for_task: Callable[[dict[str, Any]], PreloopClient | None],
) -> tuple[int, dict[str, Any]]:
    """Verify, then import the task named in the webhook body.

    Args:
        body: Raw request body, exactly as received.
        headers: Request headers (any case).
        skyvern: Client used to fetch steps and artifacts.
        skyvern_api_key: Key that signs the webhook.
        preloop_for_task: Returns the Preloop client for the session this
            task belongs to, or ``None`` to ignore the task.

    Returns:
        ``(status, body)``: 401 bad signature, 400 bad payload, 202 ignored
        (not finished, or no session), 200 imported, 502 Skyvern unreachable.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    if not verify_signature(body, lowered.get(SIGNATURE_HEADER), skyvern_api_key):
        return 401, {"error": "invalid_signature"}
    try:
        payload = json.loads(body)
    except ValueError:
        return 400, {"error": "invalid_json"}
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return 400, {"error": "missing_task_id"}
    if payload.get("status") not in TERMINAL_STATUSES:
        return 202, {"ignored": "task_not_finished", "task_id": task_id}
    preloop = preloop_for_task(payload)
    if preloop is None:
        return 202, {"ignored": "no_session", "task_id": task_id}
    try:
        report = import_task(task_id, skyvern=skyvern, preloop=preloop)
    except SkyvernError as exc:
        logger.warning("Skyvern import of %s failed: %s", task_id, exc)
        return 502, {"error": "skyvern_unavailable", "task_id": task_id}
    return 200, summarize(report)


def summarize(report: ImportReport) -> dict[str, Any]:
    """JSON summary of an import, used by the CLI and the webhook."""
    return {
        "task_id": report.task_id,
        "steps_accepted": report.steps.accepted,
        "steps_duplicate": report.steps.duplicates,
        "steps_rejected": report.steps.rejected,
        "steps_failed_batches": report.steps.failed_batches,
        "screenshots": report.screenshots,
        "artifacts": [
            {"id": a.get("id"), "kind": a.get("kind"), "name": a.get("name")}
            for a in report.artifacts
        ],
        "skipped": report.skipped,
    }
