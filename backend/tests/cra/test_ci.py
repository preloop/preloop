"""Fail-closed CRA CI helper. No live network."""

from __future__ import annotations

import gzip
import io
import json
import os
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
from preloop.cra.validate import GatePolicy

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


def test_incompletion_envelope_denies_release_with_the_stated_reason() -> None:
    """A valid report of an unfinished run is denied in those words."""
    payload = {
        "schema": "preloop.cra.releaseaudit/v1",
        "flow": "release-security-audit",
        "run_at": "2026-09-08T10:15:00Z",
        "regime_profile": "cra",
        "verdict": "error",
        "incomplete": {
            "reason": "The interactive waiver approval did not resolve in time.",
            "stage": "PHASE 2 waiver collection",
        },
        "disclaimer": (
            "Machine-generated evidence for conformity assessment support. "
            "Not a conformity assessment, certification, or legal advice."
        ),
    }
    accepted, reason, validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert validation.ok and validation.incomplete
    assert reason == (
        "run did not complete: The interactive waiver approval did not resolve in time."
    )


def test_model_cvss_99_display_cannot_pass_ci_gate(
    vulnscan_result: dict[str, Any],
) -> None:
    payload = clone(vulnscan_result)
    payload["findings"] = [
        {
            **payload["findings"][0],
            "id": "CVE-2026-0001",
            "cvss": 9.8,
            "kev": False,
            "waived": False,
            "severity": "critical",
        }
    ]
    payload["counts_by_severity"] = {
        "critical": 1,
        "high": 0,
        "medium": 0,
        "low": 0,
        "unknown": 0,
    }
    payload["gate"]["passed"] = True
    payload["gate"]["policy"] = "fail on CVSS >= 99"
    accepted, _reason, validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not accepted
    assert validation.invalid


def test_ci_operator_cvss_override(
    vulnscan_result: dict[str, Any],
) -> None:
    payload = clone(vulnscan_result)
    payload["findings"] = [
        {
            **payload["findings"][0],
            "cvss": 8.0,
            "kev": False,
            "waived": False,
            "severity": "high",
        }
    ]
    payload["counts_by_severity"] = {
        "critical": 0,
        "high": 1,
        "medium": 0,
        "low": 0,
        "unknown": 0,
    }
    payload["gate"]["passed"] = True
    default_accepted, _, default_validation = evaluate_release(
        payload, policy=ReleasePolicy(), evidence_received=True
    )
    assert not default_validation.invalid
    accepted, _, validation = evaluate_release(
        payload,
        policy=ReleasePolicy(),
        evidence_received=True,
        gate_policy=GatePolicy(fail_on_kev=True, fail_on_cvss_gte=7.0),
    )
    assert not accepted
    assert validation.invalid
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


