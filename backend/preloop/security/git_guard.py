"""Allow-listed git invocations for scanner helpers.

Forbidden operations are enforced here, not in the agent prompt. The tool
process must never run commands that dump blob contents into a transcript.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Sequence

FORBIDDEN_GIT_FLAGS = frozenset(
    {
        "-p",
        "--patch",
        "-u",
        "--unified",
        "--word-diff",
        "--color-words",
    }
)
FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"show", "diff", "format-patch"})
# cat-file can dump blobs; only -t / -s / --batch-check are allowed.
ALLOWED_CAT_FILE_FLAGS = frozenset({"-t", "-s", "--batch-check"})


class ForbiddenGitError(ValueError):
    """Raised when a caller asks for a git command that can leak blob bytes."""


def validate_git_argv(argv: Sequence[str]) -> None:
    """Reject git argv that can print historical blob contents.

    Args:
        argv: Full argument vector including the ``git`` executable.

    Raises:
        ForbiddenGitError: If the command is not allow-listed.
    """
    if not argv:
        raise ForbiddenGitError("empty git command")
    if os.path.basename(argv[0]) != "git":
        raise ForbiddenGitError(f"not a git command: {argv[0]}")
    if len(argv) < 2:
        raise ForbiddenGitError("git invoked with no subcommand")

    # Skip global options that precede the subcommand.
    idx = 1
    while idx < len(argv) and argv[idx].startswith("-"):
        if argv[idx] in {"-C", "-c"}:
            idx += 2
            continue
        idx += 1
    if idx >= len(argv):
        raise ForbiddenGitError("git invoked with no subcommand")

    sub = argv[idx]
    rest = list(argv[idx + 1 :])

    if sub in FORBIDDEN_GIT_SUBCOMMANDS:
        raise ForbiddenGitError(
            f"forbidden git subcommand {sub!r}: blob contents must not be dumped"
        )

    if sub == "cat-file":
        flags = {a for a in rest if a.startswith("-")}
        if not flags.issubset(ALLOWED_CAT_FILE_FLAGS):
            raise ForbiddenGitError(
                "git cat-file may only use -t, -s, or --batch-check"
            )
        return

    if sub == "log":
        for arg in rest:
            flag = arg.split("=", 1)[0]
            if flag in FORBIDDEN_GIT_FLAGS or arg in FORBIDDEN_GIT_FLAGS:
                raise ForbiddenGitError("git log -p / patch output is forbidden")
            if arg.startswith("-L"):
                raise ForbiddenGitError(
                    "git log -L dumps line contents at every revision; forbidden"
                )
            if flag == "--output":
                raise ForbiddenGitError(
                    "git log --output redirects output to a file; forbidden"
                )
        return

    # Allow a small set of metadata-only commands used by the scanners.
    allowed = {
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "remote",
        "tag",
        "branch",
        "status",
        "config",
        "cat-file",
        "log",
        "diff-tree",
        "describe",
        "symbolic-ref",
    }
    if sub not in allowed:
        raise ForbiddenGitError(f"git subcommand {sub!r} is not allow-listed")

    if sub == "diff-tree":
        # Name-only / metadata is fine; patch is not.
        if any(a in FORBIDDEN_GIT_FLAGS or a == "-p" for a in rest):
            raise ForbiddenGitError("git diff-tree patch output is forbidden")


def run_git(
    repo: Path,
    args: Sequence[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run an allow-listed git command in ``repo``.

    Args:
        repo: Repository root (must contain ``.git``).
        args: Git subcommand and flags, without the ``git`` executable.
        timeout: Seconds before the process is killed.

    Returns:
        Completed process with text stdout/stderr.

    Raises:
        ForbiddenGitError: If ``args`` is not allow-listed.
        FileNotFoundError: If ``repo`` is not a git work tree.
    """
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        raise FileNotFoundError(f"not a git repository: {repo_path}")
    argv: List[str] = ["git", "-C", str(repo_path), *args]
    validate_git_argv(argv)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
