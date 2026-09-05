"""Failure category derivation.

Every message below is a real (or faithfully shaped) staging failure. The
point of the category is that an operator can COUNT failures instead of
reading a hundred error messages by hand, so the tests are written as the
classification table itself.
"""

import pytest

from preloop.agents.errors import AgentStartError
from preloop.agents.failure_analysis import analyze_agent_failure
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORIES,
    FAILURE_CATEGORY_MAX_LENGTH,
    derive_failure_category,
)

# (message observed on staging, expected category)
STAGING_MESSAGES = [
    (
        "Failed to start agent Job: (409)\nReason: Conflict\njobs.batch "
        '"agent-3f2c1b0e-1c2d-4f5a-8b9c-0d1e2f3a4b5c" already exists',
        "runner_conflict",
    ),
    (
        "Failed to start agent Job: (500)\nReason: Internal Server Error",
        "runner_error",
    ),
    (
        "Failed to start agent container: exec /opt/entrypoint.sh: argument "
        "list too long",
        "runner_error",
    ),
    (
        "stream error: Upstream provider disconnected mid-stream after 41s",
        "model_transient",
    ),
    (
        "Upstream model provider timed out (HTTP 504) after 3 attempts.",
        "model_transient",
    ),
    ("exceeded retry limit, last status: 429", "model_transient"),
    ("TypeError: terminated ... SocketError: other side closed", "model_transient"),
    (
        "AI_APICallError: Invalid authentication credentials",
        "model_auth",
    ),
    ("Upstream model provider rejected our credentials (HTTP 401).", "model_auth"),
    ("insufficient_quota: You exceeded your current quota", "provider_billing"),
    # The staging run that read "model transient: usually works on a retry".
    (
        'timestamp=2026-09-03T21:32:45Z level=error msg="model call failed" '
        'error.error="AI_APICallError: Insufficient Balance"',
        "provider_billing",
    ),
    (
        "Upstream model provider refused the call (HTTP 402 Payment Required).",
        "provider_billing",
    ),
    ("exceeded retry limit, last status: 402", "provider_billing"),
    # 402 only means "pay first" when it is written as a status. A pod name,
    # a line number or an issue number that happens to contain 402 belongs to
    # the layer that failed, not to the billing bucket.
    (
        "Failed to start agent pod preloop-run-402: evicted by the node",
        "runner_error",
    ),
    ("Execution timed out after 402 seconds", "timeout"),
    ("zai does not support parameters: ['parallel_tool_calls']", "model_config"),
    (
        "Agent exited with code 0 but did not confirm success on either "
        "channel: the FLOW_EXECUTION_SUCCESS sentinel was not printed",
        "no_confirmation",
    ),
    ("Execution timed out after 3600 seconds", "timeout"),
    ("Execution stopped by user request after 45 seconds.", "cancelled"),
    ("Traceback (most recent call last): ModuleNotFoundError: no numpy", "tool_error"),
    ("opencode command failed", "agent_error"),
]


@pytest.mark.parametrize("message,expected", STAGING_MESSAGES)
def test_staging_messages_are_classified(message, expected):
    assert derive_failure_category(status="FAILED", error_message=message) == expected


class TestVocabulary:
    def test_every_category_fits_the_column(self):
        assert all(
            len(category) <= FAILURE_CATEGORY_MAX_LENGTH
            for category in FAILURE_CATEGORIES
        )

    def test_result_is_always_from_the_vocabulary(self):
        for message, _ in STAGING_MESSAGES:
            assert (
                derive_failure_category(status="FAILED", error_message=message)
                in FAILURE_CATEGORIES
            )

    def test_unclassifiable_failure_is_unknown_not_agent_error(self):
        """A rising `unknown` share is the signal to extend the module.

        Folding the unclassified into agent_error would hide exactly that.
        """
        assert (
            derive_failure_category(status="FAILED", error_message="something odd")
            == "unknown"
        )

    def test_missing_message_still_yields_a_category(self):
        assert derive_failure_category(status="FAILED", error_message=None) == "unknown"


