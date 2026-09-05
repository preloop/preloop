"""Tests for content-hash based preset linking.

Covers the propagation gap where a flow cloned from a preset and then RENAMED
(but never edited) has source_preset_id=NULL and is missed by the name-based
"Copy of <preset>" linking pass, so preset updates silently never reach it.
"""

from uuid import uuid4

from preloop.models.models.flow import Flow
from preloop.services.flow_presets_service import (
    compute_content_hash,
    link_unlinked_flows_by_content,
    sync_preset_to_derived_flows,
)

PRESET_PROMPT_V1 = "You are a PR reviewer. Review the diff carefully. v1"
PRESET_PROMPT_V2 = "You are a PR reviewer. Review the diff carefully. v2"
PRESET_TOOLS = [{"name": "get_pull_request"}, {"name": "update_comment"}]


def _make_preset(db, name="Pull Request Reviewer", prompt=PRESET_PROMPT_V1):
    preset = Flow(
        id=uuid4(),
        name=name,
        account_id=None,
        is_preset=True,
        is_enabled=False,
        prompt_template=prompt,
        allowed_mcp_tools=PRESET_TOOLS,
        agent_config={},
    )
    db.add(preset)
    db.flush()
    return preset


def _make_unlinked_flow(db, account_id, name, prompt, tools=None):
    flow = Flow(
        id=uuid4(),
        name=name,
        account_id=account_id,
        is_preset=False,
        is_enabled=True,
        prompt_template=prompt,
        allowed_mcp_tools=tools if tools is not None else PRESET_TOOLS,
        agent_config={},
        source_preset_id=None,
        source_prompt_hash=None,
        source_tools_hash=None,
        prompt_customized=False,
        tools_customized=False,
    )
    db.add(flow)
    db.flush()
    return flow


