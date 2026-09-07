"""Fail-closed CI client for CRA evidence-pack flows.

Stdlib only (``urllib``). Schema validation uses :mod:`preloop.cra.validate`
so this module is not a second unmaintained validator. Webhook POST stays
unauthenticated (secret in the URL). Result and evidence GETs use the account
API token (``view_flows``), matching the original runbook.

The webhook URL is a bearer secret: error strings never include it, and
non-idempotent POSTs are not retried. Cross-origin redirects do not forward
``Authorization``.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlparse, urlunparse

from preloop.cra.evidence_pack import (
    MAX_EVIDENCE_ARCHIVE_BYTES,
    EvidencePackError,
    accept_evidence_archive,
    same_origin,
)
from preloop.cra.schemas import (
    AUDIT_VERDICTS,
    SCHEMA_DUEDILIGENCE_V1,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
)
from preloop.cra.validate import CraValidationResult, validate_cra_result

DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_POLL_INTERVAL = 10
STARTUP_BUFFER_SECONDS = 120
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
MAX_JSON_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BODY_BYTES = 4096
TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "CANCELLED"}
)
WORKSPACE_FILES_ENCODED_CAP = 1024 * 1024

OpenUrl = Callable[..., Any]


class CraCIError(RuntimeError):
    """Raised when the CI helper cannot complete a fail-closed gate."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ReleasePolicy:
    """Explicit CI release policy for a completed CRA pack.

    Default accepts a clean pass only. ``pass_with_findings`` is an operator
    choice. There is no failure-bypass mode: ``fail`` and unknown verdicts
    always deny release. Invalid or unavailable evidence cannot be accepted.
    """

    accept_pass: bool = True
    accept_pass_with_findings: bool = False
    require_schema_validation: bool = True
    require_coverage: bool = False

    @classmethod
    def from_name(cls, name: str) -> "ReleasePolicy":
        """Build a policy from a documented name.

        ``pass`` (default): only a clean pack may release.
        ``pass_with_findings``: completed audits with findings may release;
        ``fail`` and unknown verdicts may not.
        """
        normalized = name.strip().lower().replace("-", "_")
        if normalized in {"pass", "pass_only", "clean", "default"}:
            return cls()
        if normalized == "pass_with_findings":
            return cls(accept_pass_with_findings=True)
        if normalized == "fail":
            raise CraCIError(
                "release policy 'fail' is not supported; fail and unknown "
                "verdicts cannot be accepted"
            )
        raise CraCIError(f"unknown release policy {name!r}")


@dataclass
class CraCIConfig:
    """Runtime settings for the CI helper."""

    api_url: str
    token: str
    webhook_url: str
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    overall_timeout: Optional[int] = None
    max_retries: int = MAX_RETRIES
    artifacts_dir: Path = Path("artifacts")
    policy: ReleasePolicy = ReleasePolicy()
    expected_schema: Optional[str] = None


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization when a redirect leaves the original origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        origin = req.full_url
        if not same_origin(origin, new_req.full_url):
            _drop_authorization(new_req)
        return new_req


def _drop_authorization(request: urllib.request.Request) -> None:
    for header in list(request.header_items()):
        if header[0].lower() == "authorization":
            del request.headers[header[0]]
    unredirected = getattr(request, "unredirected_hdrs", None)
    if isinstance(unredirected, dict):
        for key in list(unredirected):
            if key.lower() == "authorization":
                unredirected.pop(key, None)


def _default_opener() -> OpenUrl:
    context = ssl.create_default_context()
    https = urllib.request.HTTPSHandler(context=context)
    opener = urllib.request.build_opener(https, _NoAuthRedirectHandler())
    return opener.open


def redact_url(url: str) -> str:
    """Redact webhook secrets and query strings from operator-visible URLs."""
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    if "webhooks" in parts:
        for index in range(len(parts) - 1, -1, -1):
            if parts[index]:
                parts[index] = "***"
                break
    query = "redacted" if parsed.query else ""
    return urlunparse((parsed.scheme, parsed.netloc, "/".join(parts), "", query, ""))


