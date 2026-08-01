"""Unit tests for the usage-import service (issue #123).

Covers the Cursor dashboard Usage CSV parser (header matching, column_map
overrides, cost/token cell normalization, per-row skip accounting), the
event dedupe fingerprint, and the normalized-event schema validation.
No database required.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from preloop.schemas.usage_import import UsageImportEvent
from preloop.services.usage_import import (
    UsageImportError,
    event_fingerprint,
    parse_cursor_usage_csv,
)

CANONICAL_CSV = (
    "Date,Kind,Model,Max Mode,Input (w/ Cache Write),Input (w/o Cache Write),"
    "Cache Read,Output Tokens,Total Tokens,Cost\n"
    "2026-07-28T09:15:00.000+00:00,Usage-based,claude-4.5-sonnet,No,1200,300,"
    "4500,850,6850,$0.42\n"
    "2026-07-28T10:02:00.000+00:00,Included,composer,No,0,150,0,90,240,Included\n"
).encode("utf-8")


def _event(**overrides) -> UsageImportEvent:
    params = dict(
        timestamp=datetime(2026, 7, 28, 9, 15, tzinfo=timezone.utc),
        model="composer",
        total_tokens=100,
    )
    params.update(overrides)
    return UsageImportEvent(**params)


class TestParseCursorUsageCsv:
    """Parsing the Cursor dashboard Usage export."""

    def test_parses_canonical_export(self):
        """The documented header shape should parse every row."""
        result = parse_cursor_usage_csv(CANONICAL_CSV)

        assert result.parsed_rows == 2
        assert result.skipped_rows == 0
        assert len(result.events) == 2

        paid = result.events[0]
        assert paid.model == "claude-4.5-sonnet"
        # Prompt tokens = both input slices; w/ cache write also counts as
        # cache creation.
        assert paid.prompt_tokens == 1500
        assert paid.cache_creation_tokens == 1200
        assert paid.cache_read_tokens == 4500
        assert paid.completion_tokens == 850
        assert paid.total_tokens == 6850
        assert paid.cost_usd == pytest.approx(0.42)
        assert paid.kind == "Usage-based"
        assert paid.max_mode is False

        included = result.events[1]
        assert included.model == "composer"
        assert included.resolved_cost_usd() is None
        assert included.kind == "Included"
        assert included.total_tokens == 240

    def test_handles_bom_and_reordered_columns(self):
        """A UTF-8 BOM and shuffled column order must not break matching."""
        content = (
            "\ufeffModel,Cost,Date,Total Tokens\ncomposer,$1.10,2026-07-28,300\n"
        ).encode("utf-8")

        result = parse_cursor_usage_csv(content)

        assert len(result.events) == 1
        event = result.events[0]
        assert event.model == "composer"
        assert event.cost_usd == pytest.approx(1.10)
        assert event.total_tokens == 300

    def test_ignores_unknown_columns_like_user(self):
        """Team exports carry extra columns (e.g. User) that are ignored."""
        content = (
            "Date,User,Kind,Model,Total Tokens,Cost\n"
            "2026-07-28,alex@example.com,Usage-based,composer,500,$0.10\n"
        ).encode("utf-8")

        result = parse_cursor_usage_csv(content)

        assert len(result.events) == 1
        assert result.events[0].model == "composer"

    def test_cost_cell_variants(self):
        """'Included', '-', empty, and separator-laden dollar values parse."""
        content = (
            "Date,Model,Total Tokens,Cost\n"
            "2026-07-28,m1,10,Included\n"
            "2026-07-28,m2,10,-\n"
            "2026-07-28,m3,10,\n"
            '2026-07-28,m4,10,"$1,234.56"\n'
        ).encode("utf-8")

        result = parse_cursor_usage_csv(content)

        costs = [event.resolved_cost_usd() for event in result.events]
        assert costs == [None, None, None, pytest.approx(1234.56)]

    def test_skips_unusable_rows_with_reasons(self):
        """Bad dates and missing models are skipped with line-level reasons."""
        content = (
            "Date,Model,Total Tokens,Cost\n"
            "not-a-date,composer,10,$0.01\n"
            "2026-07-28,,10,$0.01\n"
            "2026-07-28,composer,10,$0.01\n"
        ).encode("utf-8")

        result = parse_cursor_usage_csv(content)

        assert result.parsed_rows == 3
        assert result.skipped_rows == 2
        assert len(result.events) == 1
        assert any("line 2" in reason for reason in result.skipped_row_reasons)
        assert any("line 3" in reason for reason in result.skipped_row_reasons)

    def test_row_without_tokens_or_cost_is_skipped(self):
        """A row carrying neither tokens nor a charge is unusable."""
        content = (
            "Date,Model,Total Tokens,Cost\n2026-07-28,composer,,Included\n"
        ).encode("utf-8")

        result = parse_cursor_usage_csv(content)

        assert result.skipped_rows == 1
        assert result.events == []

    def test_missing_required_columns_raises(self):
        """Without Date and Model columns the CSV shape is unusable."""
        content = b"Foo,Bar\n1,2\n"
        with pytest.raises(UsageImportError, match="Date"):
            parse_cursor_usage_csv(content)

    def test_empty_csv_raises(self):
        """An empty file has no header to match."""
        with pytest.raises(UsageImportError, match="empty"):
            parse_cursor_usage_csv(b"")

    def test_non_utf8_raises(self):
        """Invalid UTF-8 is rejected with a clear error."""
        with pytest.raises(UsageImportError, match="UTF-8"):
            parse_cursor_usage_csv(b"\xff\xfe\x00bad")

    def test_column_map_override(self):
        """column_map maps unrecognized headers onto logical fields."""
        content = (
            "When,Which Model,Tokens,Charged\n2026-07-28,composer,42,$0.05\n"
        ).encode("utf-8")

        result = parse_cursor_usage_csv(
            content,
            column_map={
                "date": "When",
                "model": "Which Model",
                "total_tokens": "Tokens",
                "cost": "Charged",
            },
        )

        assert len(result.events) == 1
        event = result.events[0]
        assert event.model == "composer"
        assert event.total_tokens == 42
        assert event.cost_usd == pytest.approx(0.05)

    def test_column_map_unknown_field_raises(self):
        """Unknown logical field names in column_map are rejected."""
        with pytest.raises(UsageImportError, match="Unknown column_map field"):
            parse_cursor_usage_csv(CANONICAL_CSV, column_map={"nope": "Date"})


class TestEventFingerprint:
    """The dedupe fingerprint contract."""

    def test_identical_events_share_fingerprint(self):
        """Replaying the same event must produce the same fingerprint."""
        first = event_fingerprint(_event(), source="cursor", agent_principal_id="ws-1")
        second = event_fingerprint(_event(), source="cursor", agent_principal_id="ws-1")
        assert first == second

    def test_fingerprint_changes_with_measured_quantity(self):
        """Two events differing in any measured field must both land."""
        base = event_fingerprint(_event(), source="cursor", agent_principal_id="ws-1")
        different = event_fingerprint(
            _event(total_tokens=101), source="cursor", agent_principal_id="ws-1"
        )
        assert base != different

    def test_fingerprint_scoped_by_source_and_agent(self):
        """The same event imported under another source/agent is distinct."""
        base = event_fingerprint(_event(), source="cursor", agent_principal_id="ws-1")
        other_source = event_fingerprint(
            _event(), source="windsurf", agent_principal_id="ws-1"
        )
        other_agent = event_fingerprint(
            _event(), source="cursor", agent_principal_id="ws-2"
        )
        assert base != other_source
        assert base != other_agent


class TestUsageImportEventSchema:
    """Validation rules on the normalized event schema."""

    def test_event_requires_tokens_or_cost(self):
        """An event with no measurable quantity is rejected."""
        with pytest.raises(ValidationError, match="token counts and/or"):
            UsageImportEvent(
                timestamp=datetime(2026, 7, 28, tzinfo=timezone.utc),
                model="composer",
            )

    def test_event_rejects_both_cost_fields(self):
        """charged_cents and cost_usd are mutually exclusive."""
        with pytest.raises(ValidationError, match="not both"):
            _event(charged_cents=100.0, cost_usd=1.0)

    def test_charged_cents_converts_to_usd(self):
        """charged_cents is normalized to USD."""
        event = _event(total_tokens=None, charged_cents=125.0)
        assert event.resolved_cost_usd() == pytest.approx(1.25)

    def test_cost_only_event_is_valid(self):
        """Money without token counts is an acceptable measurement."""
        event = _event(total_tokens=None, cost_usd=0.5)
        assert event.resolved_cost_usd() == pytest.approx(0.5)
