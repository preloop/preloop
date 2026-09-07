"""Fail-closed CRA CI helper. No live network."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from preloop.cra.ci import (
    STARTUP_BUFFER_SECONDS,
    CraCIConfig,
    CraCIError,
    ReleasePolicy,
    _NoAuthRedirectHandler,
    evaluate_release,
    fetch_evidence,
    json_request,
    poll_execution,
    post_webhook,
    redact_url,
    request_with_retries,
    resolve_overall_timeout,
    retain_artifacts,
    run_cra_ci,
)
from preloop.cra.evidence_pack import archive_sha256

from .conftest import clone


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._remaining = payload
        self.status = status
        self.headers = headers or {"content-type": "application/json"}

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._remaining
            self._remaining = b""
            return data
        data = self._remaining[:n]
        self._remaining = self._remaining[n:]
        return data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _make_evidence_archive(result: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        packed = json.dumps(result).encode("utf-8")
        info = tarfile.TarInfo("result.json")
        info.size = len(packed)
        tar.addfile(info, io.BytesIO(packed))
        note = b"# CRA evidence member\n"
        info = tarfile.TarInfo("evidence/sbom-verify.md")
        info.size = len(note)
        tar.addfile(info, io.BytesIO(note))
    return buf.getvalue()


def _request_url(request: object) -> str:
    return str(getattr(request, "full_url", "") or request.get_full_url())


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url=url, code=code, msg="error", hdrs=None, fp=io.BytesIO(b""))


def test_unknown_verdict_is_rejected(sbomaudit_result: dict[str, Any]) -> None:
    payload = clone(sbomaudit_result)
    payload["verdict"] = "pretty_good"
    accepted, reason, _validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert "verdict" in reason.lower() or "must be" in reason


def test_gate_passed_must_be_strict_true(
    vulnscan_result: dict[str, Any],
) -> None:
    payload = clone(vulnscan_result)
    payload["gate"]["passed"] = 1
    accepted, reason, _validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert "boolean true" in reason or "boolean" in reason


def test_missing_evidence_denies_release(
    releaseaudit_result: dict[str, Any],
) -> None:
    accepted, reason, _validation = evaluate_release(
        releaseaudit_result,
        policy=ReleasePolicy.from_name("pass_with_findings"),
        evidence_received=False,
    )
    assert not accepted
    assert "evidence" in reason


def test_pass_with_findings_denied_by_default(
    sbomaudit_result: dict[str, Any],
) -> None:
    accepted, reason, _validation = evaluate_release(
        sbomaudit_result, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert "denied" in reason


def test_pass_with_findings_requires_explicit_policy(
    sbomaudit_result: dict[str, Any],
) -> None:
    accepted, reason, _validation = evaluate_release(
        sbomaudit_result,
        policy=ReleasePolicy.from_name("pass_with_findings"),
        evidence_received=True,
    )
    assert accepted
    assert "pass_with_findings" in reason


def test_pass_only_policy_rejects_findings(
    sbomaudit_result: dict[str, Any],
) -> None:
    policy = ReleasePolicy.from_name("pass")
    accepted, reason, _validation = evaluate_release(
        sbomaudit_result, policy=policy, evidence_received=True
    )
    assert not accepted
    assert "denied" in reason


def test_fail_policy_is_rejected(sbomaudit_result: dict[str, Any]) -> None:
    payload = clone(sbomaudit_result)
    payload["valid"] = False
    payload["minimum_elements"]["passed"] = False
    payload["verdict"] = "fail"
    with pytest.raises(CraCIError, match="not supported"):
        ReleasePolicy.from_name("fail")
    accepted, reason, _validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert "fail" in reason


def test_policy_cannot_accept_without_evidence(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    payload["verdict"] = "pass"
    payload["license_flags"] = []
    accepted, reason, _validation = evaluate_release(
        payload,
        policy=ReleasePolicy.from_name("pass_with_findings"),
        evidence_received=False,
    )
    assert not accepted
    assert "evidence" in reason


def test_poll_timeout_raises() -> None:
    clock = {"t": 0.0}

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        return _FakeResponse(json.dumps({"status": "RUNNING"}).encode())

    def monotonic() -> float:
        return clock["t"]

    def sleep(_s: float) -> None:
        clock["t"] += 1.0

    with pytest.raises(CraCIError, match="timed out"):
        poll_execution(
            "https://preloop.example.com",
            "token",
            "exec-1",
            poll_interval=0,
            overall_timeout=1,
            request_timeout=1,
            opener=opener,
            sleep=sleep,
            monotonic=monotonic,
        )


def test_http_timeout_is_bounded() -> None:
    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        raise URLError("timed out")

    with pytest.raises(CraCIError, match="failed"):
        json_request(
            "GET",
            "https://preloop.example.com/api/v1/flows/executions/x",
            token="t",
            timeout=1,
            opener=opener,
        )


def test_webhook_errors_redact_url_secret() -> None:
    secret = "super-secret-webhook-token"
    url = f"https://preloop.example.com/api/v1/webhooks/flows/flow-1/{secret}"

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        raise URLError(url)

    with pytest.raises(CraCIError) as excinfo:
        json_request("POST", url, opener=opener, timeout=1)
    message = str(excinfo.value)
    assert secret not in message
    assert "***" in message
    assert url not in message


def test_webhook_http_error_omits_server_body() -> None:
    secret = "super-secret-webhook-token"
    url = f"https://preloop.example.com/api/v1/webhooks/flows/flow-1/{secret}"

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        raise HTTPError(
            url=url,
            code=500,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(b'{"token":"leaked-credential"}'),
        )

    status, payload, _headers = json_request("POST", url, opener=opener, timeout=1)
    assert status == 500
    assert payload == b""
    with pytest.raises(CraCIError) as excinfo:
        post_webhook(url, {"release_ref": "v1"}, opener=opener, timeout=1)
    assert "leaked-credential" not in str(excinfo.value)
    assert secret not in str(excinfo.value)


def test_webhook_post_is_not_retried_on_500() -> None:
    calls = {"n": 0}
    url = "https://preloop.example.com/api/v1/webhooks/flows/flow-1/secret"

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        calls["n"] += 1
        raise HTTPError(
            url=url, code=500, msg="error", hdrs=None, fp=io.BytesIO(b"err")
        )

    with pytest.raises(CraCIError, match="HTTP 500"):
        post_webhook(url, {"release_ref": "v1"}, opener=opener, timeout=1)
    assert calls["n"] == 1


def test_idempotent_get_may_retry_500() -> None:
    calls = {"n": 0}

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError(
                url="https://preloop.example.com/x",
                code=500,
                msg="error",
                hdrs=None,
                fp=io.BytesIO(b""),
            )
        return _FakeResponse(json.dumps({"ok": True}).encode())

    status, payload, _headers = request_with_retries(
        "GET",
        "https://preloop.example.com/api/v1/flows/executions/x",
        opener=opener,
        max_retries=3,
        sleep=lambda _s: None,
    )
    assert status == 200
    assert calls["n"] == 2
    assert json.loads(payload)["ok"] is True


def test_cross_origin_redirect_strips_authorization() -> None:
    handler = _NoAuthRedirectHandler()
    req = Request(
        "https://preloop.example.com/api/v1/flows/executions/x",
        headers={"Authorization": "Bearer secret-token"},
        method="GET",
    )
    redirected = handler.redirect_request(
        req,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl="https://evil.example.net/capture",
    )
    assert redirected is not None
    header_names = {name.lower() for name, _value in redirected.header_items()}
    assert "authorization" not in header_names
    unredirected = getattr(redirected, "unredirected_hdrs", {})
    assert not any(key.lower() == "authorization" for key in unredirected)


def test_same_origin_redirect_keeps_authorization() -> None:
    handler = _NoAuthRedirectHandler()
    req = Request(
        "https://preloop.example.com/api/v1/flows/executions/x",
        headers={"Authorization": "Bearer secret-token"},
        method="GET",
    )
    redirected = handler.redirect_request(
        req,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl="https://preloop.example.com/api/v1/flows/executions/x/",
    )
    assert redirected is not None
    values = [
        value
        for name, value in redirected.header_items()
        if name.lower() == "authorization"
    ]
    assert values == ["Bearer secret-token"]


def test_redact_url_masks_webhook_secret() -> None:
    url = "https://preloop.example.com/api/v1/webhooks/flows/abc/whsec_live"
    redacted = redact_url(url)
    assert "whsec_live" not in redacted
    assert redacted.endswith("***")


def test_fetch_evidence_missing_is_error() -> None:
    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        raise _http_error(_request_url(request), 404)

    with pytest.raises(CraCIError, match="HTTP 404"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            Path("/tmp/evidence-missing.tar.gz"),
            opener=opener,
            max_retries=1,
        )


@pytest.mark.parametrize(
    "payload",
    [b"", b"<html>not gzip</html>", gzip.compress(b"not a tar")],
)
def test_fetch_evidence_rejects_invalid_200_bodies(
    tmp_path: Path, payload: bytes
) -> None:
    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(payload, headers={"content-type": "text/html"})

    with pytest.raises(CraCIError, match="evidence pack rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
        )


def test_fetch_evidence_rejects_digest_mismatch(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _make_evidence_archive(sbomaudit_result)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(
            archive,
            headers={
                "content-type": "application/gzip",
                "x-preloop-evidence-sha256": "0" * 64,
                "x-preloop-evidence-status": "available",
            },
        )

    with pytest.raises(CraCIError, match="digest"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=sbomaudit_result,
        )


def test_fetch_evidence_accepts_matching_digest(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _make_evidence_archive(sbomaudit_result)
    digest = archive_sha256(archive)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(
            archive,
            headers={
                "content-type": "application/gzip",
                "x-preloop-evidence-sha256": digest,
                "x-preloop-evidence-status": "available",
            },
        )

    dest = fetch_evidence(
        "https://preloop.example.com",
        "token",
        "exec-1",
        tmp_path / "evidence.tar.gz",
        opener=opener,
        max_retries=1,
        api_result=sbomaudit_result,
    )
    assert dest.is_file()
    assert dest.read_bytes() == archive


def test_fetch_evidence_rejects_result_mismatch(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    packed = clone(sbomaudit_result)
    packed["verdict"] = "fail"
    archive = _make_evidence_archive(packed)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="inconsistent"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=sbomaudit_result,
        )


def test_artifacts_retained_on_failure(
    tmp_path: Path, releaseaudit_result: dict[str, Any]
) -> None:
    retain_artifacts(
        tmp_path,
        result_body=releaseaudit_result,
        evidence_path=None,
        envelope={"execution_id": "e", "result": releaseaudit_result},
    )
    assert (tmp_path / "result.json").is_file()
    assert (tmp_path / "result-wrap.json").is_file()


def test_run_retains_artifacts_when_webhook_times_out(tmp_path: Path) -> None:
    secret = "webhook-secret-value"

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        raise URLError("timed out")

    config = CraCIConfig(
        api_url="https://preloop.example.com",
        token="token",
        webhook_url=f"https://preloop.example.com/api/v1/webhooks/flows/f/{secret}",
        artifacts_dir=tmp_path,
        overall_timeout=5,
        request_timeout=1,
        max_retries=1,
    )
    with pytest.raises(CraCIError) as excinfo:
        run_cra_ci(
            config, {"release_ref": "v1.2.3"}, opener=opener, sleep=lambda _s: None
        )
    assert secret not in str(excinfo.value)
    assert (tmp_path / "result.json").is_file()


def test_exhausted_poll_retains_without_duplicate_webhook(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    posts = {"n": 0}
    archive = _make_evidence_archive(sbomaudit_result)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        method = getattr(request, "get_method", lambda: "GET")()
        if method == "POST":
            posts["n"] += 1
            return _FakeResponse(json.dumps({"execution_id": "exec-1"}).encode())
        if "/evidence-status" in url:
            raise _http_error(url, 404)
        if url.endswith("/evidence"):
            return _FakeResponse(archive, headers={"content-type": "application/gzip"})
        if url.endswith("/result"):
            return _FakeResponse(
                json.dumps(
                    {"execution_id": "exec-1", "result": sbomaudit_result}
                ).encode()
            )
        return _FakeResponse(
            json.dumps(
                {"id": "exec-1", "status": "RUNNING", "flow_id": "flow-1"}
            ).encode()
        )

    config = CraCIConfig(
        api_url="https://preloop.example.com",
        token="token",
        webhook_url="https://preloop.example.com/api/v1/webhooks/flows/f/secret",
        artifacts_dir=tmp_path,
        overall_timeout=1,
        poll_interval=0,
        request_timeout=1,
        max_retries=1,
    )
    clock = {"t": 0.0}

    def monotonic() -> float:
        return clock["t"]

    def sleep(_s: float) -> None:
        clock["t"] += 1.0

    with pytest.raises(CraCIError, match="timed out"):
        run_cra_ci(
            config,
            {"release_ref": "v1.2.3"},
            opener=opener,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert posts["n"] == 1
    retained = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert retained["schema"] == sbomaudit_result["schema"]
    assert list(tmp_path.glob("evidence-*.tar.gz"))


def test_failed_execution_retains_result_and_denies_release(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _make_evidence_archive(sbomaudit_result)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        method = getattr(request, "get_method", lambda: "GET")()
        if method == "POST":
            return _FakeResponse(json.dumps({"execution_id": "exec-1"}).encode())
        if "/evidence-status" in url:
            raise _http_error(url, 404)
        if url.endswith("/evidence"):
            return _FakeResponse(archive, headers={"content-type": "application/gzip"})
        if url.endswith("/result"):
            return _FakeResponse(
                json.dumps(
                    {"execution_id": "exec-1", "result": sbomaudit_result}
                ).encode()
            )
        return _FakeResponse(
            json.dumps(
                {"id": "exec-1", "status": "FAILED", "flow_id": "flow-1"}
            ).encode()
        )

    config = CraCIConfig(
        api_url="https://preloop.example.com",
        token="token",
        webhook_url="https://preloop.example.com/api/v1/webhooks/flows/f/secret",
        artifacts_dir=tmp_path,
        overall_timeout=30,
        request_timeout=1,
        max_retries=1,
    )
    with pytest.raises(CraCIError, match="did not succeed"):
        run_cra_ci(config, {"release_ref": "v1"}, opener=opener, sleep=lambda _s: None)
    assert (tmp_path / "result.json").is_file()


def test_deadline_uses_flow_timeout_plus_buffer() -> None:
    assert resolve_overall_timeout(explicit=None, flow_timeout_seconds=7200) == (
        7200 + STARTUP_BUFFER_SECONDS
    )
    assert resolve_overall_timeout(explicit=90, flow_timeout_seconds=7200) == 90
    with pytest.raises(CraCIError, match="deadline is unset"):
        resolve_overall_timeout(explicit=None, flow_timeout_seconds=None)
