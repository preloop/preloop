"""Thin wrapper around the zizmor CLI (GitHub Actions audit).

This is not a CI-YAML parser. If the checkout has no
``.github/workflows``, the tool returns a structured not-applicable
result. Missing binary is an error, not an empty MET.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from preloop.security.checkout import CheckoutError, checkout_repository
from preloop.security.pins import RECOMMENDED_ZIZMOR_VERSION

logger = logging.getLogger(__name__)

ZIZMOR_TIMEOUT_SEC = 120
WORKFLOW_DIR = Path(".github") / "workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")


def scanner_not_installed() -> Dict[str, Any]:
    """Return a structured error when zizmor is not on PATH."""
    return {
        "error": "scanner_not_installed",
        "scanner": "zizmor",
        "recommended_version": RECOMMENDED_ZIZMOR_VERSION,
        "message": (
            "zizmor is not installed on the Preloop API server. "
            f"Pin zizmor (recommended {RECOMMENDED_ZIZMOR_VERSION})."
        ),
    }


def _workflow_files(repo: Path) -> List[str]:
    directory = repo / WORKFLOW_DIR
    if not directory.is_dir():
        return []
    names: List[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in WORKFLOW_SUFFIXES:
            names.append(str(path.relative_to(repo)))
    return names


def _finding_location(raw: Dict[str, Any]) -> Dict[str, Any]:
    file_name = ""
    line: Optional[int] = None
    locations = raw.get("locations") or raw.get("location") or []
    if isinstance(locations, dict):
        locations = [locations]
    if isinstance(locations, list) and locations:
        loc = locations[0] if isinstance(locations[0], dict) else {}
        symbolic = loc.get("symbolic") or {}
        concrete = loc.get("concrete") or {}
        file_name = (
            loc.get("file")
            or loc.get("path")
            or symbolic.get("file")
            or symbolic.get("path")
            or ""
        )
        span = concrete.get("line_span") or concrete.get("location") or {}
        if isinstance(span, dict):
            line = span.get("start") or span.get("line")
        elif isinstance(span, (list, tuple)) and span:
            line = span[0]
        if line is None:
            line = loc.get("line") or loc.get("start_line")
    try:
        line_n = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_n = None
    return {"file": str(file_name), "line": line_n}


def _redact_finding(raw: Dict[str, Any]) -> Dict[str, Any]:
    loc = _finding_location(raw)
    rule = (
        raw.get("ident")
        or raw.get("id")
        or raw.get("rule")
        or raw.get("determination")
        or ""
    )
    severity = ""
    determinations = raw.get("determinations") or {}
    if isinstance(determinations, dict):
        severity = str(determinations.get("severity") or "")
    return {
        "rule": str(rule),
        "file": loc["file"],
        "line": loc["line"],
        "severity": severity or None,
        "status": "finding",
    }


def _parse_zizmor_json(text: str) -> List[Dict[str, Any]]:
    if not (text or "").strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("findings") or parsed.get("results") or []
    if not isinstance(parsed, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            rows.append(_redact_finding(item))
    return rows


def run_zizmor(repo: Path) -> Dict[str, Any]:
    """Run zizmor on an already-cloned work tree.

    Args:
        repo: Path to a git work tree on the API server.

    Returns:
        JSON-serializable dict. ``applicable`` is False when the repo
        has no GitHub Actions workflows.
    """
    workflows = _workflow_files(repo)
    if not workflows:
        return {
            "tool": "zizmor_scan",
            "scanner": "zizmor",
            "applicable": False,
            "finding_count": 0,
            "findings": [],
            "note": "no_github_workflows",
        }

    binary = shutil.which("zizmor")
    if not binary:
        return scanner_not_installed()

    argv = [binary, "--format", "json", str(repo)]
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=ZIZMOR_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "error": "scanner_failed",
            "scanner": "zizmor",
            "recommended_version": RECOMMENDED_ZIZMOR_VERSION,
            "message": f"zizmor failed: {exc}",
        }

    stdout = (proc.stdout or "").strip()
    findings = _parse_zizmor_json(stdout)
    payload: Dict[str, Any] = {
        "tool": "zizmor_scan",
        "scanner": "zizmor",
        "applicable": True,
        "workflow_files": workflows,
        "recommended_version": RECOMMENDED_ZIZMOR_VERSION,
        "exit_code": proc.returncode,
        "finding_count": len(findings),
        "findings": findings,
    }
    if proc.returncode not in (0, 1) and not findings:
        payload["error"] = "scanner_failed"
        payload["message"] = (proc.stderr or proc.stdout or "zizmor failed").strip()
    return payload


def zizmor_scan(
    repository_url: str,
    ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone ``repository_url`` on the API server and run zizmor.

    Args:
        repository_url: Git URL from the flow git config or the caller.
        ref: Optional branch, tag, or commit.

    Returns:
        Wrapper result (findings, not-applicable, or not-installed).
    """
    try:
        with checkout_repository(repository_url, ref=ref) as repo:
            return run_zizmor(repo)
    except CheckoutError as exc:
        return {
            "error": "checkout_failed",
            "scanner": "zizmor",
            "message": str(exc),
        }
