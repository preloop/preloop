"""Synthetic repository_dispatch publisher verification; no Actions activation."""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from preloop.services.event_webhooks import github_dispatch
from preloop.services.event_webhooks.events import build_envelope
from preloop.services.execution_tool_evidence import digest

_scripts = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(_scripts))
_spec = importlib.util.spec_from_file_location(
    "restricted_ci_publish_example", _scripts / "restricted_ci_publish.py"
)
assert _spec is not None and _spec.loader is not None
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)

FLOW = "00000000-0000-4000-8000-000000000001"
PROJECT = "00000000-0000-4000-8000-000000000002"
EXECUTION = "00000000-0000-4000-8000-000000000003"
ACCOUNT = "00000000-0000-4000-8000-000000000006"
EVENT = "00000000-0000-4000-8000-000000000005"
HEAD, BASE = "a" * 40, "b" * 40
SECRET = "whsec_synthetic-signing-secret"
REPO = "example/repository"
RESULT = {"review": "Synthetic review"}


def projection(status: str = "SUCCEEDED") -> dict[str, Any]:
    return dict(
        id=EXECUTION,
        flow_id=FLOW,
        project_id=PROJECT,
        repository_identifier="17",
        provider_pr_id="23",
        pr_number=7,
        head_sha=HEAD,
        base_sha=BASE,
        status=status,
    )


def result_document() -> dict[str, Any]:
    return {
        "execution_id": EXECUTION,
        **projection(),
        "result": RESULT,
        "result_withheld_paths": [],
    }


def completion(**changes: Any) -> dict[str, Any]:
    """The data block the server builds in callback_payload."""
    data = {
        "execution_id": EXECUTION,
        "flow_id": FLOW,
        "project_id": PROJECT,
        "repository_identifier": "17",
        "pr_number": 7,
        "provider_pr_id": "23",
        "head_sha": HEAD,
        "status": "SUCCEEDED",
        "result_ready": True,
        "tracker_type": "github",
        "repository_slug": REPO,
        "base_branch": "main",
        "base_sha": BASE,
        # The server digest and the example digest must agree.
        "result_sha256": digest(RESULT),
    }
    data.update(changes)
    return data


def client_payload(
    data: dict[str, Any] | None = None, *, stamp: int | None = None
) -> dict[str, Any]:
    """Frame exactly as the delivery worker does, with the server helpers."""
    from datetime import datetime, timezone

    envelope = build_envelope(
        event_id=EVENT,
        event_type="flow.execution.finished",
        account_id=ACCOUNT,
        data=data or completion(),
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    body = json.dumps(envelope, default=str, separators=(",", ":")).encode()
    payload = github_dispatch.client_payload(
        envelope=body,
        secret=SECRET,
        event_id=EVENT,
        delivery_id="00000000-0000-4000-8000-000000000007",
        attempt=1,
        timestamp=stamp if stamp is not None else int(time.time()),
    )
    github_dispatch.request_body("preloop-review-completed", payload)
    return payload


def pull_request(head: str = HEAD, base: str = BASE) -> dict[str, Any]:
    repository = {"id": 17, "full_name": REPO}
    return dict(
        id=23,
        number=7,
        state="open",
        head={"sha": head, "repo": repository},
        base={"sha": base, "repo": repository},
    )


class Recorder:
    """Route example HTTP calls to synthetic Preloop and GitHub state."""

    def __init__(self, pr: dict[str, Any], existing_check: bool = False) -> None:
        self.pr = pr
        self.existing_check = existing_check
        self.posts: list[tuple[str, Any]] = []
        self.preloop_paths: list[str] = []

    def __call__(
        self, base: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        if base.startswith("https://preloop"):
            assert token.startswith("ci_") and method == "GET"
            self.preloop_paths.append(path)
            if path.endswith("/result"):
                return result_document()
            return projection()
        assert token == "github-token"
        if method == "POST":
            self.posts.append((path, body))
            return {"id": 99}
        if "/check-runs" in path:
            runs = [{"id": 5, "external_id": EXECUTION}] if self.existing_check else []
            return {"check_runs": runs}
        if path.endswith("/pulls/7"):
            return self.pr
        if "/reviews" in path:
            return []
        raise AssertionError(path)


def run_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
    recorder: Callable[..., Any],
) -> dict[str, Any]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "x", "client_payload": payload}))
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("PRELOOP_CALLBACK_SECRET", SECRET)
    monkeypatch.setattr(example, "request_json", recorder)
    return example.run(
        argparse.Namespace(
            url="https://preloop.example.com",
            project=PROJECT,
            flow=FLOW,
            repository=REPO,
            account=ACCOUNT,
            event_path=str(event),
            tolerance=example.DEFAULT_TOLERANCE_SECONDS,
        )
    )