def _read_bounded(stream: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise CraCIError("HTTP response exceeds size bound")
        chunks.append(chunk)
    return b"".join(chunks)


def json_request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[bytes] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    accept: str = "application/json",
    opener: Optional[OpenUrl] = None,
    max_body: int = MAX_JSON_RESPONSE_BYTES,
) -> tuple[int, bytes, Mapping[str, str]]:
    """Perform one HTTP request. ``opener`` is injected in tests (no live net)."""
    headers = {"Accept": accept}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    open_fn = opener or _default_opener()
    safe_url = redact_url(url)
    try:
        try:
            response = open_fn(request, timeout=timeout)
        except TypeError:
            response = open_fn(request)
    except urllib.error.HTTPError as exc:
        if exc.fp is not None:
            try:
                _read_bounded(exc, MAX_ERROR_BODY_BYTES)
            except CraCIError:
                pass
        headers_map = {
            k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])
        }
        return exc.code, b"", headers_map
    except urllib.error.URLError as exc:
        raise CraCIError(
            f"HTTP {method} {safe_url} failed ({type(exc.reason).__name__})"
        ) from exc
    except TimeoutError as exc:
        raise CraCIError(
            f"HTTP {method} {safe_url} timed out after {timeout}s"
        ) from exc
    with response:
        payload = _read_bounded(response, max_body)
        status = int(getattr(response, "status", 200))
        raw_headers = getattr(response, "headers", None)
        headers_map = {
            k.lower(): v for k, v in (raw_headers.items() if raw_headers else [])
        }
        return status, payload, headers_map


def request_with_retries(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[bytes] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    opener: Optional[OpenUrl] = None,
    accept: str = "application/json",
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: Optional[float] = None,
    allow_retry: bool = True,
    max_body: int = MAX_JSON_RESPONSE_BYTES,
) -> tuple[int, bytes, Mapping[str, str]]:
    """Retry idempotent GETs. Non-idempotent POSTs are attempted once."""
    last_error: Optional[Exception] = None
    attempts = max(1, max_retries) if allow_retry else 1
    for attempt in range(attempts):
        if deadline is not None and monotonic() >= deadline:
            raise CraCIError("overall CRA CI deadline exceeded")
        remaining = timeout
        if deadline is not None:
            remaining = max(1, min(timeout, int(deadline - monotonic())))
        try:
            status, payload, headers = json_request(
                method,
                url,
                token=token,
                body=body,
                timeout=remaining,
                opener=opener,
                accept=accept,
                max_body=max_body,
            )
        except CraCIError as exc:
            last_error = exc
            if not allow_retry or attempt + 1 >= attempts:
                raise
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if allow_retry and status >= 500 and attempt + 1 < attempts:
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        return status, payload, headers
    raise CraCIError(f"HTTP {method} {redact_url(url)} failed: {last_error}")


def _join_api(api_url: str, path: str) -> str:
    base = api_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def resolve_overall_timeout(
    *,
    explicit: Optional[int],
    flow_timeout_seconds: Optional[int],
) -> int:
    """Use an explicit deadline, or the selected flow timeout plus startup buffer."""
    if explicit is not None:
        if explicit < 1:
            raise CraCIError("--timeout must be a positive integer")
        return explicit
    if isinstance(flow_timeout_seconds, int) and flow_timeout_seconds > 0:
        return flow_timeout_seconds + STARTUP_BUFFER_SECONDS
    raise CraCIError(
        "CRA CI deadline is unset: pass --timeout / PRELOOP_CRA_TIMEOUT_SECONDS "
        "or ensure the selected flow reports timeout_seconds"
    )


