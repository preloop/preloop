"""Real offline Git fixtures for the trusted reviewed-diff identity."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.flow_orchestrator import (
    AGENT_EXEC_START_MARKER,
    FlowExecutionOrchestrator,
)
from preloop.services.read_only_review import read_only_startup_line
from preloop.services.review_diff import (
    DIFF_MARKER,
    DIFF_OPTIONS,
    compute_diff,
    parse_diff_line,
    review_diff_identity,
)


def git(root: Path, *args: str) -> str:
    """Run offline Git in an isolated synthetic checkout."""
    return (
        subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Jane Doe",
                "-c",
                "user.email=jane@example.com",
                *args,
            ],
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    """Include binary, Unicode, deletion and rename-shaped changes."""
    git(tmp_path, "init", "-q")
    (tmp_path / "old.txt").write_text("old\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Initial fixture")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "old.txt").rename(tmp_path / "new.txt")
    (tmp_path / "binary.dat").write_bytes(b"\0\xff\x01")
    (tmp_path / "unicode-é.txt").write_text("new\r\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Change fixture")
    return tmp_path, base, git(tmp_path, "rev-parse", "HEAD")


def test_digest_matches_documented_git_bytes(
    repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, base, head = repository
    actual = compute_diff(str(root), base, head)["diff"]
    bare = root / "independent.git"
    git(root, "init", "--bare", "-q", str(bare))
    env = {
        **os.environ,
        "LC_ALL": "C",
        "GIT_OBJECT_DIRECTORY": str(root / ".git/objects"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    data = subprocess.check_output(
        [
            "git",
            "-c",
            "core.quotePath=true",
            "-c",
            "core.attributesFile=/dev/null",
            "--git-dir",
            str(bare),
            "diff",
            *DIFF_OPTIONS,
            base,
            head,
            "--",
        ],
        env=env,
    )
    assert actual["sha256"] == hashlib.sha256(data).hexdigest()
    assert actual["bytes"] == len(data)
    assert actual["files"] == ["binary.dat", "new.txt", "old.txt", "unicode-é.txt"]
    # Dirty attributes and local config cannot influence the bare view.
    (root / ".git/info/attributes").write_text("* -diff\n")
    git(root, "config", "diff.context", "99")
    assert compute_diff(str(root), base, head)["diff"] == actual


def test_missing_base_history_and_wrong_head_are_explicit(
    repository: tuple[Path, str, str],
) -> None:
    root, base, head = repository
    assert (
        compute_diff(str(root), None, head)["diff_unavailable_reason"] == "base_unknown"
    )
    assert (
        compute_diff(str(root), "f" * 40, head)["diff_unavailable_reason"]
        == "history_unavailable"
    )
    assert (
        compute_diff(str(root), base, base)["diff_unavailable_reason"]
        == "head_mismatch"
    )


def test_shallow_history_never_fetches_missing_objects(
    repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Missing history stays unknown even when the clone has a reachable remote."""
    root, base, head = repository
    shallow = tmp_path / "shallow"
    git(root, "clone", "--depth=1", root.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert compute_diff(str(shallow), base, head) == {
        "diff": None,
        "diff_unavailable_reason": "history_unavailable",
    }
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"


def test_completion_events_and_publication_function_share_the_digest(
    repository: tuple[Path, str, str],
) -> None:
    from preloop.models.crud.ci_subscription import _review_diff_digest
    from preloop.services.event_webhooks.emitters import flow_execution_finished_data
    from preloop.services.read_only_review import public_startup_receipt

    root, base, head = repository
    observed = compute_diff(str(root), base, head)
    execution = SimpleNamespace(
        ci_review_binding={"base_sha": base, "head_sha": head},
        startup_receipt={
            **observed,
            "commit": head,
            "checkout_verified": True,
            "setup_completed": True,
        },
    )
    expected = {"sha256": observed["diff"]["sha256"]}
    assert review_diff_identity(execution) == observed["diff"]
    assert _review_diff_digest(execution) == expected
    assert public_startup_receipt(execution)["diff_unavailable_reason"] is None
    assert (
        flow_execution_finished_data(
            execution, SimpleNamespace(), status="SUCCEEDED", failure_category=None
        )["diff"]
        == expected
    )
    execution.ci_review_binding["head_sha"] = "f" * 40
    assert review_diff_identity(execution) is None
    assert _review_diff_digest(execution) is None
    assert (
        public_startup_receipt(execution)["diff_unavailable_reason"]
        == "unverified_receipt"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_only_pre_agent_diff_is_accepted(
    repository: tuple[Path, str, str], late: bool
) -> None:
    root, base, head = repository
    value = compute_diff(str(root), base, head)
    orch = FlowExecutionOrchestrator(MagicMock(), uuid4(), {}, AsyncMock())
    orch.execution_log = SimpleNamespace(
        id=uuid4(), ci_review_binding={"base_sha": base, "head_sha": head}
    )
    orch._read_only_admitted_commit = head
    line = DIFF_MARKER + json.dumps(value)
    startup = [read_only_startup_line(head), AGENT_EXEC_START_MARKER]
    lines = [*startup, line] if late else [line, *startup]
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution.record_startup_receipt"
    ) as record:
        for index, text in enumerate(lines):
            await orch._process_agent_log_line(
                text,
                session_reference="session",
                previous_line="",
                log_count=index,
                publish_raw=False,
            )
        receipt = record.call_args.kwargs["receipt"]
        assert receipt["diff"] == (None if late else value["diff"])
        forged = {**value, "diff": {**value["diff"], "sha256": "f" * 64}}
        orch._note_read_only_startup_line(DIFF_MARKER + json.dumps(forged))
        assert record.call_count == 1
        assert orch._startup_receipt["diff"] == receipt["diff"]
    orch.execution_log.startup_receipt = receipt
    assert review_diff_identity(orch.execution_log) == (None if late else value["diff"])


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"diff": {}},
        {"diff": None},
        {"diff": {"base_sha": "b" * 40, "head_sha": "a" * 40}},
    ],
)
def test_invalid_startup_diff_is_not_trusted(value: Any) -> None:
    assert parse_diff_line(DIFF_MARKER + json.dumps(value), "b" * 40, "a" * 40) is None
