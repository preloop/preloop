"""Runner-side capture of the publication gate evidence (issue #428).

The gate verdict is runner-captured, not agent-reported: the orchestrator
scans the log stream for the verifier's marker and stores the evidence on
the execution result under ``verification``, while anything the agent wrote
itself under that key is demoted to ``verification_reported``.
"""

from unittest.mock import MagicMock


from preloop.agents.verification import (
    VERIFICATION_DENIED_MARKER,
    VERIFICATION_MARKER,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_VERIFICATION_BLOCKED,
    FAILURE_CATEGORY_VERIFICATION_FAILED,
    derive_failure_category,
)
from preloop.services.flow_orchestrator import (
    FlowExecutionOrchestrator,
    extract_verification_evidence,
    separate_agent_verification_claim,
)

ALLOWED_MARKER_JSON = (
    (
        '{"allowed":true,"commit_sha":"a" * 40,'
        '"profile_id":"default","profile_version":"v1",'
        '"reason":"all required checks passed","status":"passed",'
        '"tree_hash":"b" * 40}'
    )
    .replace('"a" * 40', '"' + "a" * 40 + '"')
    .replace('"b" * 40', '"' + "b" * 40 + '"')
)

ALLOWED_LINE = f"{VERIFICATION_MARKER} {ALLOWED_MARKER_JSON}"
DENIED_LINE = (
    f"{VERIFICATION_MARKER} "
    '{"allowed":false,"reason":"one or more required checks failed",'
    '"status":"failed"}'
)


def _orchestrator() -> FlowExecutionOrchestrator:
    return FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="flow-1",
        trigger_event_data={},
        nats_client=MagicMock(),
    )


class TestExtractVerificationEvidence:
    def test_returns_none_without_a_marker(self):
        assert extract_verification_evidence(["agent output", "more output"]) is None

    def test_parses_a_well_formed_marker(self):
        parsed = extract_verification_evidence(["noise", ALLOWED_LINE])
        assert parsed is not None
        assert parsed["allowed"] is True
        assert parsed["status"] == "passed"

    def test_last_marker_wins(self):
        lines = [ALLOWED_LINE, DENIED_LINE]
        parsed = extract_verification_evidence(lines)
        assert parsed["allowed"] is False

    def test_malformed_markers_are_skipped(self):
        lines = [f"{VERIFICATION_MARKER} not-json", ALLOWED_LINE]
        parsed = extract_verification_evidence(lines)
        assert parsed["allowed"] is True

    def test_malformed_marker_does_not_log_the_payload(self, caplog):
        import logging

        secret_line = f"{VERIFICATION_MARKER} ghp_notajsonpayloadwithsecret"
        with caplog.at_level(logging.WARNING):
            assert extract_verification_evidence([secret_line]) is None
        assert "ghp_" not in caplog.text
        assert "Ignoring malformed" in caplog.text

    def test_marker_without_allowed_flag_is_rejected(self):
        lines = [f'{VERIFICATION_MARKER} {{"status": "passed"}}']
        assert extract_verification_evidence(lines) is None

    def test_denied_line_is_still_extracted(self):
        parsed = extract_verification_evidence([DENIED_LINE])
        assert parsed["allowed"] is False
        assert parsed["status"] == "failed"


class TestAgentClaimSeparation:
    def test_agent_claim_is_demoted_to_reported(self):
        artifact = {"status": "success", "verification": {"status": "passed"}}
        separate_agent_verification_claim(artifact)
        assert "verification" not in artifact
        assert artifact["verification_reported"] == {"status": "passed"}
        assert artifact["status"] == "success"

    def test_artifact_without_a_claim_is_untouched(self):
        artifact = {"status": "success"}
        separate_agent_verification_claim(artifact)
        assert artifact == {"status": "success"}

    def test_none_artifact_is_tolerated(self):
        assert separate_agent_verification_claim(None) is None