class TestNonFailures:
    """Only terminal failures get a category."""

    @pytest.mark.parametrize("status", ["SUCCEEDED", "RUNNING", "PENDING", "STARTING"])
    def test_no_category_for_non_failures(self, status):
        assert derive_failure_category(status=status, error_message="504") is None

    @pytest.mark.parametrize("status", ["STOPPED", "CANCELLED"])
    def test_stop_is_cancelled_whatever_the_message_says(self, status):
        # A user pressing stop mid-stream leaves a transient-looking message
        # behind; that is not a provider failure and must not be counted as one.
        assert (
            derive_failure_category(
                status=status, error_message="stream error: disconnected"
            )
            == "cancelled"
        )


class TestPrecedence:
    """The most authoritative available signal wins."""

    def test_explicit_category_beats_the_message(self):
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="stream error",
                explicit_category="runner_conflict",
            )
            == "runner_conflict"
        )

    def test_exception_category_is_used(self):
        error = AgentStartError("boom", category="runner_conflict")
        assert (
            derive_failure_category(status="FAILED", exception=error)
            == "runner_conflict"
        )

    def test_unknown_explicit_category_is_ignored(self):
        """A typo must not smuggle a value outside the vocabulary into the DB."""
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="stream error",
                explicit_category="not-a-real-category",
            )
            == "model_transient"
        )

    def test_structural_shapes_beat_the_provider_verdict(self):
        """A 409-killed run is a runner conflict even if the logs look flaky.

        This is the staging shape exactly: the 409 overwrote the original
        transient message, and the executor's analysis of the same logs still
        said "transient". Counting it as model_transient would hide the bug.
        """
        analysis = analyze_agent_failure(
            "Attempt 1 failed with status 504. Retrying..."
        )
        assert analysis.transient is True
        assert (
            derive_failure_category(
                status="FAILED",
                error_message='Failed to start agent Job: (409) jobs.batch "agent-x" '
                "already exists",
                failure_analysis=analysis,
            )
            == "runner_conflict"
        )

    def test_analysis_verdict_beats_a_lossy_message(self):
        """The executor saw the full logs; the stored message is a summary."""
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="Agent run failed",
                failure_analysis={
                    "transient": True,
                    "error_class": "upstream_overloaded",
                },
            )
            == "model_transient"
        )

    def test_402_is_billing_not_transient(self):
        """A retry cannot pay the bill, so it must not be promised one.

        This is the staging run behind C04: HTTP 402 "Insufficient Balance"
        classified as model_transient, whose console tooltip says the run
        "usually works on a retry".
        """
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="Agent run failed",
                failure_analysis={"transient": True, "upstream_status": 402},
            )
            == "provider_billing"
        )

    def test_quota_exhausted_class_is_billing(self):
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="Agent run failed",
                failure_analysis={
                    "transient": True,
                    "error_class": "upstream_quota_exhausted",
                },
            )
            == "provider_billing"
        )

    @pytest.mark.parametrize(
        "message",
        [
            "Runner pod preloop-run-402 was evicted by the node",
            "Agent exited with code 1 at line 402 of run.py: connection reset by peer",
            "Tool call failed for issue #402: not found",
        ],
    )
    def test_a_bare_402_in_the_text_is_not_a_billing_refusal(self, message):
        """Only a 402 written as a status is the provider asking for money.

        The billing rule runs before the runner and setup rules, so a digit
        match here would mislabel a pod eviction and tell the operator to top
        up an account that is fine.
        """
        assert (
            derive_failure_category(status="FAILED", error_message=message)
            != "provider_billing"
        )

    def test_analysis_status_maps_auth_before_transient(self):
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="Agent run failed",
                failure_analysis={"transient": False, "upstream_status": 401},
            )
            == "model_auth"
        )


class TestRobustness:
    """Classification must never be the reason a failure is not recorded."""

    def test_unserialised_analysis_object_is_tolerated(self):
        analysis = analyze_agent_failure(
            "Attempt 1 failed with status 504. Retrying..."
        )
        assert (
            derive_failure_category(
                status="FAILED",
                error_message="Agent run failed",
                failure_analysis=analysis,
            )
            in FAILURE_CATEGORIES
        )

    def test_plain_exception_without_category_falls_back_to_the_text(self):
        assert (
            derive_failure_category(
                status="FAILED", exception=RuntimeError("stream error: disconnected")
            )
            == "model_transient"
        )

    def test_garbage_analysis_does_not_raise(self):
        assert (
            derive_failure_category(
                status="FAILED", error_message="stream error", failure_analysis=object()
            )
            == "model_transient"
        )
