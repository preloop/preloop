"""Synthetic operator workflow verification; no external Actions activation."""

import argparse
import hashlib
import hmac
import importlib.util
import json
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
