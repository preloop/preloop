"""Settings parsing for the no-progress guard (#851).

Every branch here decides whether a live run gets stopped or a second run
gets paid for, so the defaults are asserted as hard as the enabled paths: a
flow that says nothing must get nothing.
"""

from preloop.services.no_progress_guard import (
    DEFAULT_GRACE_SECONDS,
    MIN_AFTER_SECONDS,
    MIN_GRACE_SECONDS,
    parse_guard_config,
    parse_retry_config,
)


class TestParseGuardConfig:
    """``no_progress_after_seconds`` / ``no_progress_grace_seconds``."""

    def test_absent_config_disables_the_guard(self):
        assert parse_guard_config(None) is None
        assert parse_guard_config({}) is None
        assert parse_guard_config({"max_iterations": 10}) is None

    def test_explicit_null_disables_the_guard(self):
        assert parse_guard_config({"no_progress_after_seconds": None}) is None

    def test_deadline_alone_takes_the_default_grace(self):
        config = parse_guard_config({"no_progress_after_seconds": 900})
        assert config is not None
        assert config.after_seconds == 900
        assert config.grace_seconds == DEFAULT_GRACE_SECONDS

    def test_both_deadlines_are_read(self):
        config = parse_guard_config(
            {"no_progress_after_seconds": 1200, "no_progress_grace_seconds": 300}
        )
        assert (config.after_seconds, config.grace_seconds) == (1200, 300)

    def test_tiny_values_are_raised_to_the_floor(self):
        # A guard that fires during the clone would stop every run.
        config = parse_guard_config(
            {"no_progress_after_seconds": 5, "no_progress_grace_seconds": 1}
        )
        assert config.after_seconds == MIN_AFTER_SECONDS
        assert config.grace_seconds == MIN_GRACE_SECONDS

    def test_unusable_values_disable_rather_than_guess(self):
        for value in ("soon", 0, -60, True, [900]):
            assert parse_guard_config({"no_progress_after_seconds": value}) is None

    def test_unusable_grace_falls_back_to_the_default(self):
        config = parse_guard_config(
            {"no_progress_after_seconds": 900, "no_progress_grace_seconds": "later"}
        )
        assert config.grace_seconds == DEFAULT_GRACE_SECONDS


class TestParseRetryConfig:
    """``retry_on_no_progress``, which spends money when it says yes."""

    def test_default_is_off(self):
        for value in (None, {}, {"max_iterations": 10}):
            assert parse_retry_config(value).enabled is False

    def test_enabled_false_is_off(self):
        config = parse_retry_config({"retry_on_no_progress": {"enabled": False}})
        assert config.enabled is False

    def test_bare_true_is_not_an_opt_in(self):
        # One unambiguous spelling, because the cost is a whole second run.
        assert parse_retry_config({"retry_on_no_progress": True}).enabled is False

    def test_enabled_without_escalation_keeps_the_flow_model(self):
        config = parse_retry_config({"retry_on_no_progress": {"enabled": True}})
        assert config.enabled is True
        assert config.ai_model_id is None
        assert config.reasoning_effort is None

    def test_escalation_fields_are_read(self):
        config = parse_retry_config(
            {
                "retry_on_no_progress": {
                    "enabled": True,
                    "ai_model_id": "11111111-1111-4111-8111-111111111111",
                    "reasoning_effort": "High",
                }
            }
        )
        assert config.ai_model_id == "11111111-1111-4111-8111-111111111111"
        assert config.reasoning_effort == "high"

    def test_unknown_effort_is_dropped_not_forwarded(self):
        config = parse_retry_config(
            {"retry_on_no_progress": {"enabled": True, "reasoning_effort": "maximum"}}
        )
        assert config.enabled is True
        assert config.reasoning_effort is None

    def test_blank_model_id_is_no_model_id(self):
        config = parse_retry_config(
            {"retry_on_no_progress": {"enabled": True, "ai_model_id": "   "}}
        )
        assert config.ai_model_id is None