def _archive_with_blob(result: dict[str, Any] | None, blob: bytes, name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if result is not None:
            packed = json.dumps(result).encode("utf-8")
            info = tarfile.TarInfo("result.json")
            info.size = len(packed)
            tar.addfile(info, io.BytesIO(packed))
        info = tarfile.TarInfo(name)
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    return buf.getvalue()


def test_archive_above_old_2mib_cap_is_accepted(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    blob = os.urandom(2 * 1024 * 1024 + 512 * 1024)
    archive = _archive_with_blob(sbomaudit_result, blob, "evidence/blob.bin")
    assert len(archive) > 2 * 1024 * 1024
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


def test_oversize_archive_is_rejected(
    monkeypatch: Any, tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_evidence_max_bytes", 1024 * 1024)
    blob = os.urandom(2 * 1024 * 1024 + 1024)
    archive = _archive_with_blob(sbomaudit_result, blob, "evidence/blob.bin")
    assert len(archive) > 1024 * 1024

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="exceeds size bound|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=sbomaudit_result,
        )


def test_legacy_pack_without_result_json_requires_digest(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _archive_with_blob(None, b"# evidence\n", "evidence/sbom-verify.md")
    digest = archive_sha256(archive)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            return _FakeResponse(
                json.dumps(
                    {
                        "status": "available",
                        "execution_id": "exec-1",
                        "sha256": digest,
                        "kind": "evidence",
                    }
                ).encode()
            )
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
    assert dest.read_bytes() == archive


def test_legacy_flat_paths_accepted_with_digest(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _archive_with_blob(None, b"# evidence\n", "sbom-verify.md")
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
    assert dest.read_bytes() == archive


def test_legacy_pack_without_digest_is_rejected(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _archive_with_blob(None, b"# evidence\n", "evidence/sbom-verify.md")

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="legacy|digest|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=sbomaudit_result,
        )


def test_swapped_sbom_content_rejected_despite_same_verdict(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    packed = clone(sbomaudit_result)
    packed["coverage"] = dict(packed["coverage"])
    packed["coverage"]["components"] = 99
    archive = _make_evidence_archive(packed)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="content|inconsistent|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=sbomaudit_result,
        )


def test_rejected_decision_rejected_despite_same_envelope(
    tmp_path: Path, duediligence_result: dict[str, Any]
) -> None:
    packed = clone(duediligence_result)
    packed["decision"] = dict(packed["decision"])
    packed["decision"]["outcome"] = "rejected"
    archive = _make_evidence_archive(packed)

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="content|inconsistent|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=duediligence_result,
        )


def test_gate_waiver_and_gap_mutations_rejected(
    tmp_path: Path, releaseaudit_result: dict[str, Any]
) -> None:
    gate = clone(releaseaudit_result)
    gate["vuln_scan"] = dict(gate["vuln_scan"])
    gate["vuln_scan"]["gate"] = dict(gate["vuln_scan"]["gate"])
    gate["vuln_scan"]["gate"]["passed"] = False
    waived = clone(releaseaudit_result)
    waived["vuln_scan"] = dict(waived["vuln_scan"])
    waived["vuln_scan"]["gate"] = dict(waived["vuln_scan"]["gate"])
    waived["vuln_scan"]["gate"]["waivers_applied"] = [
        {
            "id": "CVE-2024-0001",
            "reason": "invented",
            "author": "agent@example.com",
            "date": "2026-08-20",
        }
    ]
    gapped = clone(releaseaudit_result)
    gapped["gap_register"] = {"items": [], "history_rows": []}

    for packed in (gate, waived, gapped):
        archive = _make_evidence_archive(packed)

        def opener(
            request: object, timeout: int = 0, body: bytes = archive
        ) -> _FakeResponse:
            url = _request_url(request)
            if url.endswith("/evidence-status"):
                raise _http_error(url, 404)
            return _FakeResponse(body, headers={"content-type": "application/gzip"})

        with pytest.raises(CraCIError, match="content|inconsistent|rejected"):
            fetch_evidence(
                "https://preloop.example.com",
                "token",
                "exec-1",
                tmp_path / "evidence.tar.gz",
                opener=opener,
                max_retries=1,
                api_result=releaseaudit_result,
            )


def test_controller_annotations_allowed_when_agent_json_matches(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    archive = _make_evidence_archive(sbomaudit_result)
    api = clone(sbomaudit_result)
    api["trusted_publication"] = {"url": "https://example.com/pr/1"}
    api["verification"] = {"status": "passed", "source": "sandbox_log"}
    api["dossier"] = {"id": "dossier-1"}
    api["provenance"] = {"controller": True}
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"schema": "preloop.cra.dossier_manifest/v1"}

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    dest = fetch_evidence(
        "https://preloop.example.com",
        "token",
        "exec-1",
        tmp_path / "evidence.tar.gz",
        opener=opener,
        max_retries=1,
        api_result=api,
    )
    assert dest.read_bytes() == archive


def test_persist_then_ci_download_accepts_product_provenance_annotations(
    tmp_path: Path, releaseaudit_result: dict[str, Any]
) -> None:
    from preloop.cra.persist import apply_cra_persist_boundary

    persisted = apply_cra_persist_boundary(clone(releaseaudit_result))
    assert not persisted.invalid
    assert isinstance(persisted.artifact, dict)
    archive = _make_evidence_archive(persisted.artifact)
    api = clone(persisted.artifact)
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"schema": "preloop.cra.dossier_manifest/v1"}

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    dest = fetch_evidence(
        "https://preloop.example.com",
        "token",
        "exec-1",
        tmp_path / "evidence.tar.gz",
        opener=opener,
        max_retries=1,
        api_result=api,
    )
    assert dest.read_bytes() == archive

    mutated = clone(persisted.artifact)
    mutated["vuln_scan"] = dict(mutated["vuln_scan"])
    mutated["vuln_scan"]["gate"] = dict(mutated["vuln_scan"]["gate"])
    mutated["vuln_scan"]["gate"]["passed"] = False
    bad_archive = _make_evidence_archive(mutated)

    def bad_opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(bad_archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="content|inconsistent|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence-bad.tar.gz",
            opener=bad_opener,
            max_retries=1,
            api_result=api,
        )


def test_agent_evidence_annotation_is_not_controller_digest(
    tmp_path: Path, sbomaudit_result: dict[str, Any]
) -> None:
    payload = clone(sbomaudit_result)
    payload["evidence"] = {"sha256": "0" * 64, "status": "available"}
    archive = _archive_with_blob(None, b"# evidence\n", "evidence/note.md")

    def opener(request: object, timeout: int = 0) -> _FakeResponse:
        url = _request_url(request)
        if url.endswith("/evidence-status"):
            raise _http_error(url, 404)
        return _FakeResponse(archive, headers={"content-type": "application/gzip"})

    with pytest.raises(CraCIError, match="legacy|digest|rejected"):
        fetch_evidence(
            "https://preloop.example.com",
            "token",
            "exec-1",
            tmp_path / "evidence.tar.gz",
            opener=opener,
            max_retries=1,
            api_result=payload,
        )
