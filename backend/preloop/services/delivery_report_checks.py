"""Deterministic checks of the Delivery Report run: fetch scope and parent commit.

Two rules of the first measured run (#2029) used to live only in the
preset prompt. They are code here:

* **Fetch scope.** :func:`fetch_scope` decides, per pull request, what the
  run reads: always the file list, the added test names, the last reviewer
  summary and the check conclusion; review threads only when the PR closes
  an issue that states criteria or carries a severity marker; diff hunks
  only for criteria that name a behaviour no added test name matches.
  :func:`bounded_pr_input` applies that scope to a ``get_pull_request``
  response, so an assessor never sees more than its item needs.
* **Parent-commit check.** Before a criterion such as "update X
  accordingly" counts as not met because X was not touched, the run checks
  whether X referenced the removed or changed thing at the parent commit
  (``<merge>^1``). :func:`parent_references` runs that check with ``git
  grep``; the platform's report pass reads its records back (see
  :func:`preloop.services.delivery_report.apply_parent_checks`).

This module uses the standard library only. The orchestrator seeds its
source into the run's workspace as :data:`WORKSPACE_PATH`, and the run
calls it as a script (``python3 tools/delivery_report_checks.py scope
pr.json`` or ``... parent-check``), so the platform and the run apply the
same code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

#: Where the run finds this script in its workspace.
WORKSPACE_PATH = "tools/delivery_report_checks.py"
#: Schema id of a parent-check record, so the report pass can tell it apart.
PARENT_CHECK_SCHEMA = "preloop.parent_check/v1"

SUMMARY_START = "<!-- PRELOOP_SUMMARY -->"
SUMMARY_END = "<!-- /PRELOOP_SUMMARY -->"

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.|\.spec\.|\.test\."
)
#: Added test names: pytest functions, Go tests, and JS ``it``/``test``.
_TEST_NAME_RES = (
    re.compile(r"^\+\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.M),
    re.compile(r"^\+\s*func\s+(Test\w+)\s*\(", re.M),
    re.compile(r"^\+\s*(?:it|test)(?:\.\w+)?\(\s*(['\"`])(.+?)\1", re.M),
)
#: A reviewer finding at a severity a reader must see, or a code-scanning
#: alert. Plain mentions of the CodeQL workflow are not markers.
_SEVERITY_RE = re.compile(
    r"\*\*(?:CRITICAL|HIGH)\b|\[(?:CRITICAL|HIGH)\]|\bseverity\s*:?\s*\**\s*"
    r"(?:critical|high)\b|\*\*\[?(?:High|Critical)\]?\*\*",
    re.IGNORECASE,
)
_CODE_SCANNING_AUTHORS = frozenset({"github-advanced-security", "codeql"})
_WORD_RE = re.compile(r"[a-z][a-z0-9]+")
_STOP_WORDS = frozenset(
    "a an and are as at be by can for from has have in is it its new no not of "
    "on or the to when with without that this they their then than only all any "
    "each one two should must still keep add adds use uses".split()
)
#: Changed lines kept per file when diff hunks are in scope.
MAX_HUNK_LINES = 200


def _author(item: Mapping[str, Any]) -> str:
    author = item.get("author")
    if isinstance(author, Mapping):
        author = author.get("login") or author.get("username")
    return str(author or "").strip().lower().removesuffix("[bot]")


def _files(pr: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    changes = pr.get("changes")
    files = changes.get("changed_files") if isinstance(changes, Mapping) else None
    return (
        [f for f in files if isinstance(f, Mapping)] if isinstance(files, list) else []
    )


def _name(f: Mapping[str, Any]) -> str:
    return str(f.get("filename") or f.get("new_path") or f.get("path") or "")


def _patch(f: Mapping[str, Any]) -> str:
    return str(f.get("patch") or f.get("diff") or "")


def added_test_names(pr: Mapping[str, Any]) -> List[str]:
    """Test names the PR adds, read from the patches of its test files.

    Python ``def test_*``, Go ``func Test*`` and JS ``it("...")`` or
    ``test("...")``. Order of first appearance, no duplicates.
    """
    names: List[str] = []
    for f in _files(pr):
        if not _TEST_PATH_RE.search(_name(f)):
            continue
        patch = _patch(f)
        for pattern in _TEST_NAME_RES:
            for match in pattern.finditer(patch):
                name = match.group(match.lastindex or 1).strip()
                if name and name not in names:
                    names.append(name)
    return names


def last_review_summary(pr: Mapping[str, Any]) -> Optional[str]:
    """The newest ``PRELOOP_SUMMARY`` block: the PR body's, else a comment's."""
    texts = [str(pr.get("description") or "")]
    comments = pr.get("comments")
    if isinstance(comments, list):
        texts.extend(
            str(c.get("body") or "")
            for c in reversed(comments)
            if isinstance(c, Mapping)
        )
    for text in texts:
        if SUMMARY_START in text:
            begin = text.rindex(SUMMARY_START) + len(SUMMARY_START)
            end = text.find(SUMMARY_END, begin)
            return text[begin : end if end != -1 else None].strip()
    return None


def has_severity_marker(pr: Mapping[str, Any]) -> bool:
    """Whether a code-scanning alert or a HIGH or CRITICAL finding is present."""
    comments = pr.get("comments")
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, Mapping):
            continue
        if _author(comment) in _CODE_SCANNING_AUTHORS:
            return True
        if _SEVERITY_RE.search(str(comment.get("body") or "")):
            return True
    summary = last_review_summary(pr) or ""
    return bool(re.search(r"\*\*\[?(?:High|Critical)\]?\*\*", summary))


def check_conclusion(pr: Mapping[str, Any]) -> str:
    """``success``, ``failure``, ``pending`` or ``unknown`` from the PR's checks.

    Reads ``check_conclusion`` when the response states one, else a list of
    check runs under ``checks``. Any failing run fails the PR; skipped and
    neutral runs do not count.
    """
    stated = pr.get("check_conclusion")
    if isinstance(stated, str) and stated.strip():
        return stated.strip().lower()
    checks = pr.get("checks")
    if not isinstance(checks, list) or not checks:
        return "unknown"
    states = []
    for run in checks:
        if not isinstance(run, Mapping):
            continue
        value = str(run.get("conclusion") or run.get("state") or "").lower()
        states.append(value or "pending")
    if any(
        s in ("failure", "failed", "error", "timed_out", "cancelled") for s in states
    ):
        return "failure"
    if any(s in ("pending", "in_progress", "queued", "running") for s in states):
        return "pending"
    return "success" if states else "unknown"


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS}


def criterion_has_matching_test(criterion: str, test_names: Sequence[str]) -> bool:
    """Whether an added test name shares two content words with the criterion.

    Test names split on ``_``, camel case and spaces. One shared word is too
    weak a match (most criteria and tests share "issue" or "page"), so two
    are required; a criterion of fewer than two content words needs one.
    """
    words = _words(criterion)
    if not words:
        return True
    needed = 2 if len(words) >= 2 else 1
    for name in test_names:
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ")
        if len(words & _words(spaced)) >= needed:
            return True
    return False


def fetch_scope(
    pr: Mapping[str, Any], *, criteria: Sequence[str] = ()
) -> Dict[str, Any]:
    """What the run reads for one PR.

    Args:
        pr: A ``get_pull_request`` response.
        criteria: The criteria of the issues this PR closes; empty when it
            closes none or they state none.

    Returns:
        ``{"review_threads": bool, "reason": str, "diff_hunks_for":
        [criterion, ...]}``. The file list, the added test names, the last
        reviewer summary and the check conclusion are always in scope.
    """
    test_names = added_test_names(pr)
    severity = has_severity_marker(pr)
    if criteria:
        reason = "closes an issue with criteria"
    elif severity:
        reason = "severity or code-scanning marker present"
    else:
        reason = "no linked criteria and no severity marker"
    return {
        "review_threads": bool(criteria) or severity,
        "reason": reason,
        "diff_hunks_for": [
            c for c in criteria if not criterion_has_matching_test(c, test_names)
        ],
    }


def _hunk_lines(patch: str) -> List[str]:
    lines = [
        ln
        for ln in patch.splitlines()
        if ln.startswith(("+", "-", "@@")) and not ln.startswith(("+++", "---"))
    ]
    return lines[:MAX_HUNK_LINES]


def bounded_pr_input(
    pr: Mapping[str, Any], *, criteria: Sequence[str] = ()
) -> Dict[str, Any]:
    """The per-item input an assessor receives for one PR, and nothing else.

    The full body, every comment and every patch stay out unless
    :func:`fetch_scope` puts them in scope. Source hunks (not test files)
    are kept only when a criterion names a behaviour with no matching test.
    """
    scope = fetch_scope(pr, criteria=criteria)
    files = _files(pr)
    listed = []
    for f in files:
        lines = sum(
            v
            for v in (f.get("additions"), f.get("deletions"))
            if isinstance(v, int) and not isinstance(v, bool)
        )
        listed.append({"path": _name(f), "lines": lines})
    item: Dict[str, Any] = {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "url": pr.get("url"),
        "author": pr.get("author"),
        "merged_at": pr.get("merged_at"),
        "head_sha": pr.get("head_sha"),
        "merge_sha": pr.get("merge_commit_sha"),
        "files": listed,
        "files_changed": (pr.get("changes") or {}).get("files_changed")
        if isinstance(pr.get("changes"), Mapping)
        else None,
        "test_names": added_test_names(pr),
        "last_review_summary": last_review_summary(pr),
        "check_conclusion": check_conclusion(pr),
        "scope": scope,
    }
    if scope["review_threads"]:
        comments = pr.get("comments")
        item["review_threads"] = [
            {
                "author": _author(c),
                "path": c.get("path"),
                "body": str(c.get("body") or "")[:2000],
            }
            for c in (comments if isinstance(comments, list) else [])
            if isinstance(c, Mapping)
        ]
    if scope["diff_hunks_for"]:
        item["diff_hunks"] = {
            _name(f): _hunk_lines(_patch(f))
            for f in files
            if _patch(f) and not _TEST_PATH_RE.search(_name(f))
        }
    return item


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _git(repo: str) -> Runner:
    def run(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    return run


def parent_references(
    repo: str,
    merge_sha: str,
    paths: Sequence[str],
    terms: Sequence[str],
    *,
    runner: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Whether each path referenced any term at the merge's first parent.

    The merges are true merge commits, so the parent is ``<merge>^1``, never
    a combined diff. A record with ``referenced: false`` for a path means
    "update that path accordingly" needed no change.

    Args:
        repo: Path of the cloned repository.
        merge_sha: The closing merge commit.
        paths: Files the criterion names.
        terms: The removed or changed names to look for (fixed strings).
        runner: Runs one git command; defaults to ``git -C repo``.

    Returns:
        ``{"schema", "merge_sha", "parent_sha", "terms", "paths": [{"path",
        "exists", "referenced", "hits"}], "error"}``. ``error`` is set and
        ``paths`` is empty when the parent cannot be resolved.
    """
    run = runner or _git(repo)
    record: Dict[str, Any] = {
        "schema": PARENT_CHECK_SCHEMA,
        "merge_sha": merge_sha,
        "parent_sha": None,
        "terms": [t for t in terms if t],
        "paths": [],
        "error": None,
    }
    if not record["terms"]:
        record["error"] = "no terms to look for"
        return record
    parent = run(["rev-parse", "--verify", f"{merge_sha}^1"])
    if parent.returncode != 0 or not parent.stdout.strip():
        record["error"] = f"cannot resolve {merge_sha}^1"
        return record
    parent_sha = parent.stdout.strip()
    record["parent_sha"] = parent_sha
    for path in paths:
        exists = run(["cat-file", "-e", f"{parent_sha}:{path}"]).returncode == 0
        hits: List[str] = []
        if exists:
            args = ["grep", "-n", "-F"]
            for term in record["terms"]:
                args += ["-e", term]
            found = run([*args, parent_sha, "--", path])
            # git grep exits 1 for no match; that is an answer, not an error.
            hits = [ln for ln in found.stdout.splitlines() if ln][:20]
        record["paths"].append(
            {"path": path, "exists": exists, "referenced": bool(hits), "hits": hits}
        )
    return record


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line for the run: ``scope`` and ``parent-check``."""
    parser = argparse.ArgumentParser(prog="delivery_report_checks")
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="bounded input for one PR")
    scope.add_argument("pr", help="get_pull_request response as a JSON file")
    scope.add_argument(
        "--criterion",
        action="append",
        default=[],
        help="a criterion of an issue the PR closes (repeatable)",
    )
    parent = sub.add_parser("parent-check", help="references at <merge>^1")
    parent.add_argument("--repo", required=True)
    parent.add_argument("--merge", required=True)
    parent.add_argument("--path", action="append", required=True)
    parent.add_argument("--term", action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == "scope":
        out = bounded_pr_input(_load(args.pr), criteria=args.criterion)
    else:
        out = parent_references(args.repo, args.merge, args.path, args.term)
    json.dump(out, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
