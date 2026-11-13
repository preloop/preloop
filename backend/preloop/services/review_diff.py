"""Trusted, offline diff identity; this stdlib module also runs in the image."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DIFF_MARKER = "PRELOOP_REVIEW_DIFF "
SHA_PATTERN = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
DIFF_OPTIONS = [
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--binary",
    "--full-index",
    "--diff-algorithm=myers",
    "--no-indent-heuristic",
    "--unified=3",
    "--inter-hunk-context=0",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
    "--ignore-submodules=none",
]


def compute_diff(root: str, base: str | None, head: str) -> dict[str, Any]:
    """Hash canonical merge-base-to-head bytes without fetching any objects.

    Args:
        root: Reviewed repository checkout.
        base: Provider-admitted base, or None for an older binding.
        head: Provider-admitted head, already checked out by startup.

    Returns:
        A diff receipt, or a null diff and a stable unavailability reason.
    """
    if base is None:
        return {"diff": None, "diff_unavailable_reason": "base_unknown"}
    if not all(
        isinstance(value, str) and re.fullmatch(SHA_PATTERN, value)
        for value in (base, head)
    ):
        return {"diff": None, "diff_unavailable_reason": "invalid_revision"}
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        # Even older Git must never authenticate a partial-clone lazy fetch.
        "GIT_TERMINAL_PROMPT": "0",
    }
    git = [
        "git",
        "-c",
        "core.quotePath=true",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
    ]

    def run(*args: str) -> bytes:
        return subprocess.check_output(
            [*git, *args], cwd=root, env=env, stderr=subprocess.DEVNULL, timeout=60
        )

    try:
        if run("rev-parse", "HEAD").strip().decode("ascii") != head:
            return {"diff": None, "diff_unavailable_reason": "head_mismatch"}
        # A bare temporary view ignores mutable worktree/info attributes and
        # local diff drivers. Objects are read from the admitted checkout;
        # no remote is configured, so missing objects can never trigger fetch.
        objects = run("rev-parse", "--git-path", "objects").strip().decode()
        objects = str((Path(root) / objects).resolve())
        with tempfile.TemporaryDirectory(prefix="preloop-diff-") as temporary:
            subprocess.run(
                [
                    *git,
                    "init",
                    "--bare",
                    "--quiet",
                    "--object-format=" + ("sha256" if len(head) == 64 else "sha1"),
                    temporary,
                ],
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            diff_env = {**env, "GIT_OBJECT_DIRECTORY": objects}
            bases = (
                subprocess.check_output(
                    [*git, "--git-dir", temporary, "merge-base", "--all", base, head],
                    env=diff_env,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
                .decode("ascii")
                .split()
            )
            if len(bases) != 1:
                return {"diff": None, "diff_unavailable_reason": "ambiguous_merge_base"}
            merge_base = bases[0]
            command = [*git, "--git-dir", temporary, "diff"]
            paths = subprocess.check_output(
                [*command, "--no-renames", "--name-only", "-z", merge_base, head, "--"],
                env=diff_env,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            try:
                files = [p.decode("utf-8") for p in paths.split(b"\0") if p]
            except UnicodeError:
                return {
                    "diff": None,
                    "diff_unavailable_reason": "unsupported_path_encoding",
                }
            with tempfile.TemporaryFile() as output:
                subprocess.run(
                    [*command, *DIFF_OPTIONS, merge_base, head, "--"],
                    env=diff_env,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=60,
                )
                size = output.tell()
                output.seek(0)
                hasher = hashlib.sha256()
                while chunk := output.read(1024 * 1024):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
        receipt = {
            "diff": {
                "base_sha": base,
                "merge_base_sha": merge_base,
                "head_sha": head,
                "sha256": digest,
                "files": files,
                "bytes": size,
            },
            "diff_unavailable_reason": None,
        }
        if len(json.dumps(receipt)) > 65536:
            return {"diff": None, "diff_unavailable_reason": "receipt_too_large"}
        return receipt
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {"diff": None, "diff_unavailable_reason": "history_unavailable"}


def parse_diff_line(line: str, base: str | None, head: str) -> dict[str, Any] | None:
    """Allowlist a startup line and bind it to the admitted repository pair."""
    if not line.startswith(DIFF_MARKER) or len(line) > 65536 + len(DIFF_MARKER):
        return None
    try:
        value = json.loads(line[len(DIFF_MARKER) :])
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    diff = value.get("diff")
    reason = value.get("diff_unavailable_reason")
    if diff is None:
        if isinstance(reason, str) and reason in {
            "base_unknown",
            "invalid_revision",
            "head_mismatch",
            "ambiguous_merge_base",
            "receipt_too_large",
            "history_unavailable",
            "unsupported_path_encoding",
        }:
            return {"diff": None, "diff_unavailable_reason": reason}
        return None
    if (
        not isinstance(diff, dict)
        or diff.get("base_sha") != base
        or diff.get("head_sha") != head
    ):
        return None
    if not isinstance(base, str) or not re.fullmatch(SHA_PATTERN, base):
        return None
    if not isinstance(diff.get("merge_base_sha"), str) or not re.fullmatch(
        SHA_PATTERN, diff["merge_base_sha"]
    ):
        return None
    if not isinstance(diff.get("sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", diff["sha256"]
    ):
        return None
    if type(diff.get("bytes")) is not int or diff["bytes"] < 0:
        return None
    files = diff.get("files")
    if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
        return None
    return {
        "diff": {
            key: diff[key]
            for key in (
                "base_sha",
                "merge_base_sha",
                "head_sha",
                "sha256",
                "files",
                "bytes",
            )
        },
        "diff_unavailable_reason": None,
    }


def review_diff_identity(execution: Any) -> dict[str, Any] | None:
    """Return trusted diff identity for completion and publication consumers."""
    receipt = getattr(execution, "startup_receipt", None)
    binding = getattr(execution, "ci_review_binding", None)
    if not isinstance(receipt, dict) or not isinstance(binding, dict):
        return None
    if (
        receipt.get("checkout_verified") is not True
        or receipt.get("setup_completed") is not True
    ):
        return None
    value = parse_diff_line(
        DIFF_MARKER + json.dumps(receipt),
        binding.get("base_sha"),
        binding.get("head_sha"),
    )
    return value["diff"] if value else None