def post_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """POST the unauthenticated webhook once. Returns ``execution_id``."""
    body = json.dumps(payload).encode("utf-8")
    status, raw, _headers = request_with_retries(
        "POST",
        webhook_url,
        body=body,
        timeout=timeout,
        opener=opener,
        max_retries=1,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
        allow_retry=False,
    )
    if status != 200:
        raise CraCIError(f"webhook returned HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CraCIError("webhook response is not JSON") from exc
    execution_id = parsed.get("execution_id") if isinstance(parsed, Mapping) else None
    if not isinstance(execution_id, str) or not execution_id or execution_id == "null":
        raise CraCIError("webhook response missing execution_id")
    return execution_id


def fetch_execution(
    api_url: str,
    token: str,
    execution_id: str,
    *,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """GET /executions/{id} once (retries allowed; idempotent)."""
    url = _join_api(api_url, f"/api/v1/flows/executions/{execution_id}")
    status, raw, _headers = request_with_retries(
        "GET",
        url,
        token=token,
        timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
    )
    if status != 200:
        raise CraCIError(f"execution fetch returned HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CraCIError("execution response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise CraCIError("execution response is not an object")
    return parsed


def fetch_flow_timeout_seconds(
    api_url: str,
    token: str,
    flow_id: str,
    *,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Optional[int]:
    """GET /flows/{id} and return ``timeout_seconds`` when present."""
    url = _join_api(api_url, f"/api/v1/flows/{flow_id}")
    status, raw, _headers = request_with_retries(
        "GET",
        url,
        token=token,
        timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
    )
    if status != 200:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("timeout_seconds")
    if isinstance(value, int) and value > 0:
        return value
    return None


def poll_execution(
    api_url: str,
    token: str,
    execution_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    overall_timeout: int,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Poll GET /executions/{id} until a terminal status or deadline."""
    start = monotonic()
    hard_deadline = deadline if deadline is not None else start + overall_timeout
    last: Optional[dict[str, Any]] = None
    while True:
        now = monotonic()
        if now >= hard_deadline:
            raise CraCIError(
                f"timed out waiting for execution {execution_id} "
                f"after {overall_timeout}s (last_status="
                f"{(last or {}).get('status')!r})"
            )
        parsed = fetch_execution(
            api_url,
            token,
            execution_id,
            request_timeout=request_timeout,
            opener=opener,
            max_retries=max_retries,
            deadline=hard_deadline,
            sleep=sleep,
            monotonic=monotonic,
        )
        last = parsed
        exec_status = str(parsed.get("status") or "")
        if exec_status in TERMINAL_STATUSES:
            return parsed
        remaining = hard_deadline - monotonic()
        sleep(min(poll_interval, max(0.0, remaining)))


def fetch_result(
    api_url: str,
    token: str,
    execution_id: str,
    *,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """GET /executions/{id}/result and return the parsed envelope."""
    url = _join_api(api_url, f"/api/v1/flows/executions/{execution_id}/result")
    status, raw, _headers = request_with_retries(
        "GET",
        url,
        token=token,
        timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
    )
    if status != 200:
        raise CraCIError(f"result fetch returned HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CraCIError("result response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise CraCIError("result response is not an object")
    envelope_id = parsed.get("execution_id")
    if envelope_id is not None and str(envelope_id) != str(execution_id):
        raise CraCIError("result execution_id does not match the polled execution")
    return parsed


def fetch_evidence_status(
    api_url: str,
    token: str,
    execution_id: str,
    *,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Optional[dict[str, Any]]:
    """GET evidence-status when the evidence worker exposes it; else None."""
    url = _join_api(api_url, f"/api/v1/flows/executions/{execution_id}/evidence-status")
    status, raw, _headers = request_with_retries(
        "GET",
        url,
        token=token,
        timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
        allow_retry=True,
    )
    if status in {404, 405}:
        return None
    if status != 200:
        raise CraCIError(f"evidence-status returned HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CraCIError("evidence-status response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise CraCIError("evidence-status response is not an object")
    return parsed


def fetch_evidence(
    api_url: str,
    token: str,
    execution_id: str,
    dest: Path,
    *,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    opener: Optional[OpenUrl] = None,
    max_retries: int = MAX_RETRIES,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    api_result: Any = None,
) -> Path:
    """GET evidence, validate gzip/tar membership, and verify receipt/digest."""
    receipt = fetch_evidence_status(
        api_url,
        token,
        execution_id,
        request_timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
    )
    url = _join_api(api_url, f"/api/v1/flows/executions/{execution_id}/evidence")
    status, raw, headers = request_with_retries(
        "GET",
        url,
        token=token,
        timeout=request_timeout,
        opener=opener,
        max_retries=max_retries,
        deadline=deadline,
        sleep=sleep,
        monotonic=monotonic,
        accept="application/gzip, application/octet-stream, */*",
        max_body=MAX_EVIDENCE_ARCHIVE_BYTES,
    )
    if status == 404:
        raise CraCIError("evidence fetch returned HTTP 404")
    if status == 410:
        raise CraCIError("evidence fetch returned HTTP 410 (expired)")
    if status == 409:
        raise CraCIError("evidence fetch returned HTTP 409 (failed)")
    if status != 200:
        raise CraCIError(f"evidence fetch returned HTTP {status}")
    try:
        accept_evidence_archive(
            raw,
            headers=headers,
            execution_id=execution_id,
            api_result=api_result,
            receipt=receipt,
        )
    except EvidencePackError as exc:
        raise CraCIError(f"evidence pack rejected: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


def _strict_true(value: Any, *, path: str) -> bool:
    if value is True:
        return True
    raise CraCIError(
        f"{path} must be boolean true for release acceptance, got {value!r} "
        f"({type(value).__name__})"
    )


def _has_findings(result_body: Mapping[str, Any]) -> bool:
    findings = result_body.get("findings")
    if isinstance(findings, list) and findings:
        return True
    vuln = result_body.get("vuln_scan")
    if isinstance(vuln, Mapping):
        nested = vuln.get("findings")
        if isinstance(nested, list) and nested:
            return True
    return False


def evaluate_release(
    result_body: Any,
    *,
    policy: ReleasePolicy,
    evidence_received: bool,
    expected_schema: Optional[str] = None,
    prompt: Optional[str] = None,
) -> tuple[bool, str, CraValidationResult]:
    """Decide whether a persisted CRA result may accept a release.

    Unknown and fail verdicts are rejected. Schema validation and a
    validated evidence receipt are always required. 005 gates only on
    ``gate.passed is True`` (not truthy 1/\"true\").
    """
    if not policy.require_schema_validation:
        raise CraCIError("release acceptance requires schema validation")
    if not evidence_received:
        return (
            False,
            "evidence pack was not received",
            validate_cra_result(
                result_body,
                expected_schema=expected_schema,
                prompt=prompt,
                require_coverage=policy.require_coverage,
            ),
        )
    validation = validate_cra_result(
        result_body,
        expected_schema=expected_schema,
        prompt=prompt,
        require_coverage=policy.require_coverage,
    )
    if validation.skipped:
        raise CraCIError(
            "result.json is not a CRA document; refusing release acceptance"
        )
    if not validation.ok:
        return False, "; ".join(validation.failures), validation
    schema_id = validation.schema_id
    if schema_id == SCHEMA_VULNSCAN_V1:
        if not isinstance(result_body, Mapping):
            return False, "vulnscan result is not an object", validation
        if result_body.get("status") != "success":
            return False, "vulnscan status is not success", validation
        gate = result_body.get("gate")
        if not isinstance(gate, Mapping):
            return False, "vulnscan gate is missing", validation
        try:
            _strict_true(gate.get("passed"), path="result.gate.passed")
        except CraCIError as exc:
            return False, str(exc), validation
        if _has_findings(result_body) and not policy.accept_pass_with_findings:
            return (
                False,
                "vulnscan findings present; pass_with_findings policy required",
                validation,
            )
        return True, "vulnscan gate.passed is true", validation
    if schema_id in {SCHEMA_SBOMAUDIT_V1, SCHEMA_RELEASEAUDIT_V1}:
        if not isinstance(result_body, Mapping):
            return False, "audit result is not an object", validation
        verdict = result_body.get("verdict")
        if not isinstance(verdict, str) or verdict not in AUDIT_VERDICTS:
            return False, f"unknown or missing verdict {verdict!r}", validation
        if verdict == "fail":
            return False, "verdict fail denies release", validation
        if verdict == "pass" and policy.accept_pass:
            return True, "verdict is pass", validation
        if verdict == "pass_with_findings" and policy.accept_pass_with_findings:
            return True, "verdict is pass_with_findings", validation
        return False, f"verdict {verdict} denied by release policy", validation
    if schema_id == SCHEMA_DUEDILIGENCE_V1:
        if not isinstance(result_body, Mapping):
            return False, "due-diligence result is not an object", validation
        decision = result_body.get("decision")
        outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
        if result_body.get("verdict") != "recorded" or outcome != "accepted":
            return (
                False,
                "due-diligence release requires verdict recorded and outcome accepted",
                validation,
            )
        return True, "due-diligence decision accepted", validation
    return False, f"unsupported schema {schema_id!r}", validation


def retain_artifacts(
    artifacts_dir: Path,
    *,
    result_body: Any,
    evidence_path: Optional[Path],
    envelope: Optional[Mapping[str, Any]] = None,
) -> None:
    """Write result.json (and copy evidence) even when the gate fails."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifacts_dir / "result.json"
    result_path.write_text(json.dumps(result_body, indent=2) + "\n", encoding="utf-8")
    if envelope is not None:
        (artifacts_dir / "result-wrap.json").write_text(
            json.dumps(envelope, indent=2) + "\n", encoding="utf-8"
        )
    if evidence_path is not None and evidence_path.is_file():
        dest = artifacts_dir / evidence_path.name
        if dest.resolve() != evidence_path.resolve():
            dest.write_bytes(evidence_path.read_bytes())


def encode_workspace_files(paths: Sequence[str]) -> list[dict[str, str]]:
    """Base64-encode existing files; fail if the encoded cap is exceeded."""
    import base64

    files: list[dict[str, str]] = []
    encoded_total = 0
    for rel in paths:
        path = Path(rel)
        if not path.is_file():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        encoded_total += len(encoded)
        files.append({"path": rel, "content_base64": encoded})
    if encoded_total > WORKSPACE_FILES_ENCODED_CAP:
        raise CraCIError(
            f"workspace_files encoded size {encoded_total} exceeds 1 MiB cap"
        )
    return files


def _retain_result_and_evidence(
    config: CraCIConfig,
    execution_id: str,
    *,
    opener: Optional[OpenUrl],
    deadline: Optional[float],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[Optional[dict[str, Any]], Any, Optional[Path]]:
    envelope: Optional[dict[str, Any]] = None
    result_body: Any = None
    evidence_path: Optional[Path] = None
    try:
        envelope = fetch_result(
            config.api_url,
            config.token,
            execution_id,
            request_timeout=config.request_timeout,
            opener=opener,
            max_retries=config.max_retries,
            deadline=deadline,
            sleep=sleep,
            monotonic=monotonic,
        )
        result_body = envelope.get("result")
    except CraCIError:
        envelope = None
        result_body = None
    try:
        evidence_path = fetch_evidence(
            config.api_url,
            config.token,
            execution_id,
            config.artifacts_dir / f"evidence-{execution_id}.tar.gz",
            request_timeout=config.request_timeout,
            opener=opener,
            max_retries=config.max_retries,
            deadline=deadline,
            sleep=sleep,
            monotonic=monotonic,
            api_result=result_body,
        )
    except CraCIError:
        evidence_path = None
    return envelope, result_body, evidence_path


def run_cra_ci(
    config: CraCIConfig,
    payload: Mapping[str, Any],
    *,
    opener: Optional[OpenUrl] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Trigger, poll, validate, gate, and retain artifacts. Returns process code."""
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    deadline = (
        started + config.overall_timeout if config.overall_timeout is not None else None
    )
    evidence_path: Optional[Path] = None
    envelope: Optional[dict[str, Any]] = None
    result_body: Any = None
    execution_id: Optional[str] = None
    try:
        execution_id = post_webhook(
            config.webhook_url,
            payload,
            timeout=config.request_timeout,
            opener=opener,
            deadline=deadline,
            sleep=sleep,
            monotonic=monotonic,
        )
        execution = fetch_execution(
            config.api_url,
            config.token,
            execution_id,
            request_timeout=config.request_timeout,
            opener=opener,
            max_retries=config.max_retries,
            deadline=deadline,
            sleep=sleep,
            monotonic=monotonic,
        )
        overall = config.overall_timeout
        if overall is None:
            flow_id = execution.get("flow_id")
            flow_timeout = None
            if isinstance(flow_id, str) and flow_id:
                flow_timeout = fetch_flow_timeout_seconds(
                    config.api_url,
                    config.token,
                    flow_id,
                    request_timeout=config.request_timeout,
                    opener=opener,
                    max_retries=config.max_retries,
                    deadline=deadline,
                    sleep=sleep,
                    monotonic=monotonic,
                )
            overall = resolve_overall_timeout(
                explicit=None, flow_timeout_seconds=flow_timeout
            )
            deadline = started + overall
        exec_status = str(execution.get("status") or "")
        if exec_status not in TERMINAL_STATUSES:
            try:
                execution = poll_execution(
                    config.api_url,
                    config.token,
                    execution_id,
                    poll_interval=config.poll_interval,
                    overall_timeout=overall,
                    request_timeout=config.request_timeout,
                    opener=opener,
                    max_retries=config.max_retries,
                    sleep=sleep,
                    monotonic=monotonic,
                    deadline=deadline,
                )
            except CraCIError:
                envelope, result_body, evidence_path = _retain_result_and_evidence(
                    config,
                    execution_id,
                    opener=opener,
                    deadline=None,
                    sleep=sleep,
                    monotonic=monotonic,
                )
                raise
            exec_status = str(execution.get("status") or "")
        envelope, result_body, evidence_path = _retain_result_and_evidence(
            config,
            execution_id,
            opener=opener,
            deadline=deadline,
            sleep=sleep,
            monotonic=monotonic,
        )
        evidence_received = evidence_path is not None and evidence_path.is_file()
        if exec_status != "SUCCEEDED":
            raise CraCIError(f"execution did not succeed: {exec_status}")
        accepted, reason, _validation = evaluate_release(
            result_body,
            policy=config.policy,
            evidence_received=evidence_received,
            expected_schema=config.expected_schema,
        )
        if not accepted:
            raise CraCIError(f"release denied: {reason}")
        return 0
    except CraCIError:
        raise
    finally:
        retain_artifacts(
            config.artifacts_dir,
            result_body=result_body
            if result_body is not None
            else {"error": "unavailable"},
            evidence_path=evidence_path,
            envelope=envelope,
        )


def _load_payload(path: Optional[str], extra_files: Sequence[str]) -> dict[str, Any]:
    if path:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise CraCIError("payload file must contain a JSON object")
        payload = loaded
    else:
        payload = {}
    if extra_files:
        files = encode_workspace_files(extra_files)
        existing = payload.get("workspace_files")
        if isinstance(existing, list):
            payload = {**payload, "workspace_files": list(existing) + files}
        else:
            payload = {**payload, "workspace_files": files}
    if "sbom" not in payload and extra_files:
        payload = {**payload, "sbom": {"paths": [extra_files[0]]}}
    return payload


def _parse_timeout(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise CraCIError("PRELOOP_CRA_TIMEOUT_SECONDS must be an integer") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m preloop.cra.ci",
        description=(
            "Trigger a CRA evidence-pack flow, poll until terminal, validate "
            "result.json, require a verified evidence pack, and fail closed "
            "on unknown or fail verdicts."
        ),
    )
    parser.add_argument(
        "--webhook-url", default=os.environ.get("PRELOOP_CRA_WEBHOOK_URL")
    )
    parser.add_argument("--api-url", default=os.environ.get("PRELOOP_URL"))
    parser.add_argument("--token", default=os.environ.get("PRELOOP_TOKEN"))
    parser.add_argument("--payload", help="JSON payload file")
    parser.add_argument(
        "--workspace-file",
        action="append",
        default=[],
        help="Workspace file to inline (repeatable). Missing files are skipped.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts",
        help="Directory that retains result.json and evidence even on failure",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_parse_timeout(os.environ.get("PRELOOP_CRA_TIMEOUT_SECONDS")),
        help=(
            "Overall deadline in seconds. Default: selected flow "
            "timeout_seconds plus 120s startup buffer. Required when the "
            "flow does not report timeout_seconds."
        ),
    )
    parser.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument(
        "--policy",
        default="pass",
        help="Release policy: pass (default, clean pack only) or pass_with_findings",
    )
    parser.add_argument("--expected-schema", default=None)
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="Validate a local result.json and evidence file; no HTTP",
    )
    parser.add_argument("--result", help="Local result.json for --gate-only")
    parser.add_argument("--evidence", help="Local evidence tarball for --gate-only")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        policy = ReleasePolicy.from_name(args.policy)
    except CraCIError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    if args.gate_only:
        if not args.result:
            parser.error("--result is required with --gate-only")
        if not args.evidence:
            parser.error("--evidence is required with --gate-only")
        body = json.loads(Path(args.result).read_text(encoding="utf-8"))
        evidence_path = Path(args.evidence)
        try:
            raw = evidence_path.read_bytes()
            accept_evidence_archive(
                raw,
                headers={},
                execution_id="gate-only",
                api_result=body.get("result", body) if isinstance(body, dict) else body,
                receipt=None,
            )
        except (OSError, EvidencePackError) as exc:
            print(f"evidence pack rejected: {exc}", file=sys.stderr)
            return 1
        result_body = (
            body.get("result", body)
            if isinstance(body, dict) and "result" in body
            else body
        )
        try:
            accepted, reason, _validation = evaluate_release(
                result_body,
                policy=policy,
                evidence_received=True,
                expected_schema=args.expected_schema,
            )
        except CraCIError as exc:
            print(exc, file=sys.stderr)
            return exc.exit_code
        artifacts = Path(args.artifacts_dir)
        retain_artifacts(
            artifacts,
            result_body=result_body,
            evidence_path=evidence_path,
            envelope=body if isinstance(body, dict) and "result" in body else None,
        )
        if not accepted:
            print(f"release denied: {reason}", file=sys.stderr)
            return 1
        print(reason)
        return 0

    if not args.webhook_url or not args.api_url or not args.token:
        parser.error("--webhook-url, --api-url, and --token are required (or env)")
    parsed = urlparse(args.api_url)
    if parsed.scheme not in {"http", "https"}:
        parser.error("--api-url must be an http(s) origin")
    try:
        payload = _load_payload(args.payload, args.workspace_file)
        config = CraCIConfig(
            api_url=args.api_url.rstrip("/"),
            token=args.token,
            webhook_url=args.webhook_url,
            request_timeout=args.request_timeout,
            poll_interval=args.poll_interval,
            overall_timeout=args.timeout,
            artifacts_dir=Path(args.artifacts_dir),
            policy=policy,
            expected_schema=args.expected_schema,
        )
        return run_cra_ci(config, payload)
    except CraCIError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
