"""An oversized workspace checkpoint is a skip, not a failed execution."""

import os
import sys
from pathlib import Path

import pytest

from preloop.agents import checkpoint_client as cc


def test_oversized_checkpoint_skips_without_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "blob.bin").write_bytes(os.urandom(4096))
    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", "64")
    monkeypatch.setenv("PRELOOP_CHECKPOINT_PUT_TOKEN", "scoped")
    monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", "https://example.invalid/artifacts")

    def fail_request(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("oversized checkpoint must not be uploaded")

    monkeypatch.setattr(cc, "request", fail_request)
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
    cc.main()
    captured = capsys.readouterr()
    assert "PRELOOP_CHECKPOINT skipped checkpoint_oversized" in captured.out
    assert "PRELOOP_CHECKPOINT failed" not in captured.out
    assert "prepublication_failed" not in captured.out


def test_checkpoint_workspace_busy_still_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("")
    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", "67108864")
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
    with pytest.raises(SystemExit) as exc:
        cc.main()
    assert exc.value.code == 1
    assert (
        "PRELOOP_CHECKPOINT failed ValueError checkpoint_workspace_busy"
        in capsys.readouterr().out
    )


def test_unstructured_checkpoint_errors_omit_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", "67108864")

    def noisy_capture(root: Path, *, max_bytes: int) -> bytes:
        raise ValueError("HTTP Error 413: /tmp/preloop-secret")

    monkeypatch.setattr(cc, "capture", noisy_capture)
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
    with pytest.raises(SystemExit) as exc:
        cc.main()
    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == "PRELOOP_CHECKPOINT failed ValueError"