class TestOrchestratorCapture:
    def test_streamed_marker_is_kept_and_last_one_wins(self):
        orchestrator = _orchestrator()
        orchestrator._note_verification_evidence(ALLOWED_LINE)
        orchestrator._note_verification_evidence(DENIED_LINE)
        evidence = orchestrator._resolve_verification_evidence()
        assert evidence is not None
        assert evidence["allowed"] is False

    def test_falls_back_to_accumulated_log_lines(self):
        orchestrator = _orchestrator()
        orchestrator.execution_logger.log_agent_output("noise")
        orchestrator.execution_logger.log_agent_output(ALLOWED_LINE)
        evidence = orchestrator._resolve_verification_evidence()
        assert evidence is not None
        assert evidence["allowed"] is True

    def test_no_marker_resolves_to_none(self):
        orchestrator = _orchestrator()
        orchestrator.execution_logger.log_agent_output("nothing here")
        assert orchestrator._resolve_verification_evidence() is None


class TestDeniedRunsAreNotRetried:
    """A gate denial is a verdict about the code: the run is never retried
    as if it were a transient provider failure, even when the agent phase of
    the run contained transient-looking noise."""

    def test_denial_in_error_message_is_not_transient(self):
        orchestrator = _orchestrator()
        assert (
            orchestrator._failure_is_transient(
                {
                    "failure_analysis": {"transient": True},
                    "error_message": (
                        f"{VERIFICATION_DENIED_MARKER} verdict=DENY reason=gate"
                    ),
                }
            )
            is False
        )

    def test_denial_in_recent_log_lines_is_not_transient(self):
        orchestrator = _orchestrator()
        orchestrator.execution_logger.log_agent_output(
            f"{VERIFICATION_DENIED_MARKER} verdict=DENY reason=gate"
        )
        assert (
            orchestrator._failure_is_transient(
                {"failure_analysis": {"transient": True}, "error_message": ""}
            )
            is False
        )

    def test_without_a_denial_the_analysis_verdict_stands(self):
        orchestrator = _orchestrator()
        assert (
            orchestrator._failure_is_transient(
                {"failure_analysis": {"transient": True}, "error_message": ""}
            )
            is True
        )


class TestVerificationFailureCategories:
    def test_failed_required_check_is_verification_failed(self):
        category = derive_failure_category(
            status="FAILED",
            error_message=(
                "PRELOOP_VERIFICATION_DENIED verdict=DENY reason=verification "
                "gate refused publication rc=3"
            ),
        )
        assert category == FAILURE_CATEGORY_VERIFICATION_FAILED

    def test_blocked_check_is_verification_blocked(self):
        category = derive_failure_category(
            status="FAILED",
            error_message=(
                "PRELOOP_VERIFICATION_VERDICT DENY status=blocked reason="
                "required checks unavailable, empty, timed out, or working "
                "tree changed; publication blocked"
            ),
        )
        assert category == FAILURE_CATEGORY_VERIFICATION_BLOCKED

    def test_denied_echo_before_verdict_is_still_blocked(self):
        """The publisher echoes DENIED after cat'ing the gate log; combined
        error text can list those lines in either order."""
        category = derive_failure_category(
            status="FAILED",
            error_message=(
                f"{VERIFICATION_DENIED_MARKER} verdict=DENY reason="
                "verification gate refused publication rc=3\n"
                "PRELOOP_VERIFICATION_VERDICT DENY status=blocked reason="
                "required command is unavailable"
            ),
        )
        assert category == FAILURE_CATEGORY_VERIFICATION_BLOCKED

    def test_verifier_crash_denied_line_is_blocked(self):
        category = derive_failure_category(
            status="FAILED",
            error_message=(
                f"{VERIFICATION_DENIED_MARKER} status=blocked reason=verifier crashed"
            ),
        )
        assert category == FAILURE_CATEGORY_VERIFICATION_BLOCKED

    def test_failed_loses_to_blocked_when_both_present(self):
        """The gate prints both a VERDICT line (status) and the DENIED echo;
        the environment gap wins so it is not misread as broken code."""
        category = derive_failure_category(
            status="FAILED",
            error_message=(
                "PRELOOP_VERIFICATION_VERDICT DENY status=blocked reason=check "
                "could not run\n"
                f"{VERIFICATION_DENIED_MARKER} verdict=DENY reason=gate rc=3"
            ),
        )
        assert category == FAILURE_CATEGORY_VERIFICATION_BLOCKED

    def test_successful_gate_output_is_not_a_failure_category(self):
        assert (
            derive_failure_category(
                status="SUCCEEDED",
                error_message=None,
            )
            is None
        )
