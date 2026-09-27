"""The stream idle bound and the stall reason on a timed-out run (#872).

The log lines below were captured from the pinned Codex CLI (0.153.4) with
``RUST_LOG=error,codex_core::responses_retry=warn``, against a local server
that accepts ``POST /v1/responses`` and never sends a byte
(``stream_idle_timeout_ms = 2000``, ``stream_max_retries = 2``).
"""

import pytest

from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_MODEL_STREAM_IDLE,
    derive_failure_category,
)
from preloop.services.stream_stall import (
    STALL_MESSAGE_MARKER,
    STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS,
    STREAM_IDLE_TIMEOUT_MAX_SECONDS,
    STREAM_IDLE_TIMEOUT_MIN_SECONDS,
    detect_stream_stall,
    resolve_stream_idle_timeout_seconds,
    validate_stream_idle_timeout,
)

HEADER = [
    "PRELOOP_STREAM_IDLE_TIMEOUT_SECONDS=450",
    "PRELOOP_AGENT_EXEC_START",
    "Reading prompt from stdin...",
    "OpenAI Codex v0.153.4",
    "user",
    "Review this diff. Reply with REVIEW_JSON: then FLOW_EXECUTION_SUCCESS.",
    "",
    (
        "warning: Model metadata for `deepseek/deepseek-v4-flash` not found. "
        "Defaulting to fallback metadata; this can degrade performance and "
        "cause issues."
    ),
]
WARN_1 = (
    "2026-09-27T03:34:41.594867Z  WARN codex_core::responses_retry: stream "
    "disconnected - retrying sampling request (1/2 in 187ms)... "
    "turn_id=01a0e0ed-dab7-72e0-a837-1ac22d4e8b99 retries=1 max_retries=2 "
    "sampling_error=stream disconnected before completion: idle timeout "
    "waiting for SSE"
)
WARN_2 = WARN_1.replace("(1/2 in 187ms)", "(2/2 in 364ms)").replace(
    "retries=1", "retries=2"
)
RECONNECT_1 = "ERROR: Reconnecting... 1/2"
RECONNECT_2 = "ERROR: Reconnecting... 2/2"
GAVE_UP = "ERROR: stream disconnected before completion: idle timeout waiting for SSE"


class TestDetectStreamStall:
    def test_run_killed_during_a_reconnect_is_a_stall(self):
        """The #872 shape: one idle timeout, a reconnect, then the budget."""
        stall = detect_stream_stall(HEADER + [WARN_1, RECONNECT_1])

        assert stall is not None
        assert stall.idle_reconnects == 1
        assert stall.retries_exhausted is False
        assert stall.stream_idle_timeout_seconds == 450
        assert "idle timeout waiting for SSE" in stall.last_signal

    def test_every_retry_spent_on_idle_streams(self):
        stall = detect_stream_stall(
            HEADER + [WARN_1, RECONNECT_1, WARN_2, RECONNECT_2, GAVE_UP, GAVE_UP]
        )

        assert stall is not None
        assert stall.idle_reconnects == 2
        assert stall.retries_exhausted is True

    def test_recovered_stream_is_not_a_stall(self):
        """A run that stalled, recovered and then ran long just timed out."""
        lines = HEADER + [
            WARN_1,
            RECONNECT_1,
            "codex",
            "Looking at the diff now.",
            "tokens used",
            "1532",
        ]

        assert detect_stream_stall(lines) is None

    def test_stall_after_recovery_is_counted_from_the_last_activity(self):
        lines = HEADER + [
            WARN_1,
            RECONNECT_1,
            "codex",
            "partial answer",
            WARN_2,
            RECONNECT_2,
        ]

        stall = detect_stream_stall(lines)

        assert stall is not None
        assert stall.idle_reconnects == 1

    def test_reconnect_without_a_reason_is_not_called_idle(self):
        """Codex prints the same line for 5xx and resets."""
        assert detect_stream_stall(HEADER + [RECONNECT_1]) is None

    def test_quiet_log_is_not_a_stall(self):
        assert detect_stream_stall(HEADER) is None
        assert detect_stream_stall([]) is None

    def test_terminal_line_alone_is_enough(self):
        """Older scripts did not set RUST_LOG; only the final line names it."""
        stall = detect_stream_stall([RECONNECT_1, RECONNECT_2, GAVE_UP])

        assert stall is not None
        assert stall.idle_reconnects == 0
        assert stall.retries_exhausted is True
        assert stall.stream_idle_timeout_seconds is None
        message = stall.timeout_message(900, "this flow's timeout budget")
        assert "0 times" not in message
        assert "gave up" in message

    def test_websocket_idle_is_a_stall(self):
        stall = detect_stream_stall(
            [
                "ERROR: stream disconnected before completion: idle timeout "
                "waiting for websocket"
            ]
        )

        assert stall is not None

    def test_quoted_idle_phrases_are_not_the_runs_own_stall(self):
        """Tool output or prose quoting the phrase is not a silent stream.

        A run that timed out while reading a diff of these very tests, or
        grepping an old Codex log, must stay a plain timeout.
        """
        quoted = [
            "exec",
            "gh pr diff 1015",
            '+GAVE_UP = "ERROR: stream disconnected before completion: idle '
            'timeout waiting for SSE"',
            '+    "sampling_error=stream disconnected before completion: idle '
            'timeout "',
            f"+WARN_1 = ({WARN_1!r})",
            "old.log:12: " + GAVE_UP,
            "The provider hit an idle timeout waiting for SSE earlier, so I "
            "retried the sampling request by hand.",
            "  " + "idle timeout waiting for websocket",
        ]

        assert detect_stream_stall(HEADER + quoted) is None

    def test_quoted_phrases_do_not_mask_a_real_stall(self):
        stall = detect_stream_stall(
            HEADER + ["+GAVE_UP = " + repr(GAVE_UP), WARN_1, RECONNECT_1]
        )

        assert stall is not None
        assert stall.idle_reconnects == 1
        assert stall.retries_exhausted is False

    def test_ansi_and_padding_are_ignored(self):
        assert detect_stream_stall(["\x1b[31m" + WARN_1 + "\x1b[0m"]) is not None
        stall = detect_stream_stall(
            ["\x1b[31m" + WARN_1 + "\x1b[0m", "  \x1b[1mcodex\x1b[0m  "]
        )

        assert stall is None

    def test_result_shape(self):
        stall = detect_stream_stall(HEADER + [WARN_1])

        assert stall.as_result() == {
            "reason": "model_stream_idle",
            "idle_reconnects": 1,
            "retries_exhausted": False,
            "stream_idle_timeout_seconds": 450,
            "last_signal": WARN_1,
        }