def test_current_pr_gets_one_review_and_one_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = Recorder(pull_request())
    outcome = run_with(monkeypatch, tmp_path, client_payload(), recorder)
    assert outcome == {
        "execution_id": EXECUTION,
        "stale": False,
        "review_id": 99,
        "check_id": 99,
    }
    assert recorder.preloop_paths == [
        f"/api/v1/flows/executions/{EXECUTION}",
        f"/api/v1/flows/executions/{EXECUTION}/result",
    ]
    (review_path, review), (check_path, check) = recorder.posts
    assert review_path == f"/repos/{REPO}/pulls/7/reviews"
    assert review["commit_id"] == HEAD and review["event"] == "COMMENT"
    assert f"preloop-ci:{EXECUTION}:{HEAD}" in review["body"]
    assert check_path == f"/repos/{REPO}/check-runs"
    assert check["head_sha"] == HEAD and check["conclusion"] == "success"
    assert check["external_id"] == EXECUTION


@pytest.mark.parametrize("moved", ["head", "base"])
def test_stale_pr_gets_neutral_check_and_no_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, moved: str
) -> None:
    pr = pull_request(**{moved: "c" * 40})
    recorder = Recorder(pr)
    outcome = run_with(monkeypatch, tmp_path, client_payload(), recorder)
    assert outcome["stale"] is True
    [(path, check)] = recorder.posts
    assert path.endswith("/check-runs") and check["conclusion"] == "neutral"


def test_redelivery_publishes_nothing_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = Recorder(pull_request(), existing_check=True)
    recorder_reviews = [
        {
            "id": 41,
            "commit_id": HEAD,
            "body": f"x <!-- preloop-ci:{EXECUTION}:{HEAD} -->",
        }
    ]

    def route(base: str, path: str, token: str, method: str = "GET", body: Any = None):
        if "/reviews?" in path:
            return recorder_reviews
        return recorder(base, path, token, method, body)

    outcome = run_with(monkeypatch, tmp_path, client_payload(), route)
    assert outcome["review_id"] == 41 and outcome["check_id"] == 5
    assert recorder.posts == []


@pytest.mark.parametrize(
    "tamper",
    [
        "signature",
        "event_bytes",
        "stale",
        "event_id",
        "repository",
        "flow",
        "base_missing",
        "base_mismatch",
        "digest",
        "head",
        "unknown_field",
    ],
)
def test_untrusted_or_mismatched_completion_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    changes: dict[str, Any] = {
        "repository": {"repository_slug": "other/repository"},
        "flow": {"flow_id": PROJECT},
        "base_missing": {"base_sha": None},
        "base_mismatch": {"base_sha": "d" * 40},
        "digest": {"result_sha256": "0" * 64},
        "head": {"head_sha": "e" * 40},
        "unknown_field": {"extra": "untrusted"},
    }.get(tamper, {})
    stamp = int(time.time()) - (example.DEFAULT_TOLERANCE_SECONDS + 60)
    payload = client_payload(
        completion(**changes), stamp=stamp if tamper == "stale" else None
    )
    if tamper == "signature":
        payload["signature"] = payload["signature"][:-4] + "0000"
    elif tamper == "event_bytes":
        payload["event"] = payload["event"].replace('"pr_number":7', '"pr_number":8')
    elif tamper == "event_id":
        payload["event_id"] = "00000000-0000-4000-8000-000000000099"
    recorder = Recorder(pull_request())
    with pytest.raises(example.VerificationError):
        run_with(monkeypatch, tmp_path, payload, recorder)
    assert recorder.posts == []


def test_signature_window_tolerates_dispatch_queueing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dispatch that waited ten minutes in GitHub's queue still verifies."""
    recorder = Recorder(pull_request())
    payload = client_payload(stamp=int(time.time()) - 600)
    assert run_with(monkeypatch, tmp_path, payload, recorder)["stale"] is False


def test_missing_secrets_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PRELOOP_CALLBACK_SECRET", raising=False)
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    with pytest.raises(example.VerificationError):
        example.run(
            argparse.Namespace(
                url="https://preloop.example.com",
                project=PROJECT,
                flow=FLOW,
                repository=REPO,
                account=None,
                event_path=str(tmp_path / "missing.json"),
                tolerance=60,
            )
        )


def test_workflow_holds_the_only_write_permissions() -> None:
    path = Path(__file__).parents[2] / "docs" / "examples" / "restricted-ci-publish.yml"
    workflow = yaml.safe_load(path.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers) == {"repository_dispatch"}
    assert triggers["repository_dispatch"]["types"] == ["preloop-review-completed"]
    assert workflow["permissions"] == {
        "contents": "read",
        "checks": "write",
        "pull-requests": "write",
    }
    [step] = [s for s in workflow["jobs"]["publish"]["steps"] if "run" in s]
    assert "client_payload" not in step["run"]
    assert "scripts/restricted_ci_publish.py" in step["run"]
    checkout = workflow["jobs"]["publish"]["steps"][0]
    assert checkout["with"]["persist-credentials"] is False
