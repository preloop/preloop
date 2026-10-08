"""The capture marker says when the server reused an identical snapshot (#1339)."""

import json
import sys
from pathlib import Path

import pytest

from preloop.agents import checkpoint_client as cc


@pytest.mark.parametrize(
    ("reference", "marker"),
    [
        (
            {"artifact_id": "a1", "deduplicated": True},
            "PRELOOP_CHECKPOINT committed a1 deduplicated",
        ),
        (
            {"artifact_id": "a1", "deduplicated": False},
            "PRELOOP_CHECKPOINT committed a1",
        ),
        ({"artifact_id": "a1"}, "PRELOOP_CHECKPOINT committed a1"),
        (
            {"artifact_id": "a1", "deduplicated": "yes"},
            "PRELOOP_CHECKPOINT committed a1",
        ),
    ],
)
def test_capture_marker_reports_deduplication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reference: dict,
    marker: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("x")
    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_PUT_TOKEN", "token")
    monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", "https://preloop.example/x")
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setattr(
        cc, "request", lambda *args, **kwargs: json.dumps(reference).encode()
    )
    written: list[str] = []
    monkeypatch.setattr(Path, "write_text", lambda self, text: written.append(text))
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "capture"])
    cc.main()
    assert capsys.readouterr().out.strip() == marker
    assert json.loads(written[-1]) == reference
