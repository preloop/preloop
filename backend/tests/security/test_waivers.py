"""Tests for deterministic waiver application (preloop.security.waivers).

Waivers reconfigure the gate the human owns and nothing else: an unwaived
failure keeps the gate failed, invalid or unmatched entries never count,
and interactive entries without a platform approval id waive nothing.
"""

import pytest

from preloop.security.waivers import (
    WaiverValidationError,
    apply_waivers,
    assert_waived_gate,
    validate_waived_gate,
    validate_waiver_entries,
)


def _waiver(**overrides):
    entry = {
        "id": "CVE-2023-38545",
        "reason": "Feature not compiled into the shipped image.",
        "author": "release-manager@example.test",
        "date": "2026-08-24",
    }
    entry.update(overrides)
    return entry


class TestValidateWaiverEntries:
    def test_complete_entry_is_valid(self):
        valid, failures = validate_waiver_entries([_waiver()])
        assert len(valid) == 1
        assert failures == []

    @pytest.mark.parametrize("missing", ["id", "reason", "author", "date"])
    def test_missing_required_field_rejected(self, missing):
        valid, failures = validate_waiver_entries([_waiver(**{missing: ""})])
        assert valid == []
        assert len(failures) == 1
        assert missing in failures[0]

    def test_non_object_entry_rejected(self):
        valid, failures = validate_waiver_entries(["CVE-2023-38545"])
        assert valid == []
        assert "not an object" in failures[0]

    def test_duplicate_ids_rejected(self):
        valid, failures = validate_waiver_entries([_waiver(), _waiver()])
        assert len(valid) == 1
        assert "duplicate id" in failures[0]

    def test_interactive_requires_approval_id(self):
        valid, failures = validate_waiver_entries([_waiver()], require_approval_id=True)
        assert valid == []
        assert "approval_id" in failures[0]

        valid, failures = validate_waiver_entries(
            [_waiver(approval_id="9f0e8a1c-approved")], require_approval_id=True
        )
        assert len(valid) == 1
        assert failures == []


class TestApplyWaivers:
    def test_no_failures_passes_without_waivers(self):
        outcome = apply_waivers([], None)
        assert outcome["gate_passed_after_waivers"] is True
        assert outcome["waivers_applied"] == []
        assert outcome["unwaived_failures"] == []

    def test_fully_waived_gate_passes(self):
        outcome = apply_waivers(["CVE-2023-38545"], [_waiver()])
        assert outcome["gate_passed_after_waivers"] is True
        assert [w["id"] for w in outcome["waivers_applied"]] == ["CVE-2023-38545"]

    def test_unwaived_failure_keeps_gate_failed(self):
        outcome = apply_waivers(["CVE-2023-38545", "CVE-2024-9681"], [_waiver()])
        assert outcome["gate_passed_after_waivers"] is False
        assert outcome["unwaived_failures"] == ["CVE-2024-9681"]

    def test_invalid_entry_waives_nothing(self):
        outcome = apply_waivers(["CVE-2023-38545"], [_waiver(reason=" ")])
        assert outcome["gate_passed_after_waivers"] is False
        assert outcome["unwaived_failures"] == ["CVE-2023-38545"]
        assert len(outcome["invalid_entries"]) == 1

    def test_unmatched_entries_are_echoed_not_applied(self):
        stale = _waiver(id="CVE-2000-0001")
        outcome = apply_waivers(["CVE-2023-38545"], [stale])
        assert outcome["gate_passed_after_waivers"] is False
        assert outcome["waivers_unmatched"] == [stale]

    def test_alias_aware_matching(self):
        """A CVE-id waiver covers the same advisory surfaced as a GHSA
        alias (staging round W2: OSV returned GHSA ids for log4shell)."""
        failing = [{"id": "GHSA-jfh8-c2jp-5v3q", "aliases": ["CVE-2021-44228"]}]
        outcome = apply_waivers(failing, [_waiver(id="CVE-2021-44228")])
        assert outcome["gate_passed_after_waivers"] is True
        assert outcome["waivers_applied"][0]["id"] == "CVE-2021-44228"

    def test_alias_miss_keeps_failure_with_display_id(self):
        failing = [{"id": "GHSA-7rjr-3q55-vv33", "aliases": ["CVE-2021-45046"]}]
        outcome = apply_waivers(failing, [_waiver(id="CVE-2021-44228")])
        assert outcome["gate_passed_after_waivers"] is False
        assert outcome["unwaived_failures"] == ["GHSA-7rjr-3q55-vv33"]

    def test_one_waiver_cannot_cover_two_failures_twice(self):
        """The same entry applies once even when two failing items alias
        to it — the second stays unwaived only if it truly has no cover."""
        failing = [
            {"id": "GHSA-jfh8-c2jp-5v3q", "aliases": ["CVE-2021-44228"]},
            {"id": "CVE-2021-44228", "aliases": []},
        ]
        outcome = apply_waivers(failing, [_waiver(id="CVE-2021-44228")])
        # Both items are the same advisory: covered, entry applied once.
        assert outcome["gate_passed_after_waivers"] is True
        assert len(outcome["waivers_applied"]) == 1

    def test_id_matching_is_case_insensitive_and_trimmed(self):
        outcome = apply_waivers(["cve-2023-38545"], [_waiver(id="  CVE-2023-38545 ")])
        assert outcome["gate_passed_after_waivers"] is True

    def test_entries_are_echoed_verbatim(self):
        entry = _waiver(approval_id="abc-123", extra_note="kept as-is")
        outcome = apply_waivers(["CVE-2023-38545"], [entry])
        assert outcome["waivers_applied"][0] == entry


class TestValidateWaivedGate:
    def test_consistent_gate_passes(self):
        gate = {"passed": True, "waivers_applied": [_waiver()]}
        assert validate_waived_gate(gate, ["CVE-2023-38545"], [_waiver()]) == []

    def test_self_upgraded_gate_fails(self):
        """The agent cannot claim a pass the waiver inputs do not support."""
        gate = {"passed": True, "waivers_applied": []}
        failures = validate_waived_gate(gate, ["CVE-2023-38545"], None)
        assert failures
        with pytest.raises(WaiverValidationError):
            assert_waived_gate(gate, ["CVE-2023-38545"], None)

    def test_invented_waiver_row_fails(self):
        """A waivers_applied row with no matching delivered entry fails."""
        gate = {"passed": True, "waivers_applied": [_waiver()]}
        failures = validate_waived_gate(gate, ["CVE-2023-38545"], [])
        assert failures

    def test_interactive_gate_requires_approval_ids(self):
        gate = {"passed": True, "waivers_applied": [_waiver()]}
        failures = validate_waived_gate(
            gate,
            ["CVE-2023-38545"],
            [_waiver()],
            require_approval_id=True,
        )
        assert failures  # entry has no approval_id -> cannot support a pass
