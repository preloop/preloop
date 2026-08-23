"""Generic fork-vs-upstream divergence. Caller supplies the upstream URL."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from preloop.security.git_guard import run_git


def upstream_divergence(
    repo_path: str,
    upstream_url: str,
    pin: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare a local pin to tags/releases advertised by an upstream remote.

    Does not hardcode any product or vendor. The caller must supply the
    upstream remote URL.

    Args:
        repo_path: Path to a git work tree.
        upstream_url: Git remote URL of the upstream project.
        pin: Commit SHA or ref to compare. Defaults to HEAD.

    Returns:
        JSON-serializable comparison: pin, matching tags, and whether the
        pin is an upstream tag / reachable from listed tags.

    Raises:
        ValueError: If ``upstream_url`` is empty.
    """
    if not (upstream_url or "").strip():
        raise ValueError("upstream_url is required")
    repo = Path(repo_path)
    url = upstream_url.strip()
    if pin:
        resolved = run_git(repo, ["rev-parse", pin]).stdout.strip()
    else:
        resolved = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()

    remote = run_git(repo, ["ls-remote", "--tags", "--refs", url], timeout=90)
    tags: List[Dict[str, str]] = []
    matching: List[str] = []
    if remote.returncode != 0:
        return {
            "tool": "upstream_divergence",
            "pin": resolved,
            "upstream_url": url,
            "error": (remote.stderr or "ls-remote failed").strip(),
            "tags": [],
            "matching_tags": [],
            "is_upstream_tag": False,
            "rows": [
                {
                    "sha": resolved,
                    "path": "",
                    "subject": "upstream_unreachable",
                    "term": url,
                    "kind": "upstream_unreachable",
                    "status": "finding",
                }
            ],
        }

    for line in remote.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/") :]
        tags.append({"name": name, "sha": sha})
        if sha == resolved:
            matching.append(name)

    is_tag = bool(matching)
    rows: List[Dict[str, Any]] = []
    if not is_tag:
        rows.append(
            {
                "sha": resolved,
                "path": "",
                "subject": "pin_is_not_an_upstream_tag",
                "term": url,
                "kind": "pin_not_upstream_tag",
                "status": "finding",
            }
        )

    return {
        "tool": "upstream_divergence",
        "pin": resolved,
        "upstream_url": url,
        "tag_count": len(tags),
        "matching_tags": matching,
        "is_upstream_tag": is_tag,
        "sample_tags": [t["name"] for t in tags[:20]],
        "rows": rows,
        "notes": [
            "caller_supplies_upstream_url",
            "default_status_is_finding",
        ],
    }
