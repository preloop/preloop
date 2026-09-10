"""The preset sync must not drop a field between create and update.

Presets 006 and 014 declare ``approval_window_seconds: 259200``. On staging
both reported ``None``, and so did a fresh clone of 006, because those preset
rows already existed and therefore only ever took the sync's update path,
whose hand-maintained ``update_data`` dict had fallen six fields behind the
create path: approval_window_seconds, timeout_seconds, runner_pool,
custom_commands, webhook_config and schedule_config.

The consequence is the failure #510 was written to remove.
``resolve_approval_window`` fell back to
``settings.approval_default_window_seconds`` (300s), so the CRA waiver in
execution e42c6086-f637-4d18-be09-2395c4d488ca would have had five minutes to
answer rather than three days, had the operator not patched the flow row by
hand first. Round 1's waiver died that way: answered in 175 seconds, 27
seconds late.

These tests pin the fix from both ends: one source of truth for the field
list, and the specific fields that were lost.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from preloop.models.models.flow import Flow
from preloop.models.schemas.flow import FlowCreate

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_flow_presets.py"


def _load_sync_module():
    """Import scripts/sync_flow_presets.py, which is not on the package path."""
    spec = importlib.util.spec_from_file_location(
        "sync_flow_presets_under_test", SYNC_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sync():
    return _load_sync_module()


# A preset definition that sets every field a preset author is allowed to set.
# Nothing here is a real preset: the point is coverage of the field list.
FULLY_LOADED_PRESET = {
    "name": "Every Field Preset",
    "description": "sets everything a preset can set",
    "icon": "shield-lock",
    "trigger_event_source": None,
    "trigger_event_types": ["push"],
    "trigger_config": {"branch": "main"},
    "webhook_config": {"webhook_secret": "s3cret-token"},
    "schedule_config": {"type": "cron", "expr": "0 3 * * *", "timezone": "UTC"},
    "prompt_template": "audit the release",
    "agent_type": "claude-code",
    "agent_config": {"model": "claude-sonnet-4-5"},
    "allowed_mcp_servers": ["preloop"],
    "allowed_mcp_tools": [{"server": "preloop", "name": "ask_user"}],
    "git_clone_config": {"enabled": False},
    "custom_commands": {"enabled": True, "commands": ["echo hi"]},
    "runner_pool": "server",
    "timeout_seconds": 7200,
    "approval_window_seconds": 259200,
    "notifications": {"on_failure": {"comment_on_trigger_issue": True}},
}


def _covers(written, declared):
    """True when the written value carries everything the preset declared.

    Nested config values come back as full model dumps with their defaults
    filled in, so equality is the wrong test for them; what matters is that
    nothing the author wrote was lost on the way to the row.
    """
    if isinstance(declared, dict):
        return isinstance(written, dict) and all(
            k in written and _covers(written[k], v) for k, v in declared.items()
        )
    return written == declared


# The fields P3 lost. Named individually so a regression says which one.
FIELDS_THE_UPDATE_PATH_DROPPED = [
    ("approval_window_seconds", 259200),
    ("timeout_seconds", 7200),
    ("runner_pool", "server"),
    ("custom_commands", {"enabled": True, "commands": ["echo hi"]}),
    ("webhook_config", {"webhook_secret": "s3cret-token"}),
    ("schedule_config", {"type": "cron", "expr": "0 3 * * *", "timezone": "UTC"}),
]


class TestOneSourceOfTruth:
    """The update path derives from the same field list as the create path."""

    def test_the_row_fields_are_the_flowcreate_fields(self, sync):
        """Add a field to FlowCreate and the update path picks it up for free.

        This is the assertion that would have caught P3 when
        approval_window_seconds was added to FlowBase: the hand-maintained
        dict could not have satisfied it.
        """
        written = set(sync.preset_row_fields(FULLY_LOADED_PRESET))
        expected = set(FlowCreate.model_fields) - set(sync.PRESET_UNMANAGED_FIELDS)
        assert written == expected

    def test_the_platform_owned_fields_are_not_written_back(self, sync):
        """Template tracking belongs to derived flows, not to the catalog."""
        written = sync.preset_row_fields(FULLY_LOADED_PRESET)
        for field in sync.PRESET_UNMANAGED_FIELDS:
            assert field not in written

    def test_every_column_written_exists_on_the_flow_model(self, sync):
        """A field the schema has and the table does not would silently vanish."""
        for field in sync.preset_row_fields(FULLY_LOADED_PRESET):
            assert hasattr(Flow, field), field


class TestFieldsSurviveAnUpdate:
    """Every field a preset can set reaches an existing preset row."""

    @pytest.mark.parametrize("field,expected", FIELDS_THE_UPDATE_PATH_DROPPED)
    def test_a_dropped_field_now_lands(self, sync, field, expected):
        assert _covers(sync.preset_row_fields(FULLY_LOADED_PRESET)[field], expected)

    def test_the_whole_definition_survives(self, sync):
        row_fields = sync.preset_row_fields(FULLY_LOADED_PRESET)
        for field, value in FULLY_LOADED_PRESET.items():
            if field in sync.PRESET_FORCED_FIELDS:
                continue
            assert _covers(row_fields[field], value), field

    def test_the_sync_owns_preset_identity(self, sync):
        """is_preset, is_enabled and trigger_event_source are not the author's."""
        armed = dict(FULLY_LOADED_PRESET, is_preset=False, is_enabled=True)
        armed["trigger_event_source"] = "github"
        row_fields = sync.preset_row_fields(armed)
        assert row_fields["is_preset"] is True
        assert row_fields["is_enabled"] is False
        assert row_fields["trigger_event_source"] is None

    def test_account_id_is_never_written(self, sync):
        """A global preset stays global even if a definition names an account."""
        claimed = dict(
            FULLY_LOADED_PRESET, account_id="11111111-1111-1111-1111-111111111111"
        )
        assert "account_id" not in sync.preset_row_fields(claimed)

    def test_an_unknown_key_is_reported_not_swallowed(self, sync, caplog):
        """Pydantic ignores extras; a preset author should not have to guess."""
        with caplog.at_level("WARNING"):
            sync.build_preset_flow_create(
                dict(FULLY_LOADED_PRESET, trigger_event_type="push")
            )
        assert "trigger_event_type" in caplog.text
        assert "FlowCreate" in caplog.text


