"""Persist-boundary wrapping and fail-closed decisions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from preloop.cra.persist import (
    CraAuthorityUnavailableError,
    apply_cra_persist_boundary,
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
