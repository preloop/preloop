"""Synthetic operator workflow verification; no external Actions activation."""

import argparse
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import pytest

_path = Path(__file__).parents[2] / "scripts" / "restricted_ci_review.py"
_spec = importlib.util.spec_from_file_location("restricted_ci_review_example", _path)
assert _spec is not None and _spec.loader is not None
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)

FLOW = "00000000-0000-4000-8000-000000000001"
PROJECT = "00000000-0000-4000-8000-000000000002"
EXECUTION = "00000000-0000-4000-8000-000000000003"
OBSOLETE = "00000000-0000-4000-8000-000000000004"
HEAD = "a" * 40


def persisted() -> dict[str, Any]:
    return dict(
        id=EXECUTION,
        execution_id=EXECUTION,
        flow_id=FLOW,
        project_id=PROJECT,
        repository_identifier="17",
        provider_pr_id="23",
        pr_number=7,
        head_sha=HEAD,
        status="SUCCEEDED",
        result={"review": "Synthetic review"},
    )


def approved_pr() -> dict[str, Any]:
    repository = {"id": 17, "full_name": "example/repository"}
    return dict(
        id=23,
        state="open",
        labels=[{"name": "ci-approved"}],
        head={"sha": HEAD, "repo": repository},
        base={"repo": repository},
    )


@pytest.mark.parametrize(
    "field",
    [
        "flow_id",
        "project_id",
        "repository_identifier",
        "provider_pr_id",
        "pr_number",
        "head_sha",
        "id",
    ],
)
def test_persisted_context_mismatch_rejects(field: str) -> None:
    row = persisted()
    expected = {name: row[name] for name in example.CORRELATION}
    row[field] = "foreign"
    with pytest.raises(example.VerificationError):
        example.verify_execution(row, expected, EXECUTION)


@pytest.mark.parametrize("change", ["fork", "stale", "unapproved", "closed"])
def test_unsafe_pr_never_accepted(change: str) -> None:
    pr = approved_pr()
    if change == "fork":
        pr["head"]["repo"] = {"full_name": "foreign/fork"}
    elif change == "stale":
        pr["head"]["sha"] = "b" * 40
    elif change == "unapproved":
        pr["labels"] = []
    else:
        pr["state"] = "closed"
    with pytest.raises(example.VerificationError):
        example.verify_pr(pr, "example/repository", HEAD)


@pytest.mark.parametrize(
    "change",
    [None, "signature", "stale", "head_sha", "execution_id", "result_ready", "extra"],
)
def test_callback_matches_signed_bytes_and_persisted_result(change: str | None) -> None:
    row = persisted()
    payload = {
        name: row[name] for name in (*example.CORRELATION, "execution_id", "status")
    }
    payload["result_ready"] = True
    if change == "extra":
        payload["extra"] = "untrusted"
    elif change == "result_ready":
        payload["result_ready"] = False
    elif change in {"head_sha", "execution_id"}:
        payload[change] = "foreign"
    raw = json.dumps(
        {
            "id": "00000000-0000-4000-8000-000000000005",
            "type": "flow.execution.finished",
            "version": "1",
            "occurred_at": "2026-01-01T00:00:00Z",
            "account_id": "00000000-0000-4000-8000-000000000006",
            "data": payload,
        }
    ).encode()
    secret, timestamp = "synthetic-signing-secret", 1000
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    signature = f"t={timestamp},v1={digest if change != 'signature' else '0' * 64}"
    if change is None:
        assert example.verify_callback(raw, signature, secret, row, now=1000) == payload
    else:
        with pytest.raises(example.VerificationError):
            example.verify_callback(
                raw, signature, secret, row, now=2000 if change == "stale" else 1000
            )


