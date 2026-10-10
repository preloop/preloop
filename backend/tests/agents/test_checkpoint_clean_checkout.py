"""A clean cloned checkout stores metadata, not a workspace payload (#1407)."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from preloop.agents import checkpoint_client as cc
from preloop.agents.container import ContainerAgentExecutor
from preloop.services.checkpoint_runtime import checkpoint_shell


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def init_repo(path: Path, filename: str = "tracked.txt") -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "jane@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Jane Doe"],
        check=True,
    )
    (path / filename).write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", filename], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    # The cloned commit is already on the code host. A remote-tracking ref is
    # what proves that; without it every local commit looks unpushed.
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )
    return git(path, "rev-parse", "HEAD")


def members(body: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        return [item.name for item in archive.getmembers() if item.isfile()]


def checkpoint_document(body: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        source = archive.extractfile("workspace/.preloop-checkpoint.json")
        assert source is not None
        document = json.loads(source.read().decode())
    assert isinstance(document, dict)
    return document


def expect_heads(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    monkeypatch.setenv(cc.EXPECTED_HEADS_ENV, json.dumps(mapping))
    monkeypatch.delenv(cc.CLONED_HEADS_ENV, raising=False)
    monkeypatch.delenv(cc.SNAPSHOT_MODE_ENV, raising=False)


class TestCaptureCleanCheckout:
    def test_clean_checkout_is_metadata_only(self, tmp_path: Path, monkeypatch) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert members(body) == ["workspace/.preloop-checkpoint.json"]
        document = checkpoint_document(body)
        assert document["metadata_only"] is True
        assert document["file_state_sha256"]
        repo = document["repositories"][0]
        assert repo["path"] == "."
        assert repo["branch"] == "main"
        assert repo["head_sha"] == sha
        assert "base_sha" in repo

    def test_modified_tracked_file_is_a_full_archive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        (tmp_path / "tracked.txt").write_text("local edit\n")

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/tracked.txt" in members(body)
        assert checkpoint_document(body).get("metadata_only") is not True

    def test_untracked_required_file_is_a_full_archive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        (tmp_path / "notes.txt").write_text("keep me\n")

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/notes.txt" in members(body)
        assert checkpoint_document(body).get("metadata_only") is not True

    def test_stash_is_a_full_archive(self, tmp_path: Path, monkeypatch) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        (tmp_path / "tracked.txt").write_text("stashed edit\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "stash", "push", "-m", "wip"],
            check=True,
            capture_output=True,
        )

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/.git/refs/stash" in members(body)
        assert checkpoint_document(body).get("metadata_only") is not True

    def test_unpushed_branch_is_a_full_archive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "local-work"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "tracked.txt").write_text("unpushed\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "local only"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True,
            capture_output=True,
        )

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/.git/refs/heads/local-work" in members(body)
        assert checkpoint_document(body).get("metadata_only") is not True

    def test_head_mismatch_is_a_full_archive(self, tmp_path: Path, monkeypatch) -> None:
        init_repo(tmp_path)
        expect_heads(monkeypatch, {".": "b" * 40})

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/tracked.txt" in members(body)
        assert checkpoint_document(body).get("metadata_only") is not True

    def test_excluded_untracked_files_stay_metadata_only(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        cache = tmp_path / "node_modules" / "pkg"
        cache.mkdir(parents=True)
        (cache / "index.js").write_text("ignored by exclusion\n")

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert members(body) == ["workspace/.preloop-checkpoint.json"]

    def test_always_keeps_a_full_archive_when_clean(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        monkeypatch.setenv(cc.SNAPSHOT_MODE_ENV, "always")

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert "workspace/tracked.txt" in members(body)

    def test_child_repository_uses_its_relative_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = tmp_path / "repo"
        sha = init_repo(repo)
        expect_heads(monkeypatch, {"repo": sha})

        body = cc.capture(tmp_path, max_bytes=2_000_000)

        assert members(body) == ["workspace/.preloop-checkpoint.json"]
        assert checkpoint_document(body)["repositories"][0]["path"] == "repo"


class TestCleanCheckoutMarker:
    def test_marker_and_barrier_for_a_clean_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        monkeypatch.setattr(cc, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setenv("PRELOOP_CHECKPOINT_PUT_TOKEN", "token")
        monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", "https://preloop.example/x")
        monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(2_000_000))
        uploaded: list[bytes] = []

        def put(method: str, token: str, body: bytes | None = None, **kwargs):
            assert method == "PUT"
            assert body is not None
            uploaded.append(body)
            return json.dumps({"artifact_id": "a1"}).encode()

        monkeypatch.setattr(cc, "request", put)
        monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
        cc.main()

        assert (
            capsys.readouterr().out.strip()
            == "PRELOOP_CHECKPOINT skipped clean_checkout"
        )
        assert members(uploaded[0]) == ["workspace/.preloop-checkpoint.json"]

    def test_dirty_checkout_still_commits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        (tmp_path / "tracked.txt").write_text("dirty\n")
        monkeypatch.setattr(cc, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setenv("PRELOOP_CHECKPOINT_PUT_TOKEN", "token")
        monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", "https://preloop.example/x")
        monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(2_000_000))
        monkeypatch.setattr(
            cc,
            "request",
            lambda *args, **kwargs: json.dumps({"artifact_id": "a1"}).encode(),
        )
        monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
        cc.main()
        assert capsys.readouterr().out.strip() == "PRELOOP_CHECKPOINT committed a1"


class TestMetadataOnlyRestore:
    def test_restore_logs_age_and_leaves_no_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sha = init_repo(tmp_path)
        expect_heads(monkeypatch, {".": sha})
        body = cc.capture(tmp_path, max_bytes=2_000_000)
        destination = tmp_path / "restored"
        monkeypatch.setattr(cc, "WORKSPACE_ROOT", destination)
        monkeypatch.setenv("PRELOOP_CHECKPOINT_GET_TOKEN", "token")
        monkeypatch.setattr(cc, "request", lambda *args, **kwargs: body)
        monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "restore"])

        cc.main()

        output = capsys.readouterr().out
        assert "PRELOOP_CHECKPOINT restored age_seconds=" in output
        assert "age_seconds=unknown" not in output
        assert "created_at=" in output
        assert not (destination / ".git").exists()
        assert (destination / ".preloop-checkpoint.json").is_file()
        restored = json.loads((destination / ".preloop-checkpoint.json").read_text())
        assert restored["metadata_only"] is True
        assert isinstance(restored["created_at"], (int, float))

    def test_record_cloned_head_is_workspace_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "workspace"
        repo = root / "repo"
        sha = init_repo(repo)
        heads = tmp_path / "heads.json"
        monkeypatch.setattr(cc, "WORKSPACE_ROOT", root)
        monkeypatch.setenv(cc.CLONED_HEADS_ENV, str(heads))

        cc.record_cloned_head(repo)

        assert json.loads(heads.read_text()) == {"repo": sha}

    def test_recorded_head_wins_over_the_trigger_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        sha = init_repo(repo)
        heads = tmp_path / "heads.json"
        heads.write_text(json.dumps({".": sha}))
        monkeypatch.setenv(cc.CLONED_HEADS_ENV, str(heads))
        monkeypatch.setenv(cc.EXPECTED_HEADS_ENV, json.dumps({".": "c" * 40}))

        body = cc.capture(repo, max_bytes=2_000_000)

        assert members(body) == ["workspace/.preloop-checkpoint.json"]


def test_metadata_only_restore_reclones(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sha = init_repo(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".preloop-checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "metadata_only": True,
                "repositories": [
                    {
                        "path": ".",
                        "branch": "main",
                        "head_sha": sha,
                        "base_sha": sha,
                    }
                ],
                "file_state_sha256": "a" * 64,
                "created_at": time.time(),
            }
        )
    )
    executor = ContainerAgentExecutor("codex", {}, image="test:latest")
    clone = (
        executor._build_git_pre_clone_shell(str(workspace))
        + "\n"
        + f"git clone {shlex.quote(str(source))} {shlex.quote(str(workspace))}"
    )
    script = executor._wrap_clone_with_workspace_restore(
        clone,
        {
            "checkpoint_env": {"PRELOOP_CHECKPOINT_GET_TOKEN": "test-capability"},
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": str(source),
                        "clone_path": str(workspace),
                    }
                ],
            },
        },
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "repository_missing" not in result.stdout
    assert git(workspace, "rev-parse", "HEAD") == sha


def test_missing_repository_without_metadata_still_fails(tmp_path: Path) -> None:
    executor = ContainerAgentExecutor("codex", {}, image="test:latest")
    absent = tmp_path / "absent"
    script = executor._wrap_clone_with_workspace_restore(
        "echo COLD_CLONE",
        {
            "checkpoint_env": {"PRELOOP_CHECKPOINT_GET_TOKEN": "test-capability"},
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "clone_path": str(absent),
                        "repository_url": "https://example.com/team/repo",
                    }
                ],
            },
        },
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert "repository_missing" in result.stdout
    assert "COLD_CLONE" not in result.stdout


def test_never_does_not_arm_capture() -> None:
    shell = checkpoint_shell(
        {"checkpoint_env": {"PRELOOP_WORKSPACE_SNAPSHOTS": "never"}}
    )
    assert "PRELOOP_CHECKPOINT skipped workspace_snapshots_never" in shell
    assert "checkpoint-client.py capture" not in shell
    assert "checkpoint-client.py evidence" in shell


def test_when_dirty_still_arms_capture() -> None:
    shell = checkpoint_shell(
        {"checkpoint_env": {"PRELOOP_WORKSPACE_SNAPSHOTS": "when_dirty"}}
    )
    assert "checkpoint-client.py capture" in shell
    assert "_preloop_start_checkpoint_loop" in shell