class TestDriftDetection:
    """A stale row must be detected, not just written correctly when detected."""

    def test_a_window_only_change_is_detected(self, sync):
        """The old comparison checked 8 fields and would have said 'up to date'.

        This is the second half of P3: even with a correct update dict, a
        preset whose only change was approval_window_seconds would never have
        reached it.
        """
        row = Flow(**sync.preset_row_fields(FULLY_LOADED_PRESET))
        assert sync.preset_drift(row, sync.preset_row_fields(FULLY_LOADED_PRESET)) == []

        row.approval_window_seconds = None
        drift = sync.preset_drift(row, sync.preset_row_fields(FULLY_LOADED_PRESET))
        assert drift == ["approval_window_seconds"]

    def test_an_account_owned_preset_is_flagged(self, sync):
        row = Flow(**sync.preset_row_fields(FULLY_LOADED_PRESET))
        row.account_id = "11111111-1111-1111-1111-111111111111"
        assert "account_id (should be NULL)" in sync.preset_drift(
            row, sync.preset_row_fields(FULLY_LOADED_PRESET)
        )

    def test_applying_the_row_fields_clears_the_drift(self, sync):
        """What the sync loop does, end to end, on a stale staging row."""
        row_fields = sync.preset_row_fields(FULLY_LOADED_PRESET)
        row = Flow(**row_fields)
        row.approval_window_seconds = None
        row.timeout_seconds = None
        row.runner_pool = None

        for field, value in row_fields.items():
            setattr(row, field, value)

        assert row.approval_window_seconds == 259200
        assert row.timeout_seconds == 7200
        assert row.runner_pool == "server"
        assert sync.preset_drift(row, row_fields) == []


class TestTheShippedCatalog:
    """The real presets, since they are what staging runs."""

    def test_every_preset_validates(self, sync):
        from preloop.flow_presets import FLOW_PRESETS

        assert FLOW_PRESETS
        for preset in FLOW_PRESETS:
            created = sync.build_preset_flow_create(preset)
            assert created.is_preset is True
            assert created.is_enabled is False

    def test_the_cra_presets_keep_their_three_day_window(self, sync):
        """006 and 014 are the two that declare it, and both were losing it."""
        from preloop.flow_presets import FLOW_PRESETS

        by_name = {p["name"]: p for p in FLOW_PRESETS}
        windowed = {
            name: sync.preset_row_fields(p)["approval_window_seconds"]
            for name, p in by_name.items()
            if p.get("approval_window_seconds") is not None
        }
        assert windowed == {
            "Release Security Audit": 259200,
            "Security Maintenance Implementation": 259200,
        }

    def test_preset_timeouts_survive(self, sync):
        from preloop.flow_presets import FLOW_PRESETS

        declared = {
            p["name"]: p["timeout_seconds"]
            for p in FLOW_PRESETS
            if p.get("timeout_seconds") is not None
        }
        assert declared, "expected at least one preset to declare timeout_seconds"
        for preset in FLOW_PRESETS:
            if preset["name"] in declared:
                assert (
                    sync.preset_row_fields(preset)["timeout_seconds"]
                    == declared[preset["name"]]
                )

    def test_no_preset_declares_a_key_no_schema_claims(self, sync, caplog):
        from preloop.flow_presets import FLOW_PRESETS

        with caplog.at_level("WARNING"):
            for preset in FLOW_PRESETS:
                sync.build_preset_flow_create(preset)
        assert "FlowCreate does not define" not in caplog.text