class TestStallMessage:
    def test_message_names_the_stall_and_the_knob(self):
        stall = detect_stream_stall(HEADER + [WARN_1, RECONNECT_1])

        message = stall.timeout_message(900, "this flow's timeout budget")

        assert message.startswith(
            "Execution timed out after 900 seconds (this flow's timeout budget) "
            + STALL_MESSAGE_MARKER
        )
        assert "450 seconds" in message
        assert "reconnected once" in message
        assert "agent_config.stream_idle_timeout_seconds" in message
        assert "\u2014" not in message

    def test_message_is_classified_as_a_stream_stall(self):
        stall = detect_stream_stall(HEADER + [WARN_1, WARN_2, GAVE_UP])

        message = stall.timeout_message(1800, "the default timeout budget")

        assert (
            derive_failure_category(status="FAILED", error_message=message)
            == FAILURE_CATEGORY_MODEL_STREAM_IDLE
        )


class TestResolveStreamIdleTimeout:
    def test_default_is_the_previous_wait(self):
        assert resolve_stream_idle_timeout_seconds(None) == 600
        assert resolve_stream_idle_timeout_seconds({}) == 600
        assert STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS == 600

    def test_long_budgets_keep_the_default(self):
        assert resolve_stream_idle_timeout_seconds({}, 1800) == 600
        assert resolve_stream_idle_timeout_seconds({}, 3600) == 600

    def test_bound_leaves_room_for_a_reconnect(self):
        """600s on a 900s run could never fire: the run is stopped first."""
        assert resolve_stream_idle_timeout_seconds({}, 900) == 450

    def test_flow_setting_wins(self):
        config = {"stream_idle_timeout_seconds": 60}

        assert resolve_stream_idle_timeout_seconds(config, 1800) == 60

    def test_flow_setting_stays_inside_the_budget(self):
        config = {"stream_idle_timeout_seconds": 800}

        assert resolve_stream_idle_timeout_seconds(config, 900) == 450

    def test_flow_setting_may_raise_the_wait(self):
        config = {"stream_idle_timeout_seconds": 1200}

        assert resolve_stream_idle_timeout_seconds(config, 7200) == 1200

    def test_tiny_budget_is_floored(self):
        assert (
            resolve_stream_idle_timeout_seconds({}, 60)
            == STREAM_IDLE_TIMEOUT_MIN_SECONDS
        )

    @pytest.mark.parametrize("bad", ["60", True, 5, 10**6, 60.5, [60]])
    def test_bad_stored_value_falls_back_to_the_default(self, bad):
        config = {"stream_idle_timeout_seconds": bad}

        assert resolve_stream_idle_timeout_seconds(config, 3600) == 600

    def test_non_dict_config_uses_the_default(self):
        assert resolve_stream_idle_timeout_seconds("exec", 3600) == 600

    def test_non_numeric_budget_is_ignored(self):
        assert resolve_stream_idle_timeout_seconds({}, "soon") == 600


class TestValidateStreamIdleTimeout:
    @pytest.mark.parametrize(
        "good",
        [STREAM_IDLE_TIMEOUT_MIN_SECONDS, 120, STREAM_IDLE_TIMEOUT_MAX_SECONDS, 90.0],
    )
    def test_accepts_whole_seconds_in_range(self, good):
        validate_stream_idle_timeout(good)

    @pytest.mark.parametrize(
        "bad",
        [
            STREAM_IDLE_TIMEOUT_MIN_SECONDS - 1,
            STREAM_IDLE_TIMEOUT_MAX_SECONDS + 1,
            0,
            -30,
            "120",
            True,
            60.5,
            None,
        ],
    )
    def test_rejects_anything_else(self, bad):
        with pytest.raises(ValueError, match="stream_idle_timeout_seconds"):
            validate_stream_idle_timeout(bad)
