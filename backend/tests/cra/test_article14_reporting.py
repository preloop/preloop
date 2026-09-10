"""CRA Article 14: the reportability judgement and the 24 h/72 h/14 d clock.

The obligation applies from 11 September 2026. These tests pin the two
things a round 2 rerun could not answer: is anything reportable, and by
when. Preloop does not file anything, so nothing here talks to a
submission endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from preloop.cra.reporting import (
    article14_deadlines,
    deadline_mismatches,
    earliest_discovery,
    format_timestamp,
    kev_finding_ids,
    parse_timestamp,
    reportable_candidates,
)
from preloop.cra.validate import validate_cra_result

from .conftest import clone
from .test_validate import (
    DEADLINES,
    DISCOVERED_AT,
    _art14_candidate,
    _reporting,
    _with_reporting,
)


def _kev(finding_id: str = "CVE-2024-0001", **overrides: Any) -> dict[str, Any]:
    finding = {
        "id": finding_id,
        "pkg": "libexample",
        "version": "1.0",
        "severity": "critical",
        "cvss": 9.8,
        "epss": None,
        "kev": True,
        "fix_version": None,
        "vex_status": None,
        "sources": ["osv_purl"],
        "match_kind": "database",
        "waived": False,
        "aliases": None,
    }
    finding.update(overrides)
    return finding


def _release(
    payload: dict[str, Any],
    findings: list[dict[str, Any]],
    reporting: Any = "auto",
) -> dict[str, Any]:
    """A release audit that failed its gate on the KEV findings it carries."""
    out = clone(payload)
    out["vuln_scan"]["findings"] = findings
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for item in findings:
        severity = str(item.get("severity"))
        if severity in counts:
            counts[severity] += 1
    out["vuln_scan"]["counts_by_severity"] = counts
    out["vuln_scan"]["art14_candidates"] = [
        item["id"] for item in findings if item.get("kev") is True
    ]
    gate = out["vuln_scan"]["gate"]
    gate["passed"] = False
    gate["passed_before_waivers"] = False
    gate["waivers_applied"] = []
    gate["unwaived_failures"] = [
        {"id": item["id"], "aliases": []} for item in findings if item.get("kev")
    ]
    out["verdict"] = "fail"
    if reporting == "auto":
        _with_reporting(out)
    elif reporting is not None:
        out["vuln_scan"]["reporting"] = reporting
    return out


class TestDeadlineArithmetic:
    """24 h, 72 h and 14 d, all measured from awareness, all in UTC."""

    def test_three_offsets_from_discovery(self) -> None:
        assert article14_deadlines("2026-08-20T12:00:00Z") == DEADLINES

    def test_offsets_are_absolute_not_chained(self) -> None:
        due = article14_deadlines("2026-09-11T00:00:00Z")
        assert due == {
            "early_warning_24h": "2026-09-12T00:00:00Z",
            "notification_72h": "2026-09-14T00:00:00Z",
            "final_report_14d": "2026-09-25T00:00:00Z",
        }

    def test_non_utc_offset_is_normalized(self) -> None:
        due = article14_deadlines("2026-08-20T14:00:00+02:00")
        assert due == DEADLINES

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        assert article14_deadlines("2026-08-20T12:00:00") == DEADLINES

    def test_dst_does_not_shift_the_clock(self) -> None:
        """The offsets are elapsed time, so a local DST change is irrelevant."""
        due = article14_deadlines("2026-10-24T23:30:00Z")
        assert due["early_warning_24h"] == "2026-10-25T23:30:00Z"
        assert due["notification_72h"] == "2026-10-27T23:30:00Z"

    def test_leap_day_is_ordinary_arithmetic(self) -> None:
        due = article14_deadlines("2028-02-28T09:00:00Z")
        assert due["early_warning_24h"] == "2028-02-29T09:00:00Z"
        assert due["final_report_14d"] == "2028-03-13T09:00:00Z"

    def test_unparseable_discovery_has_no_deadlines(self) -> None:
        assert article14_deadlines("not a timestamp") is None
        assert article14_deadlines(None) is None

    def test_datetime_input_is_accepted(self) -> None:
        start = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        assert article14_deadlines(start) == DEADLINES

    def test_format_drops_sub_second_noise(self) -> None:
        start = datetime(2026, 8, 20, 12, 0, 0, 987654, tzinfo=timezone.utc)
        assert format_timestamp(start) == "2026-08-20T12:00:00Z"

    def test_mismatch_names_the_key_and_the_expected_value(self) -> None:
        bad = dict(DEADLINES, notification_72h="2026-08-22T12:00:00Z")
        problems = deadline_mismatches(DISCOVERED_AT, bad)
        assert problems == [
            (
                "notification_72h",
                "2026-08-22T12:00:00Z",
                "2026-08-23T12:00:00Z",
            )
        ]

    def test_one_second_of_serialization_drift_is_tolerated(self) -> None:
        close = dict(DEADLINES, early_warning_24h="2026-08-21T12:00:01Z")
        assert deadline_mismatches(DISCOVERED_AT, close) == []

    def test_missing_deadlines_object_is_a_full_mismatch(self) -> None:
        assert len(deadline_mismatches(DISCOVERED_AT, None)) == 3


class TestEarliestDiscovery:
    """Awareness starts once. A re-run never restarts the clock."""

    def test_baseline_timestamp_wins_over_this_run(self) -> None:
        assert (
            earliest_discovery("2026-09-01T08:00:00Z", "2026-08-20T12:00:00Z")
            == "2026-08-20T12:00:00Z"
        )

    def test_unparseable_values_are_ignored(self) -> None:
        assert earliest_discovery(None, "", "2026-08-20T12:00:00Z") == DISCOVERED_AT

    def test_nothing_parseable_is_none(self) -> None:
        assert earliest_discovery(None, "later today") is None

    def test_offsets_compare_across_zones(self) -> None:
        assert (
            earliest_discovery("2026-08-20T12:00:00Z", "2026-08-20T13:00:00+02:00")
            == "2026-08-20T11:00:00Z"
        )


class TestReportingHelpers:
    def test_kev_ids_keep_delivery_order(self) -> None:
        findings = [_kev("CVE-2"), _kev("CVE-1", kev=False), _kev("CVE-3")]
        assert kev_finding_ids(findings) == ["CVE-2", "CVE-3"]

    def test_reportable_candidates_reads_the_flag_literally(self) -> None:
        block = _reporting(
            [
                _art14_candidate("CVE-1"),
                _art14_candidate("CVE-2", affected=False, source="vex"),
            ]
        )
        assert [item["id"] for item in reportable_candidates(block)] == ["CVE-1"]

    def test_missing_block_yields_no_candidates(self) -> None:
        assert reportable_candidates(None) == []


class TestReportingBlockRequired:
    def test_kev_finding_without_reporting_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(releaseaudit_result, [_kev()], reporting=None)
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "reporting is required when KEV-listed findings exist" in item
            for item in result.failures
        )

    def test_no_kev_findings_needs_no_block(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    def test_every_kev_finding_is_a_candidate(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev("CVE-2024-0001"), _kev("CVE-2024-0002")],
            reporting=_reporting([_art14_candidate("CVE-2024-0001")]),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "missing KEV-listed findings ['CVE-2024-0002']" in item
            for item in result.failures
        )

    def test_complete_block_validates(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(releaseaudit_result, [_kev()])
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    def test_candidates_must_be_a_list(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting([_art14_candidate("CVE-2024-0001")]),
        )
        payload["vuln_scan"]["reporting"]["candidates"] = "CVE-2024-0001"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("candidates must be a list" in item for item in result.failures)


class TestReportableDerivation:
    def test_exploited_and_affected_is_reportable(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(releaseaudit_result, [_kev()])
        candidate = payload["vuln_scan"]["reporting"]["candidates"][0]
        assert candidate["reportable"] is True
        assert payload["vuln_scan"]["reporting"]["assessment"] == "reportable_candidate"
        assert validate_cra_result(payload).ok

    def test_vex_not_affected_clears_the_report(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        """The determination Article 14 actually turns on."""
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [
                    _art14_candidate(
                        "CVE-2024-0001",
                        affected=False,
                        source="vex",
                        vex_status="not_affected",
                    )
                ]
            ),
        )
        assert payload["vuln_scan"]["reporting"]["assessment"] == (
            "no_reportable_vulnerability"
        )
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    def test_reportable_flag_cannot_contradict_its_inputs(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        candidate = _art14_candidate("CVE-2024-0001", affected=False, source="vex")
        candidate["reportable"] = True
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting([candidate], assessment="reportable_candidate"),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("reportable is True but" in item for item in result.failures)

    def test_undetermined_affected_keeps_the_assessment_open(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [
                    _art14_candidate(
                        "CVE-2024-0001", affected="undetermined", source="unknown"
                    )
                ]
            ),
        )
        assert payload["vuln_scan"]["reporting"]["assessment"] == "undetermined"
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    def test_unsourced_affected_call_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting([_art14_candidate("CVE-2024-0001", source="unknown")]),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("affected.source is 'unknown'" in item for item in result.failures)

    def test_exploitation_claim_needs_evidence(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting([_art14_candidate("CVE-2024-0001", evidence="none")]),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "claims active exploitation with exploited_evidence 'none'" in item
            for item in result.failures
        )

    def test_assessment_must_match_the_candidates(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [_art14_candidate("CVE-2024-0001")],
                assessment="no_reportable_vulnerability",
            ),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "but the candidates give 'reportable_candidate'" in item
            for item in result.failures
        )


class TestSilenceIsNotSafety:
    """The rule the snapshot asked for: unknown must not read as clear."""

    def test_failed_kev_fetch_forces_undetermined(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [_art14_candidate("CVE-2024-0001", affected=False, source="vex")],
                kev_snapshot_date=None,
                assessment="no_reportable_vulnerability",
            ),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "silence is not 'nothing to report'" in item for item in result.failures
        )

    def test_failed_kev_fetch_with_undetermined_is_accepted(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [
                    _art14_candidate(
                        "CVE-2024-0001", affected="undetermined", source="unknown"
                    )
                ],
                kev_snapshot_date=None,
                assessment="undetermined",
                basis="KEV fetch failed; exploitation status unknown",
            ),
        )
        result = validate_cra_result(payload)
        assert result.ok, result.failures


class TestCandidateShape:
    def test_deadline_must_match_the_discovery_time(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        bad = dict(DEADLINES, final_report_14d="2026-09-30T12:00:00Z")
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting([_art14_candidate("CVE-2024-0001", deadlines=bad)]),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "deadlines.final_report_14d is '2026-09-30T12:00:00Z'" in item
            for item in result.failures
        )

    def test_discovery_time_must_be_a_timestamp(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [_art14_candidate("CVE-2024-0001", discovered_at="last tuesday")]
            ),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "discovered_at must be an ISO 8601 timestamp" in item
            for item in result.failures
        )

    def test_out_of_scope_status_needs_a_reason(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [_art14_candidate("CVE-2024-0001", status="out_of_scope")]
            ),
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("status_reason is required" in item for item in result.failures)

    def test_out_of_scope_with_a_reason_is_accepted(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [
                    _art14_candidate(
                        "CVE-2024-0001",
                        status="out_of_scope",
                        status_reason="Component ships only in an internal tool.",
                    )
                ]
            ),
        )
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    @pytest.mark.parametrize(
        "field,value",
        [
            ("status", "filed"),
            ("exploited_evidence", "rumour"),
        ],
    )
    def test_enumerations_are_closed(
        self, releaseaudit_result: dict[str, Any], field: str, value: str
    ) -> None:
        candidate = _art14_candidate("CVE-2024-0001")
        candidate[field] = value
        payload = _release(
            releaseaudit_result, [_kev()], reporting=_reporting([candidate])
        )
        result = validate_cra_result(payload)
        assert not result.ok

    def test_legal_disclaimer_is_mandatory(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        block = _reporting([_art14_candidate("CVE-2024-0001")])
        block.pop("not_a_legal_determination")
        payload = _release(releaseaudit_result, [_kev()], reporting=block)
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "not_a_legal_determination must be true" in item for item in result.failures
        )


class TestIncompleteScan:
    def test_incomplete_release_cannot_claim_nothing_to_report(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _release(
            releaseaudit_result,
            [_kev()],
            reporting=_reporting(
                [_art14_candidate("CVE-2024-0001", affected=False, source="vex")],
                assessment="no_reportable_vulnerability",
            ),
        )
        payload["verdict"] = "error"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "silence is not 'nothing to report'" in item for item in result.failures
        )

    def test_vulnscan_error_status_forces_undetermined(
        self, vulnscan_result: dict[str, Any]
    ) -> None:
        payload = clone(vulnscan_result)
        payload["status"] = "error"
        payload["findings"] = [_kev()]
        payload["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["art14_candidates"] = ["CVE-2024-0001"]
        payload["gate"]["passed"] = False
        payload["gate"]["passed_before_waivers"] = False
        payload["reporting"] = _reporting(
            [_art14_candidate("CVE-2024-0001")], assessment="reportable_candidate"
        )
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "silence is not 'nothing to report'" in item for item in result.failures
        )


class TestSbomAuditReporting:
    """004 may only refuse the question, never answer it."""

    def test_undetermined_block_is_accepted(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["reporting"] = {
            "assessment": "undetermined",
            "basis": (
                "SBOM verification does not screen for vulnerabilities; "
                "run preset 005 or 006"
            ),
            "kev_snapshot_date": None,
            "kev_source_url": None,
            "candidates": [],
            "not_a_legal_determination": True,
        }
        result = validate_cra_result(payload)
        assert result.ok, result.failures

    def test_clear_verdict_is_rejected(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["reporting"] = {
            "assessment": "no_reportable_vulnerability",
            "basis": "no findings",
            "kev_snapshot_date": None,
            "kev_source_url": None,
            "candidates": [],
            "not_a_legal_determination": True,
        }
        result = validate_cra_result(payload)
        assert not result.ok
        assert any(
            "this preset does not screen for vulnerabilities" in item
            for item in result.failures
        )

    def test_candidates_cannot_be_invented(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["reporting"] = {
            "assessment": "undetermined",
            "basis": "SBOM verification does not screen for vulnerabilities",
            "kev_snapshot_date": None,
            "kev_source_url": None,
            "candidates": [_art14_candidate("CVE-2024-0001")],
            "not_a_legal_determination": True,
        }
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("candidates must be empty" in item for item in result.failures)

    def test_absent_block_is_still_valid(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        result = validate_cra_result(clone(sbomaudit_result))
        assert result.ok, result.failures


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "value",
        ["2026-08-20T12:00:00Z", "2026-08-20T12:00:00z", "2026-08-20T12:00:00+00:00"],
    )
    def test_utc_spellings_agree(self, value: str) -> None:
        assert parse_timestamp(value) == datetime(
            2026, 8, 20, 12, 0, tzinfo=timezone.utc
        )

    def test_offset_is_converted_not_dropped(self) -> None:
        parsed = parse_timestamp("2026-08-20T12:00:00-05:00")
        assert parsed == datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", [None, 17, "", "   ", "yesterday"])
    def test_junk_is_none(self, value: Any) -> None:
        assert parse_timestamp(value) is None

    def test_deadline_offsets_are_the_regulation_values(self) -> None:
        start = parse_timestamp(DISCOVERED_AT)
        assert start is not None
        due = article14_deadlines(start)
        assert due is not None
        assert parse_timestamp(due["early_warning_24h"]) - start == timedelta(hours=24)
        assert parse_timestamp(due["notification_72h"]) - start == timedelta(hours=72)
        assert parse_timestamp(due["final_report_14d"]) - start == timedelta(days=14)