class TestLinkUnlinkedFlowsByContent:
    def test_renamed_identical_flow_gets_linked_and_auto_updated(
        self, db_session, test_user
    ):
        """A renamed, byte-identical clone is linked by content hash and then
        auto-updated when the preset changes (the reported production gap)."""
        preset = _make_preset(db_session, prompt=PRESET_PROMPT_V1)
        flow = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="My Team's Reviewer",  # renamed: name-based linking misses it
            prompt=PRESET_PROMPT_V1,
        )

        linked = link_unlinked_flows_by_content(db_session)

        assert linked == 1
        db_session.refresh(flow)
        assert flow.source_preset_id == preset.id
        assert flow.source_prompt_hash == compute_content_hash(PRESET_PROMPT_V1)
        assert flow.source_tools_hash == compute_content_hash(PRESET_TOOLS)
        assert flow.prompt_customized is False
        assert flow.tools_customized is False
        assert flow.preset_update_available is False

        # Now the preset is updated -> the flow must receive the new prompt.
        preset.prompt_template = PRESET_PROMPT_V2
        db_session.flush()

        result = sync_preset_to_derived_flows(db_session, preset.id)

        assert result.auto_updated == 1
        db_session.refresh(flow)
        assert flow.prompt_template == PRESET_PROMPT_V2
        assert flow.source_prompt_hash == compute_content_hash(PRESET_PROMPT_V2)

    def test_customized_flow_is_not_linked(self, db_session, test_user):
        """A genuinely customized (non-identical) unlinked flow is never
        force-linked, so its prompt can never be overwritten by preset sync."""
        preset = _make_preset(db_session, prompt=PRESET_PROMPT_V1)
        customized_prompt = PRESET_PROMPT_V1 + "\nAlways answer in French."
        flow = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="My Team's Reviewer",
            prompt=customized_prompt,
        )

        linked = link_unlinked_flows_by_content(db_session)

        assert linked == 0
        db_session.refresh(flow)
        assert flow.source_preset_id is None
        assert flow.source_prompt_hash is None
        assert flow.prompt_customized is False  # untouched

        # Preset update must not touch the unlinked flow either.
        preset.prompt_template = PRESET_PROMPT_V2
        db_session.flush()
        sync_preset_to_derived_flows(db_session, preset.id)
        db_session.refresh(flow)
        assert flow.prompt_template == customized_prompt

    def test_flow_matching_older_preset_version_is_linked_then_updated(
        self, db_session, test_user
    ):
        """A pristine clone of an OLDER preset version (recovered via the
        link-time hashes of already-linked flows) is linked at the old hash,
        which makes the subsequent sync auto-update it."""
        preset = _make_preset(db_session, prompt=PRESET_PROMPT_V2)
        old_hash = compute_content_hash(PRESET_PROMPT_V1)

        # An already-linked flow that recorded the old preset version's hash.
        linked_old = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="Copy of Pull Request Reviewer",
            prompt=PRESET_PROMPT_V1,
        )
        linked_old.source_preset_id = preset.id
        linked_old.source_prompt_hash = old_hash
        linked_old.source_tools_hash = compute_content_hash(PRESET_TOOLS)
        db_session.flush()

        # A renamed unlinked clone of the same OLD version.
        stale_clone = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="Renamed Old Clone",
            prompt=PRESET_PROMPT_V1,
        )

        linked = link_unlinked_flows_by_content(db_session)

        assert linked == 1
        db_session.refresh(stale_clone)
        assert stale_clone.source_preset_id == preset.id
        assert stale_clone.source_prompt_hash == old_hash
        assert stale_clone.prompt_customized is False

        result = sync_preset_to_derived_flows(db_session, preset.id)
        assert result.auto_updated == 2  # both stale flows catch up
        db_session.refresh(stale_clone)
        assert stale_clone.prompt_template == PRESET_PROMPT_V2

    def test_ambiguous_hash_across_presets_is_skipped(self, db_session, test_user):
        """If two presets share the same prompt content, no link is made."""
        _make_preset(db_session, name="Preset A", prompt=PRESET_PROMPT_V1)
        _make_preset(db_session, name="Preset B", prompt=PRESET_PROMPT_V1)
        flow = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="Which preset am I?",
            prompt=PRESET_PROMPT_V1,
        )

        linked = link_unlinked_flows_by_content(db_session)

        assert linked == 0
        db_session.refresh(flow)
        assert flow.source_preset_id is None

    def test_customized_tools_are_notified_not_overwritten(self, db_session, test_user):
        """Identical prompt but different tools: link, mark tools_customized,
        set the update notification, and never overwrite the tools."""
        preset = _make_preset(db_session, prompt=PRESET_PROMPT_V1)
        custom_tools = [{"name": "get_pull_request"}]
        flow = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="Reviewer With Fewer Tools",
            prompt=PRESET_PROMPT_V1,
            tools=custom_tools,
        )

        linked = link_unlinked_flows_by_content(db_session)

        assert linked == 1
        db_session.refresh(flow)
        assert flow.source_preset_id == preset.id
        assert flow.tools_customized is True
        assert flow.preset_update_available is True
        assert flow.allowed_mcp_tools == custom_tools

        preset.prompt_template = PRESET_PROMPT_V2
        db_session.flush()
        sync_preset_to_derived_flows(db_session, preset.id)
        db_session.refresh(flow)
        # Prompt (not customized) auto-updates; tools (customized) survive.
        assert flow.prompt_template == PRESET_PROMPT_V2
        assert flow.allowed_mcp_tools == custom_tools

    def test_dry_run_makes_no_changes(self, db_session, test_user):
        _make_preset(db_session, prompt=PRESET_PROMPT_V1)
        flow = _make_unlinked_flow(
            db_session,
            test_user.account_id,
            name="Renamed",
            prompt=PRESET_PROMPT_V1,
        )

        linked = link_unlinked_flows_by_content(db_session, dry_run=True)

        assert linked == 1
        db_session.refresh(flow)
        assert flow.source_preset_id is None
