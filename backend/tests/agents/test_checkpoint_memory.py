"""Checkpoint members and compressed output must fit a bounded memory budget."""

import hashlib
import io
import json
import os
import tarfile
import tracemalloc
from pathlib import Path

import pytest

from preloop.agents.checkpoint_client import _CheckpointBuffer, capture, restore
from preloop.agents import checkpoint_client as cc


def test_large_compressible_member_is_streamed(tmp_path: Path) -> None:
    workspace = tmp_path / "source"
    workspace.mkdir()
    payload = b"z" * 65536
    with (workspace / "generated.bin").open("wb") as file:
        for _ in range(256):
            file.write(payload)
    tracemalloc.start()
    try:
        body = capture(workspace, max_bytes=100_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A 16 MiB source file must not allocate a 16 MiB Python bytes object.
    assert peak < 4 * 1024**2
    metadata = restore(body, tmp_path / "restored")
    expected_hash = hashlib.sha256()
    for _ in range(256):
        expected_hash.update(payload)
    expected_state = hashlib.sha256(
        b"generated.bin\0" + expected_hash.digest()
    ).hexdigest()
    assert metadata["file_state_sha256"] == expected_state
    assert (tmp_path / "restored/generated.bin").stat().st_size == 16 * 1024**2


def test_output_cap_applies_inside_a_single_member(tmp_path: Path) -> None:
    (tmp_path / "large.bin").write_bytes(os.urandom(1024**2))
    with pytest.raises(ValueError, match="checkpoint_oversized"):
        capture(tmp_path, max_bytes=1024)


def test_buffer_rejects_write_before_allocating() -> None:
    buffer = _CheckpointBuffer(64)
    buffer.write(b"x" * 32)
    with pytest.raises(ValueError, match="checkpoint_oversized"):
        buffer.write(b"y" * 33)
    assert buffer.getvalue() == b"x" * 32


def test_file_truncated_while_streaming_defers_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "changing.bin").write_bytes(b"x" * 4096)
    original_read = cc._CheckpointReader.read

    def truncate_then_read(reader: cc._CheckpointReader, size: int) -> bytes:
        os.truncate(reader.source.name, 0)
        return original_read(reader, size)

    monkeypatch.setattr(cc._CheckpointReader, "read", truncate_then_read)
    with pytest.raises(ValueError, match="checkpoint_workspace_busy"):
        capture(tmp_path, max_bytes=100_000)


def test_nested_dependency_directories_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("print('hello')")
    for directory in ("backend/.venv", "frontend/node_modules", "backend/venv"):
        target = tmp_path / directory
        target.mkdir(parents=True)
        (target / "generated").write_bytes(os.urandom(4096))
    body = capture(tmp_path, max_bytes=4096)
    with tarfile.open(fileobj=io.BytesIO(body)) as archive:
        names = archive.getnames()
        assert names == ["workspace/source.py", "workspace/.preloop-checkpoint.json"]
        metadata = json.load(archive.extractfile(names[-1]))
        assert metadata["version"] == 1


def test_short_read_is_classified_without_a_tarfile_error_message() -> None:
    reader = cc._CheckpointReader(io.BytesIO(b"short"), expected_size=10)
    with pytest.raises(ValueError, match="checkpoint_workspace_busy"):
        reader.read(10)
