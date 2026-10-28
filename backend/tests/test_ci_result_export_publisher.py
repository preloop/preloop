"""Export publication verifies separate consent and independent provider proof."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.test_restricted_ci_review_example import (
    EXECUTION,
    FLOW,
    HEAD,
    PROJECT,
    _args,
    _prepare,
    _route,
    approved_pr,
    example,
)

ACCOUNT = "00000000-0000-4000-8000-000000000006"
GRANT = "00000000-0000-4000-8000-000000000007"
BASE = "b" * 40


def document() -> dict[str, Any]:
    result = {"review": "Synthetic approved result"}
    receipt = dict(
        version=1,
        account_id=ACCOUNT,
        execution_id=EXECUTION,
        flow_id=FLOW,
        project_id=PROJECT,
        repository_identifier="17",
        repository_slug="example/repository",
        tracker_type="github",
        tracker_url=None,
        pr_number=7,
        provider_pr_id="23",
        head_sha=HEAD,
        base_sha=BASE,
        result_sha256=example.json_digest(result),
    )
    return dict(
        grant_id=GRANT,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        receipt=receipt,
        purpose="result_export",
        publication_authority="separate_publisher_required",
        status="SUCCEEDED",
        result=result,
        publication_idempotency_key="preloop-result-export-v1:"
        + example.json_digest(receipt),
    )


def args(**overrides: Any) -> Any:
    return _args(
        execution=EXECUTION,
        result_export_grant=GRANT,
        export_account=ACCOUNT,
        base=BASE,
        expected_result_sha256=document()["receipt"]["result_sha256"],
        **overrides,
    )


def setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    export: dict[str, Any] | None = None,
    deny_second: bool = False,
    reviews: list[dict[str, Any]] | None = None,
    proof: dict[str, Any] | None = None,
    base: str = BASE,
) -> list[tuple[Any, ...]]:
    _prepare(monkeypatch)
    calls: list[tuple[Any, ...]] = []
    pr = approved_pr()
    pr["base"]["sha"] = base
    route = _route(calls, reviews=reviews, proof=proof, prs=[pr])
    reads = 0

    def request(
        origin: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        nonlocal reads
        if "/result-export/" in path:
            reads += 1
            calls.append((method, path, body))
            if deny_second and reads >= 2:
                raise example.TransportError("Grant revoked")
            return export if export is not None else document()
        if "/flows/" in path:
            raise AssertionError(
                "Export must never enumerate, launch, stop or read an owned execution"
            )
        return route(origin, path, token, method, body)

    monkeypatch.setattr(example, "request_json", request)
    return calls


def test_explicit_export_publishes_and_reads_back_without_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = setup(monkeypatch)
    example.run(args(publish_review=True))
    import json

    assert json.loads(capsys.readouterr().out)["review_id"] == 41
    assert sum("/result-export/" in path for _, path, _ in calls) == 3
    writes = [(path, body) for method, path, body in calls if method == "POST"]
    assert len(writes) == 1
    assert writes[0][1]["commit_id"] == HEAD
    assert document()["publication_idempotency_key"] in writes[0][1]["body"]


@pytest.mark.parametrize(
    "mutation",
    [
        "result",
        "digest",
        "head_sha",
        "base_sha",
        "account_id",
        "grant",
        "expiry",
        "marker",
    ],
)
def test_invalid_export_never_publishes(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    exported = deepcopy(document())
    if mutation == "result":
        exported["result"]["review"] = "Changed after consent"
    elif mutation == "digest":
        exported["receipt"]["result_sha256"] = "0" * 64
    elif mutation in {"head_sha", "base_sha", "account_id"}:
        exported["receipt"][mutation] = "different"
    elif mutation == "grant":
        exported["grant_id"] = ACCOUNT
    elif mutation == "expiry":
        exported["expires_at"] = "2000-01-01T00:00:00Z"
    else:
        exported["publication_idempotency_key"] = "forged"
    calls = setup(monkeypatch, export=exported)
    with pytest.raises(example.VerificationError):
        example.run(args(publish_review=True))
    assert not any(method == "POST" for method, _, _ in calls)


def test_expired_or_revoked_consent_before_write_never_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = setup(monkeypatch, deny_second=True)
    with pytest.raises(example.TransportError):
        example.run(args(publish_review=True))
    assert not any(method == "POST" for method, _, _ in calls)


def test_changed_provider_base_denies_export_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = setup(monkeypatch, base="c" * 40)
    with pytest.raises(example.VerificationError):
        example.run(args(publish_review=True))
    assert not any(method == "POST" for method, _, _ in calls)


def test_existing_export_receipt_prevents_duplicate_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {
        "id": 41,
        "body": "Synthetic approved result\n\n<!-- "
        + document()["publication_idempotency_key"]
        + " -->",
        "commit_id": HEAD,
        "user": {"id": 31},
        "state": "COMMENTED",
        "submitted_at": "2026-01-01T00:00:00Z",
    }
    calls = setup(monkeypatch, reviews=[proof], proof=proof)
    example.run(args(publish_review=True))
    assert not any(method == "POST" for method, _, _ in calls)


def test_export_publication_readback_requires_exact_review_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {
        "id": 41,
        "body": "Altered verdict\n\n<!-- "
        + document()["publication_idempotency_key"]
        + " -->",
        "commit_id": HEAD,
        "user": {"id": 31},
        "state": "COMMENTED",
        "submitted_at": "2026-01-01T00:00:00Z",
    }
    setup(monkeypatch, proof=proof)
    with pytest.raises(example.VerificationError, match="receipt mismatch"):
        example.run(args(publish_review=True))


@pytest.mark.parametrize(
    "tracker_url", ["https://github.com/", "https://api.github.com/"]
)
def test_export_accepts_conventional_tracker_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, tracker_url: str
) -> None:
    exported = document()
    exported["receipt"]["tracker_url"] = tracker_url
    exported["publication_idempotency_key"] = (
        "preloop-result-export-v1:" + example.json_digest(exported["receipt"])
    )
    calls = setup(monkeypatch, export=exported)
    example.run(args())
    assert not any(method == "POST" for method, _, _ in calls)


@pytest.mark.parametrize(
    "tracker_url",
    [
        "https://evil.example/",
        "https://github.com.evil.example/",
        "https://github.com/private/",
    ],
)
def test_export_rejects_untrusted_tracker_after_normalization(
    monkeypatch: pytest.MonkeyPatch, tracker_url: str
) -> None:
    exported = document()
    exported["receipt"]["tracker_url"] = tracker_url
    exported["publication_idempotency_key"] = (
        "preloop-result-export-v1:" + example.json_digest(exported["receipt"])
    )
    calls = setup(monkeypatch, export=exported)
    with pytest.raises(example.VerificationError, match="correlation"):
        example.run(args(publish_review=True))
    assert not any(method == "POST" for method, _, _ in calls)
