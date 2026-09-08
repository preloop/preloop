"""Persist-boundary wrapping and fail-closed decisions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from preloop.cra.persist import (
    CraAuthorityUnavailableError,
    apply_cra_fail_closed_completion,
    apply_cra_persist_boundary,
    cra_fail_closed_completion_error,
    cra_fail_closed_error_message,
    delivered_waivers_from_trigger,
    load_platform_approvals,
    resolve_persist_authority,
)
from preloop.cra.schemas import INVALID_ERROR, MISSING_ERROR, UNSUPPORTED_ERROR
from preloop.cra.validate import AUTHORITY_REQUIRED

from .conftest import clone


def test_non_cra_json_unchanged() -> None:
    payload = {"status": "success", "summary": "ok"}
    decision = apply_cra_persist_boundary(payload)
    assert not decision.invalid
    assert decision.validation.skipped
    assert decision.artifact == payload


def test_error_list_does_not_crash() -> None:
    payload: dict[str, Any] = {"error": []}
    decision = apply_cra_persist_boundary(payload)
    assert decision.validation.skipped
    assert decision.artifact == payload


def test_error_object_does_not_crash() -> None:
    payload: dict[str, Any] = {"error": {}}
    decision = apply_cra_persist_boundary(payload)
    assert decision.validation.skipped
    assert decision.artifact == payload


def test_other_schema_error_object_unchanged() -> None:
    payload = {"schema": "other/v1", "error": {"message": "example"}}
    decision = apply_cra_persist_boundary(payload)
    assert decision.validation.skipped
    assert decision.artifact == payload


def test_malformed_known_schema_preserves_raw(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    del payload["verdict"]
    decision = apply_cra_persist_boundary(payload)
    assert decision.invalid
    assert decision.artifact is not None
    assert decision.artifact["error"] == INVALID_ERROR
    assert decision.artifact["raw"]["schema"] == payload["schema"]
    assert "verdict" in "".join(decision.validation.failures)


def test_unsupported_version_uses_unsupported_error(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    payload["schema"] = "preloop.cra.releaseaudit/v9"
    decision = apply_cra_persist_boundary(payload)
    assert decision.invalid
    assert decision.artifact is not None
    assert decision.artifact["error"] == UNSUPPORTED_ERROR


def test_expected_cra_missing_schema_cannot_evade() -> None:
    prompt = 'Required shape (preloop.cra.vulnscan/v1): { "schema": "x" }'
    decision = apply_cra_persist_boundary({"status": "success"}, prompt=prompt)
    assert decision.invalid
    assert decision.fail_closed_status == "FAILED"
    assert decision.artifact is not None
    assert decision.artifact["error"] in {INVALID_ERROR, MISSING_ERROR}


def test_expected_cra_missing_result() -> None:
    prompt = "Required shape (preloop.cra.sbomaudit/v1): {}"
    decision = apply_cra_persist_boundary(None, prompt=prompt)
    assert decision.invalid
    assert decision.artifact is not None
    assert decision.artifact["error"] == MISSING_ERROR


def test_valid_fail_verdict_is_execution_completed(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    payload["valid"] = False
    payload["minimum_elements"]["passed"] = False
    payload["verdict"] = "fail"
    decision = apply_cra_persist_boundary(payload)
    assert not decision.invalid
    assert decision.fail_closed_status is None
    assert decision.validation.execution_completed
    assert decision.validation.release_denied
    assert decision.artifact == payload


def test_trigger_waivers_key_absent_is_none() -> None:
    assert delivered_waivers_from_trigger({"release_ref": "v1"}) is None


def test_trigger_nested_payload_waivers() -> None:
    entries = delivered_waivers_from_trigger(
        {
            "payload": {
                "waivers": [{"id": "CVE-1", "reason": "x", "author": "a", "date": "d"}]
            }
        }
    )
    assert entries is not None
    assert entries[0]["id"] == "CVE-1"


def test_claimed_decision_fails_closed_when_lookup_unavailable(
    duediligence_result: dict[str, Any],
) -> None:
    decision = apply_cra_persist_boundary(
        duediligence_result,
        authority=AUTHORITY_REQUIRED,
        platform_approvals=None,
    )
    assert decision.invalid
    assert decision.fail_closed_status == "FAILED"
    assert any("unavailable" in item for item in decision.validation.failures)


def test_load_platform_approvals_raises_on_query_error(monkeypatch: Any) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        boom,
    )
    with pytest.raises(CraAuthorityUnavailableError):
        load_platform_approvals(MagicMock(), "exec-1")


def test_resolve_authority_does_not_query_non_cra(monkeypatch: Any) -> None:
    called = {"n": 0}

    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        boom,
    )
    approvals, authority = resolve_persist_authority(
        {"status": "success", "summary": "ok"}, MagicMock(), "exec-1"
    )
    assert approvals is None
    assert authority == "offline"
    assert called["n"] == 0


def test_resolve_authority_ignores_non_cra_decision(monkeypatch: Any) -> None:
    called = {"n": 0}

    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        boom,
    )
    approvals, authority = resolve_persist_authority(
        {"decision": {"outcome": "accepted"}, "status": "ok"},
        MagicMock(),
        "exec-1",
    )
    assert approvals is None
    assert authority == "offline"
    assert called["n"] == 0


def test_resolve_authority_queries_only_claimed_decisions(
    monkeypatch: Any, duediligence_result: dict[str, Any]
) -> None:
    called = {"n": 0}

    def empty(*_args: Any, **_kwargs: Any) -> list[Any]:
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        empty,
    )
    approvals, authority = resolve_persist_authority(
        duediligence_result, MagicMock(), "exec-1"
    )
    assert called["n"] == 1
    assert approvals == []
    assert authority == "required"


def test_resolve_authority_failed_lookup_is_required_none(
    monkeypatch: Any, duediligence_result: dict[str, Any]
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        boom,
    )
    approvals, authority = resolve_persist_authority(
        duediligence_result, MagicMock(), "exec-1"
    )
    assert approvals is None
    assert authority == "required"


def test_load_platform_approvals_copies_ask_user_delivery(monkeypatch: Any) -> None:
    row = MagicMock()
    row.id = "appr-1"
    row.status = "approved"
    row.tool_name = "ask_user"
    row.tool_args = {"question": "Which findings?", "options": ["CVE-2024-0001"]}
    row.tool_result = {
        "answer": '[{"id":"CVE-2024-0001","reason":"Feature not compiled."}]',
        "answered_by": "release-manager@example.com",
        "answered_at": "2026-08-20T12:00:00Z",
    }
    row.responses = [
        {
            "user_id": "release-manager@example.com",
            "decision": "approved",
            "comment": '[{"id":"CVE-2024-0001","reason":"Feature not compiled."}]',
        }
    ]
    row.approver_comment = '[{"id":"CVE-2024-0001","reason":"Feature not compiled."}]'
    row.resolved_at = "2026-08-20T12:00:00Z"
    row.decided_by_ai = False
    row.auto_approved_reason = None

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        lambda *_args, **_kwargs: [row],
    )
    loaded = load_platform_approvals(MagicMock(), "exec-1")
    assert len(loaded) == 1
    assert loaded[0].tool_name == "ask_user"
    assert loaded[0].tool_result["answered_by"] == "release-manager@example.com"
    assert loaded[0].responses is not None
    assert loaded[0].approver_comment is not None
    assert loaded[0].decided_by_ai is False
    assert loaded[0].auto_approved_reason is None


def test_load_platform_approvals_copies_nonhuman_flags(monkeypatch: Any) -> None:
    row = MagicMock()
    row.id = "appr-ai"
    row.status = "approved"
    row.tool_name = "request_approval"
    row.tool_args = {"operation": "Component risk decision: libexample@1.4.2"}
    row.tool_result = None
    row.responses = None
    row.approver_comment = None
    row.resolved_at = "2026-08-20T12:00:00Z"
    row.decided_by_ai = True
    row.auto_approved_reason = "bypass"

    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.get_multi_by_execution",
        lambda *_args, **_kwargs: [row],
    )
    loaded = load_platform_approvals(MagicMock(), "exec-1")
    assert loaded[0].decided_by_ai is True
    assert loaded[0].auto_approved_reason == "bypass"


def test_malformed_error_envelope_failures_do_not_crash(
    releaseaudit_result: dict[str, Any],
) -> None:
    payload = {
        "error": INVALID_ERROR,
        "failures": [{}],
        "raw": releaseaudit_result,
    }
    decision = apply_cra_persist_boundary(
        payload,
        prompt="Required shape (preloop.cra.releaseaudit/v1): {}",
    )
    assert decision.invalid
    assert all(isinstance(item, str) for item in decision.validation.failures)
    assert isinstance(decision.detail, str)
    assert decision.artifact is not None
    assert decision.artifact.get("raw") == releaseaudit_result
    assert decision.artifact.get("error") == INVALID_ERROR


def test_gap_register_nested_json_is_invalid_not_exception(
    releaseaudit_result: dict[str, Any],
) -> None:
    payload = clone(releaseaudit_result)
    payload["gap_register"] = {
        "items": [{"status": {}}],
        "history_rows": [42],
    }
    decision = apply_cra_persist_boundary(payload)
    assert decision.invalid
    assert decision.artifact is not None
    assert decision.artifact.get("error") == INVALID_ERROR
    assert "raw" in decision.artifact


def test_trigger_gate_override_is_authoritative(
    releaseaudit_result: dict[str, Any],
) -> None:
    payload = clone(releaseaudit_result)
    finding = {
        "id": "CVE-2026-0001",
        "pkg": "libexample",
        "version": "1.0",
        "severity": "high",
        "cvss": 8.0,
        "epss": None,
        "kev": False,
        "fix_version": None,
        "vex_status": None,
        "sources": ["osv_purl"],
        "match_kind": "database",
        "waived": False,
        "aliases": None,
    }
    payload["vuln_scan"]["findings"] = [finding]
    payload["vuln_scan"]["counts_by_severity"] = {
        "critical": 0,
        "high": 1,
        "medium": 0,
        "low": 0,
        "unknown": 0,
    }
    payload["vuln_scan"]["gate"]["passed"] = True
    payload["vuln_scan"]["gate"]["passed_before_waivers"] = True
    payload["vuln_scan"]["gate"]["policy"] = "fail on CVSS >= 99"
    payload["vuln_scan"]["gate"]["waivers_applied"] = []
    payload["verdict"] = "pass_with_findings"
    default_decision = apply_cra_persist_boundary(payload)
    assert not default_decision.invalid
    override = apply_cra_persist_boundary(
        payload, trigger_payload={"gate": {"fail_on_cvss_gte": 7.0}}
    )
    assert override.invalid


_GITHUB_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


@pytest.mark.parametrize("status", ["FAILED", "STOPPED"])
def test_invalid_cra_keeps_original_failure_and_contract_diagnostics(
    vulnscan_result: dict[str, Any], status: str
) -> None:
    payload = clone(vulnscan_result)
    del payload["gate"]
    decision = apply_cra_persist_boundary(payload)
    assert decision.invalid
    original = (
        f"container OOM while cloning https://{_GITHUB_PAT}@github.com/acme/app.git"
    )
    failed_status, error = apply_cra_fail_closed_completion(status, original, decision)
    assert failed_status == "FAILED"
    assert error is not None
    assert "container OOM" in error
    assert "failed contract validation" in error
    assert _GITHUB_PAT not in error
    assert "[REDACTED]" in error
    contract = cra_fail_closed_error_message(decision)
    assert error == cra_fail_closed_completion_error(decision, original)
    assert contract in error


@pytest.mark.parametrize(
    "runner_error",
    [
        "exec /opt/entrypoint.sh: argument list too long",
        "Failed to start agent Job: ImagePullBackOff",
    ],
)
def test_runner_failure_stands_alone_without_contract_diagnostics(
    vulnscan_result: dict[str, Any], runner_error: str
) -> None:
    """A runner that never started the container is the whole cause.

    "no result.json was persisted" is a consequence of the container not
    running. Concatenating the two sent operators to debug the preset or the
    model when the answer was in the first clause (preloop/preloop#505,
    dogfood report 4.2).
    """
    payload = clone(vulnscan_result)
    del payload["gate"]
    decision = apply_cra_persist_boundary(payload)
    assert decision.invalid

    status, error = apply_cra_fail_closed_completion("FAILED", runner_error, decision)

    assert status == "FAILED"
    assert error == runner_error
    assert "failed contract validation" not in error
    assert "cra_result_missing" not in (error or "")


def test_invalid_cra_without_original_error_is_contract_only(
    vulnscan_result: dict[str, Any],
) -> None:
    payload = clone(vulnscan_result)
    del payload["gate"]
    decision = apply_cra_persist_boundary(payload)
    status, error = apply_cra_fail_closed_completion("SUCCEEDED", None, decision)
    assert status == "FAILED"
    assert error == cra_fail_closed_error_message(decision)
