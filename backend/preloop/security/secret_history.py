"""Git-history secret scan that emits classifiable rows, never values."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from preloop.security.defaults import (
    DEFAULT_GREP_TERMS,
    DEFAULT_SECRET_TERMS,
    SENSITIVE_FILENAME_NAMES,
    SENSITIVE_FILENAME_SUBSTRINGS,
    SENSITIVE_FILENAME_SUFFIXES,
)
from preloop.security.git_guard import run_git
from preloop.security.opt_in import RECOMMENDED_GITLEAKS_VERSION

FINDING_STATUS = "finding"
KIND_PICKAXE = "pickaxe"
KIND_GREP = "grep"
KIND_DELETED_FILENAME = "deleted_filename"
KIND_BLOB_INVENTORY = "blob_inventory"


def _normalize_terms(extra_terms: Optional[Iterable[str]]) -> List[str]:
    terms: List[str] = []
    seen: Set[str] = set()
    for term in list(DEFAULT_SECRET_TERMS) + list(extra_terms or []):
        cleaned = str(term).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)
    return terms


def _normalize_grep_terms(extra_terms: Optional[Iterable[str]]) -> List[str]:
    terms: List[str] = []
    seen: Set[str] = set()
    for term in list(DEFAULT_GREP_TERMS) + list(extra_terms or []):
        cleaned = str(term).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)
    return terms


def _parse_log_sha_subject(text: str) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        if len(sha) < 7 or any(c not in "0123456789abcdefABCDEF" for c in sha):
            continue
        subject = parts[1] if len(parts) > 1 else ""
        rows.append((sha, subject))
    return rows


def _paths_for_commit(repo: Path, sha: str) -> List[str]:
    proc = run_git(
        repo,
        ["log", "-1", "--name-only", "--pretty=format:", sha],
    )
    paths = [p.strip() for p in proc.stdout.splitlines() if p.strip()]
    return paths


def _is_sensitive_filename(path: str) -> bool:
    name = Path(path).name
    lowered = name.lower()
    if lowered in SENSITIVE_FILENAME_NAMES:
        return True
    for suffix in SENSITIVE_FILENAME_SUFFIXES:
        if lowered.endswith(suffix):
            return True
    for fragment in SENSITIVE_FILENAME_SUBSTRINGS:
        if fragment in lowered:
            return True
    return False


def _blob_kind_for_path(path: str, size: int) -> str:
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if suffix in {".pem", ".crt", ".cer"} or name.endswith(".pem"):
        return "certificate"
    if suffix in {".key", ".p12", ".pfx"} or name in {"id_rsa", "id_ed25519"}:
        return "key_material"
    if suffix in {".bin", ".exe", ".dll", ".so", ".dylib"} or size >= 1024 * 1024:
        return "binary_ish"
    if suffix in SENSITIVE_FILENAME_SUFFIXES:
        return "sensitive_extension"
    return "tracked_blob"


def _run_gitleaks(repo: Path) -> Dict[str, Any]:
    """Invoke gitleaks if present. A count of 0 is never a secrets MET."""
    binary = shutil.which("gitleaks")
    if not binary:
        return {
            "available": False,
            "version": f"unavailable: not on PATH (pin {RECOMMENDED_GITLEAKS_VERSION} in the runner image)",
            "exit_code": None,
            "finding_count": None,
            "note": "gitleaks_zero_is_not_met",
        }
    try:
        version_proc = subprocess.run(
            [binary, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        version = (
            version_proc.stdout or version_proc.stderr or ""
        ).strip() or "unknown"
        detect = subprocess.run(
            [
                binary,
                "detect",
                "--no-banner",
                "--redact",
                "-s",
                str(repo),
                "-f",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        count = 0
        stdout = detect.stdout.strip()
        if stdout.startswith("["):
            import json

            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, list):
                    count = len(parsed)
            except json.JSONDecodeError:
                count = 0
        return {
            "available": True,
            "version": version,
            "recommended_version": RECOMMENDED_GITLEAKS_VERSION,
            "exit_code": detect.returncode,
            "finding_count": count,
            "note": "gitleaks_zero_is_not_met",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "version": f"unavailable: {exc}",
            "exit_code": None,
            "finding_count": None,
            "note": "gitleaks_zero_is_not_met",
        }


def secret_history_scan(
    repo_path: str,
    extra_terms: Optional[Sequence[str]] = None,
    all_refs: bool = True,
    include_blob_inventory: bool = True,
) -> Dict[str, Any]:
    """Walk git history for secret-like changes without emitting values.

    Every row is classifiable. Default ``status`` is ``finding``. The agent
    may later attach ``not_a_finding`` plus a reason; unclassified rows fail
    register validation.

    Args:
        repo_path: Path to a git work tree.
        extra_terms: Additional pickaxe / grep terms supplied by the caller.
        all_refs: When True (default), search ``--all`` refs.
        include_blob_inventory: When True, inventory HEAD blobs by extension
            and size (kind only, never bytes).

    Returns:
        JSON-serializable dict with ``rows``, ``gitleaks``, and notes.

    Raises:
        FileNotFoundError: If ``repo_path`` is not a git repository.
    """
    repo = Path(repo_path)
    ref_args = ["--all"] if all_refs else []
    rows: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str, str]] = set()

    def _add(
        sha: str,
        path: str,
        subject: str,
        term: str,
        kind: str,
    ) -> None:
        key = (sha, path, term, kind)
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "sha": sha,
                "path": path,
                "subject": subject,
                "term": term,
                "kind": kind,
                "status": FINDING_STATUS,
            }
        )

    for term in _normalize_terms(extra_terms):
        proc = run_git(
            repo,
            ["log", *ref_args, "--format=%H %s", "-S", term],
        )
        for sha, subject in _parse_log_sha_subject(proc.stdout):
            paths = _paths_for_commit(repo, sha) or [""]
            for path in paths:
                _add(sha, path, subject, term, KIND_PICKAXE)

    for term in _normalize_grep_terms(extra_terms):
        proc = run_git(
            repo,
            ["log", *ref_args, "--format=%H %s", "--grep", term],
        )
        for sha, subject in _parse_log_sha_subject(proc.stdout):
            paths = _paths_for_commit(repo, sha) or [""]
            for path in paths:
                _add(sha, path, subject, term, KIND_GREP)

    deleted = run_git(
        repo,
        ["log", *ref_args, "--diff-filter=D", "--name-only", "--pretty=format:%H %s"],
    )
    current_sha = ""
    current_subject = ""
    for line in deleted.stdout.splitlines():
        raw = line.rstrip("\n")
        if not raw:
            current_sha = ""
            current_subject = ""
            continue
        parsed = _parse_log_sha_subject(raw)
        if parsed:
            current_sha, current_subject = parsed[0]
            continue
        if current_sha and _is_sensitive_filename(raw):
            _add(
                current_sha,
                raw,
                current_subject,
                Path(raw).name,
                KIND_DELETED_FILENAME,
            )

    if include_blob_inventory:
        tree = run_git(repo, ["ls-tree", "-r", "-l", "HEAD"])
        for line in tree.stdout.splitlines():
            # <mode> <type> <sha> <size>\t<path>
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            parts = meta.split()
            if len(parts) < 4:
                continue
            blob_sha, size_s = parts[2], parts[3]
            try:
                size = int(size_s)
            except ValueError:
                size = 0
            sensitive = _is_sensitive_filename(path)
            if not sensitive and size < 1024 * 1024:
                continue
            kind = _blob_kind_for_path(path, size)
            head = run_git(repo, ["rev-parse", "HEAD"])
            sha = head.stdout.strip()
            _add(sha, path, "HEAD", kind, KIND_BLOB_INVENTORY)
            # blob_sha is metadata only; never fetch the object.
            _ = blob_sha

    return {
        "tool": "secret_history_scan",
        "rows": rows,
        "gitleaks": _run_gitleaks(repo),
        "notes": [
            "default_status_is_finding",
            "gitleaks_zero_is_not_met",
            "values_are_never_emitted",
            "unclassified_rows_fail_register_validation",
        ],
        "all_refs": all_refs,
    }