@pytest.mark.parametrize("publish", [False, True])
def test_dedup_cancel_and_separate_review_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], publish: bool
) -> None:
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
    row = persisted()
    obsolete = {**row, "id": OBSOLETE, "head_sha": "b" * 40, "status": "RUNNING"}
    calls: list[tuple[str, str]] = []
    posted: dict[str, Any] = {}

    def request(
        base: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        calls.append((method, path))
        if path == "/users/github-actions%5Bbot%5D":
            return {"id": 31}
        if path.endswith("/pulls/7"):
            return approved_pr()
        if "/reviews?" in path:
            return []
        if path.endswith("/reviews"):
            posted.update(
                body,
                id=41,
                user={"id": 31},
                state="COMMENTED",
                submitted_at="2026-01-01T00:00:00Z",
            )
            return posted
        if path.endswith("/reviews/41"):
            return posted
        if "/flows/executions?" in path:
            return [obsolete, row]
        if path.endswith(OBSOLETE + "/command"):
            assert body == {"command": "stop"}
            return {"status": "stopped"}
        if path.endswith(OBSOLETE):
            return obsolete
        if path.endswith(EXECUTION) or path.endswith(EXECUTION + "/result"):
            return row
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(example, "request_json", request)
    args = argparse.Namespace(
        url="https://example.com",
        project=PROJECT,
        flow=FLOW,
        repository="example/repository",
        pr=7,
        head=HEAD,
        execution=None,
        timeout=1,
        callback_body=None,
        publish_review=publish,
        review_author="github-actions[bot]",
    )
    example.run(args)
    proof = json.loads(capsys.readouterr().out)
    assert proof["execution_id"] == EXECUTION
    assert proof["review_id"] == (41 if publish else None)
    assert ("POST", f"/api/v1/flows/executions/{OBSOLETE}/command") in calls
    assert not any(path.endswith("/trigger") for _, path in calls)
    assert bool(posted) is publish


def test_redirect_cannot_forward_credentials() -> None:
    assert (
        example.NoCredentialRedirect().redirect_request(
            None, None, 302, "", {}, "https://foreign.example"
        )
        is None
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com?token=synthetic",
        "https://example.com#synthetic",
    ],
)
def test_unsafe_origin_never_sends_credentials(
    origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unsafe origin reached the network")

    monkeypatch.setattr(example, "build_opener", network)
    with pytest.raises(example.VerificationError):
        example.request_json(origin, "/api/v1/ci-identities", "synthetic-human")


def _prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = dict(
        url="https://example.com",
        project=PROJECT,
        flow=FLOW,
        repository="example/repository",
        pr=7,
        head=HEAD,
        execution=None,
        timeout=1,
        callback_body=None,
        callback_account=None,
        publish_review=False,
        review_author="github-actions[bot]",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _route(
    calls: list[tuple[Any, ...]],
    *,
    executions: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    proof: dict[str, Any] | None = None,
    prs: list[Any] | None = None,
    trigger: dict[str, Any] | None = None,
) -> Any:
    posted: dict[str, Any] = {}
    pr_calls = 0

    def request(
        base: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        nonlocal pr_calls
        calls.append((method, path, body))
        if path == "/users/github-actions%5Bbot%5D":
            return {"id": 31}
        if path.endswith("/pulls/7") and "/reviews" not in path:
            pr_calls += 1
            if prs is not None:
                outcome = prs[min(pr_calls, len(prs)) - 1]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return approved_pr()
        if "/reviews?" in path:
            return [] if reviews is None else reviews
        if path.endswith("/reviews"):
            posted.update(
                body or {},
                id=41,
                user={"id": 31},
                state="COMMENTED",
                submitted_at="2026-01-01T00:00:00Z",
            )
            return posted
        if path.endswith("/reviews/41"):
            return posted if proof is None else proof
        if path.endswith("/trigger"):
            if trigger is None:
                raise AssertionError(path)
            return trigger
        if "/flows/executions?" in path:
            return [persisted()] if executions is None else executions
        if path.endswith("/command"):
            return {"status": "stopped"}
        if path.endswith(EXECUTION) or path.endswith(EXECUTION + "/result"):
            return persisted()
        raise AssertionError(path)

    return request


def test_null_review_body_does_not_abort_publication(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        example,
        "request_json",
        _route(
            calls,
            reviews=[
                {
                    "body": None,
                    "commit_id": HEAD,
                    "user": {"id": 31},
                    "state": "COMMENTED",
                    "submitted_at": "2026-01-01T00:00:00Z",
                }
            ],
        ),
    )
    example.run(_args(publish_review=True))
    assert json.loads(capsys.readouterr().out)["review_id"] == 41
    assert any(
        path.endswith("/reviews") and method == "POST" for method, path, _ in calls
    )


def test_null_receipt_body_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(monkeypatch)
    monkeypatch.setattr(
        example,
        "request_json",
        _route(calls=[], proof={"body": None, "user": {"id": 31}}),
    )
    with pytest.raises(example.VerificationError, match="receipt mismatch"):
        example.run(_args(publish_review=True))


def test_empty_history_triggers_and_rejects_a_mismatched_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    created = persisted()
    created["head_sha"] = "c" * 40
    monkeypatch.setattr(
        example,
        "request_json",
        _route(calls, executions=[], trigger=created),
    )
    with pytest.raises(example.VerificationError, match="correlation mismatch"):
        example.run(_args())
    assert (
        "POST",
        f"/api/v1/flows/{FLOW}/trigger",
        {"pr_number": 7, "head_sha": HEAD},
    ) in calls


def test_explicit_execution_skips_trigger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(example, "request_json", _route(calls, executions=[]))
    example.run(_args(execution=EXECUTION))
    proof = json.loads(capsys.readouterr().out)
    assert proof["execution_id"] == EXECUTION
    assert not any(path.endswith("/trigger") for _, path, _ in calls)


def test_transport_failure_leaves_the_owned_execution_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        example,
        "request_json",
        _route(
            calls,
            prs=[
                approved_pr(),
                example.TransportError("Remote request failed (HTTP 503)"),
            ],
        ),
    )
    with pytest.raises(example.TransportError, match="HTTP 503"):
        example.run(_args())
    assert not any(path.endswith("/command") for _, path, _ in calls)


def test_changed_head_stops_the_owned_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    stale = approved_pr()
    stale["head"] = {**stale["head"], "sha": "b" * 40}
    monkeypatch.setattr(
        example, "request_json", _route(calls, prs=[approved_pr(), stale])
    )
    with pytest.raises(example.VerificationError, match="approved same-repository"):
        example.run(_args())
    assert any(
        path.endswith("/command") and body == {"command": "stop"}
        for _, path, body in calls
    )


def _callback_bytes(secret: str, *, signature_ok: bool) -> tuple[bytes, str]:
    row = persisted()
    payload = {
        name: row[name] for name in (*example.CORRELATION, "execution_id", "status")
    }
    payload["result_ready"] = True
    raw = json.dumps(
        {
            "id": "00000000-0000-4000-8000-000000000005",
            "type": "flow.execution.finished",
            "version": "1",
            "occurred_at": "2026-01-01T00:00:00Z",
            "account_id": "00000000-0000-4000-8000-000000000006",
            "data": payload,
        }
    ).encode()
    stamp = int(time.time())
    digest = hmac.new(
        secret.encode(), str(stamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    if not signature_ok:
        digest = "0" * 64
    return raw, f"t={stamp},v1={digest}"


def test_run_checks_the_callback_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(monkeypatch)
    secret = "synthetic-signing-secret"
    raw, signature = _callback_bytes(secret, signature_ok=True)
    body = tmp_path / "callback.json"
    body.write_bytes(raw)
    monkeypatch.setenv("PRELOOP_CALLBACK_SECRET", secret)
    monkeypatch.setenv("PRELOOP_CALLBACK_SIGNATURE", signature)
    monkeypatch.setattr(example, "request_json", _route([]))
    example.run(
        _args(
            callback_body=str(body),
            callback_account="00000000-0000-4000-8000-000000000006",
        )
    )
    assert json.loads(capsys.readouterr().out)["status"] == "SUCCEEDED"


def test_run_rejects_a_mismatched_callback_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(monkeypatch)
    secret = "synthetic-signing-secret"
    raw, signature = _callback_bytes(secret, signature_ok=False)
    body = tmp_path / "callback.json"
    body.write_bytes(raw)
    monkeypatch.setenv("PRELOOP_CALLBACK_SECRET", secret)
    monkeypatch.setenv("PRELOOP_CALLBACK_SIGNATURE", signature)
    monkeypatch.setattr(example, "request_json", _route([]))
    with pytest.raises(example.VerificationError, match="signature mismatch"):
        example.run(_args(callback_body=str(body)))
