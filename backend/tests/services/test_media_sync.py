"""Recording mapping precedence, skew, gaps and schema boundaries."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from preloop.schemas.media_timing import MediaTiming
from preloop.services.media_sync import resolve_recordings

START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def activity(
    index: int,
    *,
    occurred: float | None = None,
    recorded: float = 30,
    kind: str = "browser_step",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(index),
        activity_type=kind,
        timestamp=START + timedelta(seconds=recorded),
        metadata_={
            "source_step_id": f"step-{index}",
            "occurred_at": (START + timedelta(seconds=occurred)).isoformat()
            if occurred is not None
            else None,
            "recorded_at": (START + timedelta(seconds=recorded)).isoformat(),
        },
    )


def recording(**timing: object) -> SimpleNamespace:
    return SimpleNamespace(
        id="recording",
        availability="available",
        media_timing={
            "started_at": START.isoformat(),
            "clock": "agent",
            "duration_ms": 10000,
            **timing,
        },
    )


def test_precedence_skew_and_independent_recordings() -> None:
    rows = [activity(i, occurred=i, recorded=i + 30) for i in range(3)]
    rows += [
        activity(4, recorded=34, kind="tool_call"),
        activity(5, recorded=35, kind="model_gateway_call"),
    ]
    first = recording(step_offsets=[{"source_step_id": "step-1", "offset_ms": 4500}])
    second = recording(started_at=(START + timedelta(seconds=4)).isoformat())
    result = resolve_recordings([first, second], rows).recordings
    assert result[0].skew_ms == 30000
    mapped = {m.activity_id: m for m in result[0].mappings}
    assert (mapped["1"].offset_ms, mapped["1"].rule) == (4500, "exact")
    assert (mapped["2"].offset_ms, mapped["2"].rule) == (2000, "occurred_at")
    assert (mapped["4"].offset_ms, mapped["4"].rule, mapped["4"].approximate) == (
        4000,
        "server_skew",
        False,
    )
    assert {m.activity_id: m.offset_ms for m in result[1].mappings} == {
        "4": 0,
        "5": 1000,
    }
    assert result[1].unmapped_activity_ids == ["0", "1", "2"]


def test_median_and_insufficient_samples() -> None:
    rows = [
        activity(0, occurred=0, recorded=30),
        activity(1, occurred=1, recorded=31),
        activity(2, occurred=2, recorded=500),
        activity(3, recorded=34, kind="tool_call"),
    ]
    result = resolve_recordings([recording()], rows).recordings[0]
    assert result.skew_ms == 30000
    assert result.mappings[-1].offset_ms == 4000
    result = resolve_recordings(
        [recording(duration_ms=40000)], rows[:2] + rows[3:]
    ).recordings[0]
    assert result.skew_ms == 0
    assert result.mappings[-1].offset_ms == 34000
    assert result.mappings[-1].approximate


def test_no_clamp_or_lower_trust_fallback() -> None:
    rows = [
        activity(0, occurred=1, recorded=1),
        activity(1, occurred=-1, recorded=1),
        activity(2, occurred=10),
        activity(3, occurred=11),
    ]
    result = resolve_recordings(
        [recording(step_offsets=[{"source_step_id": "step-0", "offset_ms": 11000}])],
        rows,
    ).recordings[0]
    assert [(m.activity_id, m.offset_ms) for m in result.mappings] == [("2", 10000)]
    assert result.unmapped_activity_ids == ["0", "1", "3"]


def test_server_clock_does_not_subtract_agent_skew() -> None:
    rows = [activity(i, occurred=i, recorded=i + 30) for i in range(3)]
    result = resolve_recordings(
        [
            recording(
                clock="server", started_at=(START + timedelta(seconds=30)).isoformat()
            )
        ],
        rows,
    ).recordings[0]
    assert [m.offset_ms for m in result.mappings] == [0, 1000, 2000]
    assert all(m.rule == "server_skew" and not m.approximate for m in result.mappings)


def test_server_clock_with_few_steps_is_exact() -> None:
    """No skew is estimated for a server clock, so few samples cost nothing."""
    rows = [
        activity(0, occurred=0, recorded=31),
        activity(1, recorded=33, kind="tool_call"),
    ]
    result = resolve_recordings(
        [
            recording(
                clock="server", started_at=(START + timedelta(seconds=30)).isoformat()
            )
        ],
        rows,
    ).recordings[0]
    assert [(m.offset_ms, m.approximate) for m in result.mappings] == [
        (1000, False),
        (3000, False),
    ]


def test_no_timing_or_unavailable_is_plain_player() -> None:
    rec = recording()
    rec.media_timing = None
    assert not resolve_recordings([rec], [activity(1)]).recordings[0].mappings
    rec = recording()
    rec.availability = "evicted"
    assert not resolve_recordings([rec], [activity(1)]).recordings[0].mappings


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": "2026-10-01T00:00:00"},
        {"clock": "unknown"},
        {"duration_ms": -1},
        {"duration_ms": 1.5},
        {"duration_ms": True},
        {"step_offsets": [{"source_step_id": "x", "offset_ms": -1}]},
        {"step_offsets": [{"source_step_id": "x", "offset_ms": 0}] * 2},
        {
            "step_offsets": [
                {"source_step_id": str(i), "offset_ms": 0} for i in range(5001)
            ]
        },
    ],
)
def test_invalid_timing(changes: dict) -> None:
    with pytest.raises(ValidationError):
        MediaTiming.model_validate({**recording().media_timing, **changes})
