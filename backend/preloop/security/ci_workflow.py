"""Generic CI workflow audit: mutable tags, pull_request_target, permissions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from preloop.security.git_guard import run_git

USES_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)['\"]?",
    re.IGNORECASE,
)
PERMISSIONS_WRITE_ALL_RE = re.compile(
    r"^\s*permissions:\s*write-all\b",
    re.IGNORECASE,
)
PULL_REQUEST_TARGET_RE = re.compile(
    r"^\s*(?:-\s*)?pull_request_target\b",
    re.IGNORECASE,
)
PERMISSIONS_KEY_RE = re.compile(r"^\s*permissions\s*:")
PINNED_SHA_RE = re.compile(r"@[0-9a-fA-F]{40}$")
MUTABLE_REF_RE = re.compile(
    r"@(?:v\d+(?:\.\d+)*|main|master|latest|head|dev|develop)$",
    re.IGNORECASE,
)

WORKFLOW_DIR_HINTS = (
    ".github/workflows",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    ".circleci/config.yml",
)


def _workflow_paths(repo: Path) -> List[str]:
    proc = run_git(repo, ["ls-files"])
    paths: List[str] = []
    for path in proc.stdout.splitlines():
        lowered = path.replace("\\", "/").lower()
        if "/.github/workflows/" in f"/{lowered}" or lowered.startswith(
            ".github/workflows/"
        ):
            if lowered.endswith((".yml", ".yaml")):
                paths.append(path)
            continue
        if lowered in {".gitlab-ci.yml", ".gitlab-ci.yaml"}:
            paths.append(path)
        if lowered in {"azure-pipelines.yml", "azure-pipelines.yaml"}:
            paths.append(path)
        if lowered == ".circleci/config.yml":
            paths.append(path)
    return paths


def _mutable_uses(value: str) -> bool:
    if PINNED_SHA_RE.search(value):
        return False
    if "@" not in value:
        return True
    return bool(MUTABLE_REF_RE.search(value)) or not PINNED_SHA_RE.search(value)


def ci_workflow_audit(repo_path: str) -> Dict[str, Any]:
    """Flag generic CI workflow hazards without product-specific names.

    Checks tracked workflow YAML for mutable-tag ``uses:``,
    ``pull_request_target``, and over-broad ``permissions: write-all`` /
    missing top-level permissions.

    Args:
        repo_path: Path to a git work tree.

    Returns:
        JSON-serializable dict of classifiable rows.
    """
    repo = Path(repo_path)
    head = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    rows: List[Dict[str, Any]] = []
    paths = _workflow_paths(repo)

    for rel in paths:
        abs_path = repo / rel
        try:
            text = abs_path.read_text(errors="replace")
        except OSError:
            continue
        has_permissions = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            uses = USES_RE.match(line)
            if uses:
                action = uses.group(1)
                if _mutable_uses(action):
                    rows.append(
                        {
                            "sha": head,
                            "path": f"{rel}:{lineno}",
                            "subject": "HEAD",
                            "term": action,
                            "kind": "mutable_uses_tag",
                            "status": "finding",
                        }
                    )
            if PULL_REQUEST_TARGET_RE.search(line):
                rows.append(
                    {
                        "sha": head,
                        "path": f"{rel}:{lineno}",
                        "subject": "HEAD",
                        "term": "pull_request_target",
                        "kind": "pull_request_target",
                        "status": "finding",
                    }
                )
            if PERMISSIONS_WRITE_ALL_RE.search(line):
                rows.append(
                    {
                        "sha": head,
                        "path": f"{rel}:{lineno}",
                        "subject": "HEAD",
                        "term": "write-all",
                        "kind": "overbroad_permissions",
                        "status": "finding",
                    }
                )
            if PERMISSIONS_KEY_RE.search(line):
                has_permissions = True
        if (
            rel.replace("\\", "/").startswith(".github/workflows/")
            and not has_permissions
        ):
            rows.append(
                {
                    "sha": head,
                    "path": rel,
                    "subject": "HEAD",
                    "term": "missing_permissions",
                    "kind": "missing_permissions",
                    "status": "finding",
                }
            )

    return {
        "tool": "ci_workflow_audit",
        "rows": rows,
        "workflow_files": paths,
        "notes": [
            "default_status_is_finding",
            "generic_yaml_checks_not_zizmor",
            "pin_zizmor_in_the_runner_image_for_deeper_audit",
        ],
    }
