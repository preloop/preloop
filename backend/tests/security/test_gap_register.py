"""Freeze-the-register comparator: prior SHA+path is a floor."""

from __future__ import annotations

from preloop.security.gap_register import (
    GapRegisterValidationError,
    assert_gap_register,
    secrets_findings_count,
    validate_gap_register,
)

SHA_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _rows():
    return [
        {
            "sha": SHA_A,
            "path": "config/service.env",
            "status": "finding",
            "kind": "gitleaks",
        },
        {
            "sha": SHA_B,
            "path": "old.env",
            "status": "finding",
            "kind": "gitleaks",
        },
    ]


def _register(rows, extras=None):
    findings = [r for r in rows if r.get("status") == "finding"]
    payload = {
        "schema": "preloop.cra.releaseaudit/v1",
        "verdict": "pass_with_findings",
        "sbom_audit": {"verdict": "pass_with_findings"},
        "gap_register": {
            "ran": True,
            "repo": {
                "remote": "https://git.example.com/example.git",
                "commit": SHA_A,
            },
            "items": [
                {
                    "id": "secrets_hygiene",
                    "title": "Secrets hygiene",
                    "status": "gap",
                    "evidence": findings[0]["sha"] if findings else "",
                }
            ],
            "secrets_findings": findings,
            "secrets_findings_count": len(findings),
            "not_checkable": ["private signing ceremony not in repo"],
            "ready": False,
        },
    }
    if extras:
        payload["gap_register"].update(extras)
    return payload


class TestGapRegisterFreeze:
    def test_count_matches_sha_path_rows(self):
        rows = _rows()
        result = _register(rows)
        failures = validate_gap_register(
            result,
            repo_pin=SHA_A,
            scan_rows=rows,
        )
        assert failures == []
        assert (
            secrets_findings_count(rows)
            == result["gap_register"]["secrets_findings_count"]
        )

    def test_drop_without_resolved_fails(self):
        first = _register(_rows())
        dropped = [r for r in _rows() if r["sha"] != SHA_A]
        second = _register(dropped)
        failures = validate_gap_register(second, previous=first)
        assert any("dropped prior SHA+path" in f for f in failures)

    def test_drop_with_resolved_reason_passes(self):
        rows = _rows()
        first = _register(rows)
        dropped_row = rows[0]
        kept = [r for r in rows if r is not dropped_row]
        second = _register(
            kept,
            extras={
                "resolved": [
                    {
                        "sha": dropped_row["sha"],
                        "path": dropped_row["path"],
                        "reason": "rewritten history; blob no longer reachable",
                    }
                ]
            },
        )
        failures = validate_gap_register(second, previous=first)
        assert failures == []

    def test_unclassified_row_fails(self):
        result = _register(
            [{"sha": "abc1234", "path": "x", "status": None, "kind": "gitleaks"}]
        )
        result["gap_register"]["secrets_findings"] = [
            {"sha": "abc1234", "path": "x", "kind": "gitleaks"}
        ]
        result["gap_register"]["secrets_findings_count"] = 1
        failures = validate_gap_register(result)
        assert any("unclassified" in f for f in failures)

    def test_sbom_fail_forces_overall_fail(self):
        result = _register(_rows())
        result["sbom_audit"] = {"verdict": "fail"}
        result["verdict"] = "pass_with_findings"
        failures = validate_gap_register(result)
        assert any("sbom_audit.verdict is fail" in f for f in failures)

    def test_assert_raises(self):
        try:
            assert_gap_register(
                {"gap_register": {"items": [], "secrets_findings_count": 0}}
            )
            raise AssertionError("expected GapRegisterValidationError")
        except GapRegisterValidationError as exc:
            assert exc.failures

    def test_gitleaks_zero_count_is_not_a_wrapper_verdict(self):
        """finding_count 0 satisfies the count rule; it is not a MET oracle."""
        result = _register([])
        result["gap_register"]["secrets_findings_count"] = 0
        failures = validate_gap_register(result)
        assert failures == []
        assert secrets_findings_count([]) == 0
