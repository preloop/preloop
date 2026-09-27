"""Direct tests for the validation logic in ``preloop.schemas.gateway_usage``."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from pydantic import ValidationError

from preloop.schemas.gateway_usage import (
    GatewayTokenUsage,
    ManagedAgentRegisterRequest,
    ManagedAgentSummary,
    _agree_direction_pair,
)
from preloop.utils.agent_kind import AGENT_KIND_SHAPE_ERROR


class TestAgreeDirectionPair:
    """``_agree_direction_pair`` fills one side from the other or rejects."""

    @pytest.mark.parametrize(
        ("wire", "product", "expected"),
        [
            (0, 0, (0, 0)),
            (7, 0, (7, 7)),
            (0, 9, (9, 9)),
            (5, 5, (5, 5)),
        ],
    )
    def test_agreed_pairs(
        self, wire: int, product: int, expected: tuple[int, int]
    ) -> None:
        assert _agree_direction_pair(wire, product, "w", "p") == expected

    def test_disagreeing_pair_names_both_fields(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _agree_direction_pair(3, 4, "prompt_tokens", "input_tokens")

        assert str(excinfo.value) == (
            "prompt_tokens (3) and input_tokens (4) must agree"
        )


class TestGatewayTokenUsageValidator:
    """The model validator derives the hit ratio only when it is not given."""

    def test_hit_ratio_is_none_without_a_cache_split(self) -> None:
        assert GatewayTokenUsage(prompt_tokens=10).cache_hit_ratio is None

    def test_hit_ratio_is_derived_from_the_cache_split(self) -> None:
        usage = GatewayTokenUsage(cache_read_tokens=30, uncached_input_tokens=10)

        assert usage.cache_hit_ratio == pytest.approx(0.75)

    def test_explicit_hit_ratio_is_kept(self) -> None:
        usage = GatewayTokenUsage(
            cache_read_tokens=30, uncached_input_tokens=10, cache_hit_ratio=0.5
        )

        assert usage.cache_hit_ratio == 0.5

    @pytest.mark.parametrize("row", [None, {}])
    def test_from_row_without_traffic_is_all_zero(
        self, row: Optional[Dict[str, Any]]
    ) -> None:
        usage = GatewayTokenUsage.from_row(row)

        assert usage.prompt_tokens == usage.input_tokens == 0
        assert usage.cache_hit_ratio is None

    def test_from_row_mirrors_wire_names_and_tolerates_nulls(self) -> None:
        usage = GatewayTokenUsage.from_row(
            {
                "prompt_tokens": 12,
                "completion_tokens": None,
                "total_tokens": "12",
                "cache_read_tokens": 4,
                "uncached_input_tokens": 4,
            }
        )

        assert usage.input_tokens == 12
        assert usage.output_tokens == 0
        assert usage.total_tokens == 12
        assert usage.cache_hit_ratio == pytest.approx(0.5)


class TestManagedAgentSummaryControlSessionMode:
    """An unset control session mode reads as offline."""

    @staticmethod
    def _summary(**overrides: Any) -> ManagedAgentSummary:
        fields: Dict[str, Any] = {
            "id": "agent-1",
            "display_name": "Example agent",
            "session_source_type": "runtime",
            "session_source_id": "source-1",
            "enrolled_via": "cli",
            "last_seen_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        fields.update(overrides)
        return ManagedAgentSummary(**fields)

    @pytest.mark.parametrize("value", [None, ""])
    def test_blank_mode_becomes_offline(self, value: Optional[str]) -> None:
        assert self._summary(control_session_mode=value).control_session_mode == (
            "offline"
        )

    def test_default_is_offline(self) -> None:
        assert self._summary().control_session_mode == "offline"

    def test_set_mode_is_kept(self) -> None:
        assert self._summary(control_session_mode="live").control_session_mode == (
            "live"
        )


class TestManagedAgentRegisterRequestAgentKind:
    """``agent_kind`` is normalized and shape-checked."""

    def test_omitted_kind_stays_none(self) -> None:
        assert ManagedAgentRegisterRequest(display_name="Agent").agent_kind is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Cursor", "cursor"),
            ("Gemini CLI", "gemini_cli"),
            ("gemini-cli", "gemini_cli"),
        ],
    )
    def test_kind_is_normalized(self, raw: str, expected: str) -> None:
        request = ManagedAgentRegisterRequest(display_name="Agent", agent_kind=raw)

        assert request.agent_kind == expected

    def test_blank_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="agent_kind must not be blank"):
            ManagedAgentRegisterRequest(display_name="Agent", agent_kind="   ")

    def test_kind_with_punctuation_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ManagedAgentRegisterRequest(display_name="Agent", agent_kind="agent/1")

        assert AGENT_KIND_SHAPE_ERROR in str(excinfo.value)
