"""Test the bounded selection-artifact reader with synthetic archives only."""

import io
import json
from pathlib import Path
import sys
from typing import Any
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ci_control  # noqa: E402


class FakeApi:
    """Serve a fixed artifact list."""

    repository = "example/repo"

    def __init__(self, artifacts: list[dict[str, Any]]) -> None:
        self.artifacts = artifacts

    def pages(self, path: str, key: str = "") -> list[dict[str, Any]]:
        """Return the synthetic artifact rows."""
        return self.artifacts


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


class FakeProc:
    """Stand in for a streamed `gh api` download."""

    def __init__(self, payload: bytes, code: int) -> None:
        self.stdout = io.BytesIO(payload)
        self.code = code
        self.killed = False

    def __enter__(self) -> "FakeProc":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def kill(self) -> None:
        """Record that the oversized download was stopped."""
        self.killed = True

    def wait(self, timeout: float) -> int:
        """Return the synthetic exit code."""
        return self.code


def _serve(monkeypatch: pytest.MonkeyPatch, payload: bytes, code: int = 0) -> None:
    monkeypatch.setattr(
        ci_control.subprocess, "Popen", lambda *a, **k: FakeProc(payload, code)
    )


ONE = [{"id": 7, "name": "ci-selection", "expired": False}]


def test_valid_receipt_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed receipt is returned as a dict."""
    _serve(monkeypatch, _zip({"ci-selection.json": json.dumps({"full": True})}))
    assert ci_control.read_selection(FakeApi(ONE), 1) == {"full": True}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [{"id": 7, "name": "ci-selection", "expired": True}],
        ONE + [{"id": 8, "name": "ci-selection", "expired": False}],
    ],
)
def test_missing_expired_or_ambiguous_artifacts_fail_closed(
    artifacts: list[dict[str, Any]],
) -> None:
    """No unique live artifact means no evidence."""
    assert ci_control.read_selection(FakeApi(artifacts), 1) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not a zip", 0),
        (_zip({"other.json": b"{}"}), 0),
        (_zip({"ci-selection.json": b"{broken"}), 0),
        (_zip({"ci-selection.json": b"[1, 2]"}), 0),
        (_zip({"ci-selection.json": b" " * (2 * 1024 * 1024 + 10)}), 0),
        (b"x" * (4 * 1024 * 1024 + 1), 0),
        (b"", 1),
    ],
)
def test_malformed_or_oversized_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, code: int
) -> None:
    """Bad archives never raise into the workflow and never trigger a rerun."""
    _serve(monkeypatch, payload, code)
    assert ci_control.read_selection(FakeApi(ONE), 1) is None  # type: ignore[arg-type]
