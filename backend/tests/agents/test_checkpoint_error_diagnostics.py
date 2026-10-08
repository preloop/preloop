"""An HTTP failure names its status, reason and operation, never the body (#1331)."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

from preloop.agents import checkpoint_client as cc

SECRET_URL = "https://preloop.example/api/v1/flows/executions/abc/artifacts"
SECRET_TOKEN = "scoped-capability-token"


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(SECRET_URL, code, "Some Reason", {}, io.BytesIO(body))


def arm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    error: urllib.error.HTTPError,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.json").write_text("{}")
    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cc, "EVIDENCE_REFERENCE_PATH", tmp_path / "ref.json")
    for name in ("PUT", "GET"):
        monkeypatch.setenv(f"PRELOOP_CHECKPOINT_{name}_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("PRELOOP_EVIDENCE_PUT_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", SECRET_URL)
    monkeypatch.setenv("PRELOOP_EVIDENCE_URL", SECRET_URL)
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(64 * 1024 * 1024))
    monkeypatch.setenv("PRELOOP_EVIDENCE_MAX_BYTES", str(32 * 1024 * 1024))

    def failing_request(*args: object, **kwargs: object) -> bytes:
        raise error

    monkeypatch.setattr(cc, "request", failing_request)
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", operation])


def run(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    with pytest.raises(SystemExit) as exc:
        cc.main()
    return exc.value.code, capsys.readouterr().out.strip()


MARKERS = {
    "capture": "PRELOOP_CHECKPOINT failed HTTPError",
    "restore": "PRELOOP_CHECKPOINT failed HTTPError",
    "evidence": "PRELOOP_EVIDENCE failed HTTPError",
}


@pytest.mark.parametrize("operation", ["capture", "restore", "evidence"])
@pytest.mark.parametrize("status", [401, 403, 404, 409, 413, 422])
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            json.dumps({"detail": "artifact_execution_closed"}),
            "artifact_execution_closed",
        ),
        (
            json.dumps({"error": "invalid_artifact_capability"}),
            "invalid_artifact_capability",
        ),
        # What FastAPI actually sends for the capability refusal.
        (
            json.dumps(
                {"detail": {"error": "invalid_artifact_capability", "message": "x y"}}
            ),
            "invalid_artifact_capability",
        ),
    ],
)
def test_http_error_marker_names_status_reason_and_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    status: int,
    body: str,
    reason: str,
) -> None:
    arm(monkeypatch, tmp_path, operation, http_error(status, body.encode()))
    code, out = run(capsys)
    assert code == 1
    assert out == (
        f"{MARKERS[operation]} status={status} detail={reason} op={operation}"
    )
    assert SECRET_URL not in out and SECRET_TOKEN not in out
    assert "prepublication" not in out


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        # nginx in front of the API answers with HTML, not the API's JSON.
        (
            b"<html><body><h1>413 Request Entity Too Large</h1></body></html>",
            "not_json",
        ),
        (b"", "empty"),
        (
            json.dumps({"detail": "Free text with /secret/path"}).encode(),
            "unrecognized",
        ),
        (json.dumps({"detail": "x" * 65}).encode(), "unrecognized"),
        (json.dumps(["artifact_oversized"]).encode(), "unrecognized"),
        # Larger than the bounded read: never parsed, never echoed.
        (
            b'{"detail": "artifact_oversized", "pad": "' + b"a" * 8192 + b'"}',
            "not_json",
        ),
    ],
)
def test_unexpected_bodies_reduce_to_a_fixed_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: bytes,
    reason: str,
) -> None:
    arm(monkeypatch, tmp_path, "capture", http_error(413, body))
    code, out = run(capsys)
    assert code == 1
    assert (
        out
        == f"PRELOOP_CHECKPOINT failed HTTPError status=413 detail={reason} op=capture"
    )
    assert "secret" not in out and "html" not in out.lower()


def test_body_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reads: list[int] = []

    class Tracking(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            reads.append(-1 if size is None else size)
            return super().read(size)

    error = urllib.error.HTTPError(
        SECRET_URL, 502, "Bad Gateway", {}, Tracking(b"x" * 10**6)
    )
    arm(monkeypatch, tmp_path, "capture", error)
    code, out = run(capsys)
    assert code == 1
    assert reads and all(0 < size <= cc.HTTP_ERROR_BODY_LIMIT + 1 for size in reads)
    assert out.endswith("status=502 detail=not_json op=capture")


def test_http_error_without_body_still_reports_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = urllib.error.HTTPError(SECRET_URL, 500, "Internal", {}, None)
    arm(monkeypatch, tmp_path, "restore", error)
    code, out = run(capsys)
    assert code == 1
    assert (
        out == "PRELOOP_CHECKPOINT failed HTTPError status=500 detail=empty op=restore"
    )


def test_non_http_evidence_errors_still_print_only_the_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arm(monkeypatch, tmp_path, "evidence", http_error(500, b""))

    def noisy(*args: object, **kwargs: object) -> bytes:
        raise OSError("connection refused to " + SECRET_URL)

    monkeypatch.setattr(cc, "request", noisy)
    code, out = run(capsys)
    assert code == 1
    assert out == "PRELOOP_EVIDENCE failed OSError"


def test_a_body_that_cannot_be_read_reports_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("connection reset by " + SECRET_URL)

    error = urllib.error.HTTPError(SECRET_URL, 502, "Bad Gateway", {}, Broken())
    arm(monkeypatch, tmp_path, "capture", error)
    code, out = run(capsys)
    assert code == 1
    assert out == (
        "PRELOOP_CHECKPOINT failed HTTPError status=502 detail=unreadable op=capture"
    )


@pytest.mark.parametrize("operation", ["capture", "evidence"])
def test_quota_refusal_marker_appends_byte_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    """The stable code stays; the numbers explain it in the runner log (#1339)."""
    body = json.dumps(
        {
            "detail": {
                "error": "artifact_quota_exceeded",
                "retained_bytes": 4250000000,
                "quota_bytes": 4294967296,
                "incoming_bytes": 41000000,
            }
        }
    ).encode()
    arm(monkeypatch, tmp_path, operation, http_error(422, body))
    code, out = run(capsys)
    assert code == 1
    assert out == (
        f"{MARKERS[operation]} status=422 detail=artifact_quota_exceeded"
        f" op={operation} retained=4250000000 quota=4294967296 incoming=41000000"
    )


@pytest.mark.parametrize(
    "detail",
    [
        "artifact_quota_exceeded",
        {"error": "artifact_quota_exceeded"},
        {"error": "artifact_quota_exceeded", "retained_bytes": 1, "quota_bytes": 2},
        {
            "error": "artifact_quota_exceeded",
            "retained_bytes": "1; rm -rf",
            "quota_bytes": 2,
            "incoming_bytes": 3,
        },
    ],
)
def test_quota_marker_without_all_numbers_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detail: object,
) -> None:
    arm(
        monkeypatch,
        tmp_path,
        "capture",
        http_error(422, json.dumps({"detail": detail}).encode()),
    )
    code, out = run(capsys)
    assert code == 1
    assert out == (
        "PRELOOP_CHECKPOINT failed HTTPError status=422"
        " detail=artifact_quota_exceeded op=capture"
    )
