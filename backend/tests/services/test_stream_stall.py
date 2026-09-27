"""The stream idle bound and the stall reason on a timed-out run (#872)."""

import pytest

from preloop.services.stream_stall import (
    STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS,
    STREAM_IDLE_TIMEOUT_MAX_SECONDS,
    STREAM_IDLE_TIMEOUT_MIN_SECONDS,
    resolve_stream_idle_timeout_seconds,
    validate_stream_idle_timeout,
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
