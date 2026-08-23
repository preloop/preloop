"""Thin wrapper around the gitleaks CLI (git mode).

This is not a secrets engine. It runs a pinned OSS scanner and returns
redacted pointers (commit, file, rule, line). Secret values are stripped
even if the binary forgot ``--redact``. The wrapper never runs
``git log -p`` / ``git show`` and never claims a MET verdict.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from preloop.security.checkout import CheckoutError, checkout_repository
from preloop.security.pins import RECOMMENDED_GITLEAKS_VERSION

logger = logging.getLogger(__name__)

GITLEAKS_TIMEOUT_SEC = 180
_SECRET_KEYS = frozenset(
    {
        "secret",
        "match",
        "author",
        "email",
        "message",
        "offender",
        "offenderentropy",
    }
)


def scanner_not_installed() -> Dict[str, Any]:
    """Return a structured error when gitleaks is not on PATH."""
    return {
        "error": "scanner_not_installed",
        "scanner": "gitleaks",
        "recommended_version": RECOMMENDED_GITLEAKS_VERSION,
        "message": (
            "gitleaks is not installed on the Preloop API server. "
            f"Pin gitleaks 8.24.x (recommended {RECOMMENDED_GITLEAKS_VERSION})."
        ),
    }


def _redact_finding(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only pointer fields. Drop any value-bearing keys."""
    rule = raw.get("RuleID") or raw.get("Rule") or raw.get("rule") or ""
    path = raw.get("File") or raw.get("FilePath") or raw.get("file") or ""
    commit = raw.get("Commit") or raw.get("commit") or ""
    line = raw.get("StartLine") or raw.get("Line") or raw.get("line")
    try:
        line_n = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_n = None
    return {
        "commit": str(commit),
        "file": str(path),
        "rule": str(rule),
        "line": line_n,
        "status": "finding",
    }


def _parse_gitleaks_json(text: str) -> List[Dict[str, Any]]:
    if not (text or "").strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict) and "findings" in parsed:
        parsed = parsed["findings"]
    if not isinstance(parsed, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            rows.append(_redact_finding(item))
    return rows


def _assert_no_secret_keys(findings: List[Dict[str, Any]]) -> None:
    blob = json.dumps(findings).lower()
    for key in _SECRET_KEYS:
        # Findings must not carry gitleaks value-bearing field names.
        if f'"{key}"' in blob:
            raise RuntimeError(f"refusing to emit gitleaks field {key!r}")


def run_gitleaks(repo: Path) -> Dict[str, Any]:
    """Run gitleaks detect (git mode) on an already-cloned work tree.

    Args:
        repo: Path to a git work tree on the API server.

    Returns:
        JSON-serializable dict of redacted findings. Never includes a
        MET/gap verdict. A finding count of 0 is just a count.
    """
    binary = shutil.which("gitleaks")
    if not binary:
        return scanner_not_installed()

    try:
        version_proc = subprocess.run(
            [binary, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        version = (version_proc.stdout or version_proc.stderr or "").strip() or (
            "unknown"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "error": "scanner_failed",
            "scanner": "gitleaks",
            "recommended_version": RECOMMENDED_GITLEAKS_VERSION,
            "message": f"gitleaks version check failed: {exc}",
        }

    with tempfile.NamedTemporaryFile(
        prefix="gitleaks-", suffix=".json", delete=False
    ) as report:
        report_path = Path(report.name)
    argv = [
        binary,
        "detect",
        "--no-banner",
        "--redact",
        "-s",
        str(repo),
        "-f",
        "json",
        "-r",
        str(report_path),
        "--exit-code",
        "0",
    ]
    # Refuse any invocation that could dump patches. This wrapper never
    # forwards caller-controlled git log flags.
    joined = " ".join(argv)
    if " -p" in f" {joined}" or "--patch" in joined or "git show" in joined:
        raise RuntimeError("gitleaks wrapper refuses patch-dumping git flags")

    try:
        detect = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=GITLEAKS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report_path.unlink(missing_ok=True)
        return {
            "error": "scanner_failed",
            "scanner": "gitleaks",
            "recommended_version": RECOMMENDED_GITLEAKS_VERSION,
            "message": f"gitleaks detect failed: {exc}",
        }

    report_text = ""
    if report_path.is_file():
        report_text = report_path.read_text(errors="replace")
        report_path.unlink(missing_ok=True)
    if not report_text.strip() and (detect.stdout or "").strip().startswith(("[", "{")):
        report_text = detect.stdout

    findings = _parse_gitleaks_json(report_text)
    payload = {
        "tool": "gitleaks_scan",
        "scanner": "gitleaks",
        "version": version,
        "recommended_version": RECOMMENDED_GITLEAKS_VERSION,
        "exit_code": detect.returncode,
        "finding_count": len(findings),
        "findings": findings,
    }
    if detect.returncode not in (0, 1) and not findings:
        err = (detect.stderr or detect.stdout or "").strip()
        payload["error"] = "scanner_failed"
        payload["message"] = err or "gitleaks detect failed"
    _assert_no_secret_keys(findings)
    return payload


def gitleaks_scan(
    repository_url: str,
    ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone ``repository_url`` on the API server and run gitleaks.

    Args:
        repository_url: Git URL from the flow git config or the caller.
        ref: Optional branch, tag, or commit.

    Returns:
        Wrapper result (findings or a scanner-not-installed error).
    """
    try:
        with checkout_repository(repository_url, ref=ref) as repo:
            return run_gitleaks(repo)
    except CheckoutError as exc:
        return {
            "error": "checkout_failed",
            "scanner": "gitleaks",
            "message": str(exc),
        }
