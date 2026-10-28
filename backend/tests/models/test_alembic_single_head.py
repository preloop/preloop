"""Guard: the Alembic revision graph must always have exactly one head.

This is a pure graph test. It reads the migration scripts off disk with
``alembic.script.ScriptDirectory`` and never opens a database connection, so it
runs in a plain ``pytest`` invocation with no Postgres available.

Why it exists: multiple feature branches each parented a new revision on the
same main head, then a later branch added a merge revision while another branch
turned its own DDL revision into a merge over the same parents. Both branches
passed CI in isolation; merging them produced two heads and
``alembic upgrade head`` failed with "Multiple head revisions are present",
which broke database init on main. A per-branch head count catches that at the
merge commit instead of after it lands.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

# backend/tests/models/ -> backend/preloop/models/
ALEMBIC_ROOT = Path(__file__).resolve().parents[2] / "preloop" / "models"


def _script_directory() -> ScriptDirectory:
    """Load the migration graph from disk without touching a database."""
    config = Config(str(ALEMBIC_ROOT / "alembic.ini"))
    # script_location in alembic.ini is relative to the models package.
    config.set_main_option("script_location", str(ALEMBIC_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_alembic_has_exactly_one_head() -> None:
    """Fail loudly, naming the heads, if the revision graph forked."""
    script = _script_directory()
    heads = script.get_heads()

    if len(heads) != 1:
        details = []
        for head in heads:
            revision = script.get_revision(head)
            down = revision.down_revision or "<base>"
            details.append(
                f"  - {head} ({Path(revision.path).name}, down_revision={down!r})"
            )
        raise AssertionError(
            f"Expected exactly 1 Alembic head, found {len(heads)}:\n"
            + "\n".join(details)
            + "\n\nTwo or more branches added revisions on the same parent, or "
            "two merge revisions were created for the same fork. "
            "`alembic upgrade head` cannot run in this state. Re-parent the "
            "newest revision onto the current head, or keep a single merge "
            "revision, so the graph converges again."
        )


def test_alembic_head_is_reachable_from_base() -> None:
    """Every revision must be walkable base -> head (no orphan branches)."""
    script = _script_directory()
    heads = script.get_heads()
    # A forked graph is already reported by test_alembic_has_exactly_one_head;
    # don't stack a second, more confusing failure on top of it.
    if len(heads) != 1:
        pytest.skip(f"graph has {len(heads)} heads; see the single-head test")
    head = heads[0]

    walked = {revision.revision for revision in script.walk_revisions("base", head)}
    all_revisions = {revision.revision for revision in script.walk_revisions()}

    orphans = sorted(all_revisions - walked)
    assert not orphans, (
        "Revisions are not reachable from the single head "
        f"{head!r}: {orphans}. They are detached from the migration graph."
    )


def test_flow_runners_revision_chains_onto_approval_rule_context() -> None:
    """Preserve published chains and converge on one migration head."""
    script = _script_directory()
    runners = script.get_revision("20260817_add_flow_runners")
    assert runners.down_revision == "20260806_approval_rule_ctx"
    ingest = script.get_revision("20260818_usage_ingest_conv")
    assert ingest.down_revision == "20260817_add_flow_runners"
    toggles = script.get_revision("20260818_notify_toggles")
    assert toggles.down_revision == "20260818_usage_ingest_conv"
    evidence = script.get_revision("20260821_flow_exec_evidence")
    assert evidence.down_revision == "20260818_notify_toggles"
    alias_audit = script.get_revision("20260822_alias_collision_audit")
    assert alias_audit.down_revision == "20260821_flow_exec_evidence"
    user_avatar = script.get_revision("20260830_user_avatar")
    assert user_avatar.down_revision == "20260822_alias_collision_audit"
    dismissals = script.get_revision("20260902_attention_dismiss")
    assert dismissals.down_revision == "20260830_user_avatar"
    failure_category = script.get_revision("20260903_failure_category")
    assert failure_category.down_revision == "20260902_attention_dismiss"
    flow_timeout = script.get_revision("20260903_flow_timeout")
    assert flow_timeout.down_revision == "20260903_failure_category"
    account_runner_pool = script.get_revision("20260904_acct_runner_pool")
    assert account_runner_pool.down_revision == "20260903_flow_timeout"
    workspace = script.get_revision("20260904_flow_exec_workspace")
    assert workspace.down_revision == "20260904_acct_runner_pool"
    notifications = script.get_revision("20260904_flow_notifications")
    assert notifications.down_revision == "20260904_flow_exec_workspace"
    cli_session = script.get_revision("20260905_flow_cli_session")
    assert cli_session.down_revision == "20260904_flow_notifications"
    heartbeat = script.get_revision("20260905_control_heartbeat")
    assert heartbeat.down_revision == "20260905_flow_cli_session"
    viewed_uniq = script.get_revision("20260906_ae_viewed_uniq")
    assert viewed_uniq.down_revision == "20260905_control_heartbeat"
    feedback = script.get_revision("20260906_flow_feedback")
    assert feedback.down_revision == "20260906_ae_viewed_uniq"
    halt = script.get_revision("20260906_account_halt")
    assert halt.down_revision == "20260906_flow_feedback"
    durability = script.get_revision("20260906_halt_durability")
    assert durability.down_revision == "20260906_account_halt"
    runner_caps = script.get_revision("20260906_runner_caps")
    assert runner_caps.down_revision == "20260906_flow_feedback"
    artifacts = script.get_revision("20260906_flow_artifacts")
    assert artifacts.down_revision == "20260906_flow_feedback"
    runner_merged = script.get_revision("20260906_runner_artifact_merge")
    assert set(runner_merged.down_revision) == {
        "20260906_runner_caps",
        "20260906_flow_artifacts",
    }
    halt_merged = script.get_revision("20260906_halt_artifact_merge")
    assert set(halt_merged.down_revision) == {
        "20260906_halt_durability",
        "20260906_flow_artifacts",
    }
    launch_intent = script.get_revision("20260906_halt_launch_intent")
    assert launch_intent.down_revision == "20260906_halt_artifact_merge"
    joined = script.get_revision("20260906_halt_runner_merge")
    assert set(joined.down_revision) == {
        "20260906_halt_launch_intent",
        "20260906_runner_artifact_merge",
    }
    approval_api_key = script.get_revision("20260906_approval_api_key")
    assert approval_api_key.down_revision == "20260906_halt_runner_merge"
    capabilities = script.get_revision("20260906_publication_caps")
    assert capabilities.down_revision == "20260906_flow_artifacts"
    publication = script.get_revision("20260906_pub_caps_merge")
    assert set(publication.down_revision) == {
        "20260906_approval_api_key",
        "20260906_publication_caps",
    }
    lifecycle = script.get_revision("20260906_issue_lifecycle")
    assert lifecycle.down_revision == "20260906_flow_artifacts"
    lifecycle_joined = script.get_revision("20260906_lifecycle_key_merge")
    assert set(lifecycle_joined.down_revision) == {
        "20260906_pub_caps_merge",
        "20260906_issue_lifecycle",
    }
    receipt = script.get_revision("20260907_evidence_receipt")
    assert receipt.down_revision == "20260906_lifecycle_key_merge"
    maintenance = script.get_revision("20260907_security_maintenance")
    assert maintenance.down_revision == "20260906_lifecycle_key_merge"
    joined = script.get_revision("20260907_sm_evidence_merge")
    assert set(joined.down_revision) == {
        "20260907_security_maintenance",
        "20260907_evidence_receipt",
    }
    pending_item = script.get_revision("20260907_sm_pending_item")
    assert pending_item.down_revision == "20260907_sm_evidence_merge"
    sweep_cursor = script.get_revision("20260907_sm_sweep_cursor")
    assert sweep_cursor.down_revision == "20260907_sm_pending_item"
    structured_answer = script.get_revision("20260908_structured_answer")
    assert structured_answer.down_revision == "20260907_sm_sweep_cursor"
    approval_park = script.get_revision("20260908_approval_park")
    assert approval_park.down_revision == "20260908_structured_answer"
    event_webhooks = script.get_revision("20260908_event_webhooks")
    assert event_webhooks.down_revision == "20260908_approval_park"
    delivery_key = script.get_revision("20260908_webhook_delivery_key")
    assert delivery_key.down_revision == "20260908_event_webhooks"
    retention = script.get_revision("20260910_retention_hold")
    assert retention.down_revision == "20260908_webhook_delivery_key"
    audit_chain = script.get_revision("20260910_audit_chain")
    assert audit_chain.down_revision == "20260910_retention_hold"
    operator_notes = script.get_revision("20260910_operator_notes")
    assert operator_notes.down_revision == "20260910_audit_chain"
    control_connection = script.get_revision("20260912_control_connection")
    assert control_connection.down_revision == "20260910_operator_notes"
    repricing_job = script.get_revision("20260913_repricing_job")
    assert repricing_job.down_revision == "20260912_control_connection"
    billing = script.get_revision("20260912_billing_ops")
    assert billing.down_revision == "20260910_operator_notes"
    durable = script.get_revision("20260912_history_floor")
    assert durable.down_revision == "20260912_billing_ops"
    hosted = script.get_revision("20260912_hosted_spend")
    assert hosted.down_revision == "20260912_history_floor"
    pricing_merge = script.get_revision("20260914_pricing_merge")
    assert set(pricing_merge.down_revision) == {
        "20260913_repricing_job",
        "20260912_hosted_spend",
    }
    queued_reason = script.get_revision("20260915_queued_reason")
    assert queued_reason.down_revision == "20260914_pricing_merge"
    lineage = script.get_revision("20260915_execution_lineage")
    assert lineage.down_revision == "20260914_pricing_merge"
    queued_lineage = script.get_revision("20260915_queued_lineage_merge")
    assert set(queued_lineage.down_revision) == {
        "20260915_queued_reason",
        "20260915_execution_lineage",
    }
    callable_flows = script.get_revision("20260915_flow_callable_flows")
    assert callable_flows.down_revision == "20260915_queued_lineage_merge"
    session_hold = script.get_revision("20260915_session_hold")
    assert session_hold.down_revision == "20260915_flow_callable_flows"
    note_author = script.get_revision("20260915_agent_note_author")
    assert note_author.down_revision == "20260915_session_hold"
    child_park = script.get_revision("20260915_child_park")
    assert child_park.down_revision == "20260915_agent_note_author"
    redispatch_backoff = script.get_revision("20260915_redispatch_backoff")
    assert redispatch_backoff.down_revision == "20260915_child_park"
    session_search = script.get_revision("20260915_session_search")
    assert session_search.down_revision == "20260915_redispatch_backoff"
    session_embedding = script.get_revision("20260915_session_embedding")
    assert session_embedding.down_revision == "20260915_session_search"
    session_parent = script.get_revision("20260915_session_parent")
    assert session_parent.down_revision == "20260915_session_embedding"
    session_backfill = script.get_revision("20260915_session_backfill")
    assert session_backfill.down_revision == "20260915_session_parent"
    ephemeral_runner = script.get_revision("20260916_ephemeral_runner")
    assert ephemeral_runner.down_revision == "20260915_session_backfill"
    session_saved_search = script.get_revision("20260916_session_saved_search")
    assert session_saved_search.down_revision == "20260916_ephemeral_runner"
    embedding_scope = script.get_revision("20260916_embedding_scope")
    assert embedding_scope.down_revision == "20260916_session_saved_search"
    runner_concurrency = script.get_revision("20260916_runner_concurrency")
    assert runner_concurrency.down_revision == "20260916_embedding_scope"
    trial_prompt = script.get_revision("20260916_trial_prompt")
    assert trial_prompt.down_revision == "20260916_runner_concurrency"
    onboarding_claim = script.get_revision("20260917_onboarding_claim")
    assert onboarding_claim.down_revision == "20260916_trial_prompt"
    plan_choice = script.get_revision("20260917_plan_choice")
    assert plan_choice.down_revision == "20260917_onboarding_claim"
    auth_generation = script.get_revision("20260921_auth_generation")
    assert auth_generation.down_revision == "20260917_plan_choice"
    session_artifact = script.get_revision("20260924_session_artifact")
    assert session_artifact.down_revision == "20260921_auth_generation"
    browser_step_idx = script.get_revision("20260924_browser_step_idx")
    assert browser_step_idx.down_revision == "20260924_session_artifact"
    usage_principal_ts = script.get_revision("20260924_usage_principal_ts")
    assert usage_principal_ts.down_revision == "20260924_browser_step_idx"
    exec_log_type_ts = script.get_revision("20260927_exec_log_type_ts")
    assert exec_log_type_ts.down_revision == "20260924_usage_principal_ts"
    review_instructions = script.get_revision("20260927_review_instructions")
    assert review_instructions.down_revision == "20260927_exec_log_type_ts"
    copilot_import = script.get_revision("20260927_copilot_import")
    assert copilot_import.down_revision == "20260927_review_instructions"
    spend_outliers = script.get_revision("20260927_spend_outliers")
    assert spend_outliers.down_revision == "20260927_copilot_import"
    issue_cost_rollup = script.get_revision("20260927_issue_cost_rollup")
    assert issue_cost_rollup.down_revision == "20260927_spend_outliers"
    policy_notice_hit = script.get_revision("20260927_policy_notice_hit")
    assert policy_notice_hit.down_revision == "20260927_issue_cost_rollup"
    cli_login_session = script.get_revision("20260928_cli_session")
    assert cli_login_session.down_revision == "20260927_policy_notice_hit"
    account_hierarchy = script.get_revision("20260928_account_hierarchy")
    assert account_hierarchy.down_revision == "20260928_cli_session"
    access_grants = script.get_revision("20260928_access_grants")
    assert access_grants.down_revision == "20260928_account_hierarchy"
    person_membership = script.get_revision("20260928_person_membership")
    assert person_membership.down_revision == "20260928_access_grants"
    person_backfill = script.get_revision("20260928_person_backfill")
    assert person_backfill.down_revision == "20260928_person_membership"
    person_constraints = script.get_revision("20260928_person_constraints")
    assert person_constraints.down_revision == "20260928_person_backfill"
    share_tag_rule = script.get_revision("20260928_share_tag_rule")
    assert share_tag_rule.down_revision == "20260928_person_constraints"
    issue_cost_accuracy = script.get_revision("20260927_issue_cost_accuracy")
    assert issue_cost_accuracy.down_revision == "20260928_share_tag_rule"
    artifact_kinds = script.get_revision("20261001_artifact_kinds_labels")
    assert artifact_kinds.down_revision == "20260927_issue_cost_accuracy"
    session_cwd = script.get_revision("20261002_runtime_session_cwd")
    assert session_cwd.down_revision == "20261001_artifact_kinds_labels"
    resume_root_idx = script.get_revision("20261003_resume_root_idx")
    assert resume_root_idx.down_revision == "20261002_runtime_session_cwd"
    managed_oauth = script.get_revision("20261002_managed_oauth")
    assert managed_oauth.down_revision == "20261003_resume_root_idx"
    chat_connections = script.get_revision("20261002_chat_connections")
    assert chat_connections.down_revision == "20261002_managed_oauth"
    issue_extid = script.get_revision("20261003_issue_extid_unique")
    assert issue_extid.down_revision == "20261002_chat_connections"
    artifact_created = script.get_revision("20261004_artifact_created_idx")
    assert artifact_created.down_revision == "20261003_issue_extid_unique"
    audit_lookup = script.get_revision("20261004_audit_lookup_idx")
    assert audit_lookup.down_revision == "20261004_artifact_created_idx"
    copilot_user_mapping = script.get_revision("20261004_copilot_user_mapping")
    assert copilot_user_mapping.down_revision == "20261004_audit_lookup_idx"
    ci_principal = script.get_revision("20261004_ci_principal")
    assert ci_principal.down_revision == "20261004_audit_lookup_idx"
    ci_merge = script.get_revision("20261004_ci_copilot_merge")
    assert set(ci_merge.down_revision) == {
        "20261004_ci_principal",
        "20261004_copilot_user_mapping",
    }
    discovery = script.get_revision("20261003_discovery_candidates")
    assert discovery.down_revision == "20261004_ci_copilot_merge"
    artifact_avail = script.get_revision("20261004_artifact_avail_idx")
    assert artifact_avail.down_revision == "20261003_discovery_candidates"
    ci_execution = script.get_revision("20261004_ci_execution_binding")
    assert ci_execution.down_revision == "20261003_discovery_candidates"
    ci_execution_merge = script.get_revision("20261004_ci_exec_artifact_merge")
    assert set(ci_execution_merge.down_revision) == {
        "20261004_ci_execution_binding",
        "20261004_artifact_avail_idx",
    }
    ci_subscription = script.get_revision("20261004_ci_subscription_binding")
    assert ci_subscription is not None
    assert ci_subscription.down_revision == "20261004_ci_exec_artifact_merge"
    receipt = script.get_revision("20261009_oauth_receipt_anchor")
    assert receipt.down_revision == "20261004_ci_subscription_binding"
    collisions = script.get_revision("20261009_mcp_tool_shadow_prefix")
    assert collisions is not None
    assert collisions.down_revision == "20261009_oauth_receipt_anchor"
    access = script.get_revision("20261009_access_rule_generation")
    assert access.down_revision == "20261009_mcp_tool_shadow_prefix"
    gateway_subject = script.get_revision("20261009_gateway_subject")
    assert gateway_subject.down_revision == "20261009_access_rule_generation"
    grant = script.get_revision("20261009_grant_consent_index")
    assert grant.down_revision == "20261009_gateway_subject"
    observation = script.get_revision("20261009_discovery_observation")
    assert observation.down_revision == "20261009_grant_consent_index"
    sharing = script.get_revision("20261009_resource_sharing_intent")
    assert sharing.down_revision == "20261009_discovery_observation"
    callback = script.get_revision("20261010_callback_receipt")
    assert callback.down_revision == "20261009_resource_sharing_intent"
    readiness = script.get_revision("20261010_ticket_readiness")
    assert readiness is not None
    assert readiness.down_revision == "20261010_callback_receipt"
    gateway_idp = script.get_revision("20261010_gateway_idp")
    assert gateway_idp.down_revision == "20261010_ticket_readiness"
    anthropic_import = script.get_revision("20261010_anthropic_import")
    assert anthropic_import.down_revision == "20261010_gateway_idp"
    otlp_ingest = script.get_revision("20261010_otlp_telemetry_ingest")
    assert otlp_ingest.down_revision == "20261010_anthropic_import"
    harness_inventory = script.get_revision("20261011_harness_inventory")
    assert harness_inventory.down_revision == "20261010_otlp_telemetry_ingest"
    harness_routing = script.get_revision("20261011_harness_routing")
    assert harness_routing.down_revision == "20261011_harness_inventory"
    runner_sessions = script.get_revision("20261011_runner_remote_sessions")
    assert runner_sessions.down_revision == "20261011_harness_routing"
    runner_presence = script.get_revision("20261011_runner_presence")
    assert runner_presence.down_revision == "20261011_runner_remote_sessions"
    result_schema = script.get_revision("20261011_flow_result_schema")
    assert result_schema.down_revision == "20261011_runner_presence"
    audit_outbox = script.get_revision("20261011_audit_outbox")
    assert audit_outbox.down_revision == "20261011_flow_result_schema"
    identity_schema = script.get_revision("20261012_identity_schema")
    assert identity_schema.down_revision == "20261011_audit_outbox"
    identity_backfill = script.get_revision("20261012_identity_backfill")
    assert identity_backfill.down_revision == "20261012_identity_schema"
    # The notes chain is re-parented onto the identity head so both features
    # share one linear history and the tree keeps a single head.
    note_delivery_event = script.get_revision("20261012_note_delivery_event")
    assert note_delivery_event.down_revision == "20261012_identity_backfill"
    note_receipts = script.get_revision("20261013_note_receipts")
    assert note_receipts.down_revision == "20261012_note_delivery_event"
    runner_scheduling = script.get_revision("20261011_runner_scheduling")
    assert runner_scheduling.down_revision == "20261013_note_receipts"
    desktop_views = script.get_revision("20261011_desktop_views")
    assert desktop_views.down_revision == "20261011_runner_scheduling"
    admin_activity = script.get_revision("20261013_admin_activity_idx")
    assert admin_activity.down_revision == "20261011_desktop_views"
    session_alias = script.get_revision("20261011_session_alias")
    assert session_alias.down_revision == "20261013_admin_activity_idx"
    gateway_agg = script.get_revision("20261014_gateway_agg_idx")
    assert gateway_agg.down_revision == "20261011_session_alias"
    execution_end = script.get_revision("20261014_execution_end_idx")
    assert execution_end.down_revision == "20261014_gateway_agg_idx"
    api_usage_autovacuum = script.get_revision("20261015_api_usage_autovacuum")
    assert api_usage_autovacuum.down_revision == "20261014_execution_end_idx"
    scope_nulls = script.get_revision("20261014_tool_config_scope_nulls")
    assert scope_nulls.down_revision == "20261015_api_usage_autovacuum"
    expected_devices = script.get_revision("20261014_expected_devices")
    assert expected_devices.down_revision == "20261014_tool_config_scope_nulls"
    policy_match = script.get_revision("20261014_policy_notice_match")
    assert policy_match.down_revision == "20261014_expected_devices"
    governance_review = script.get_revision("20261016_governance_review")
    assert governance_review.down_revision == "20261014_policy_notice_match"
    model_detail = script.get_revision("20261014_model_detail_idx")
    assert model_detail.down_revision == "20261016_governance_review"
    candidate = script.get_revision("20261017_gov_candidate_idx")
    assert candidate.down_revision == "20261014_model_detail_idx"
    hosted_credit = script.get_revision("20261018_hosted_credit")
    assert hosted_credit.down_revision == "20261017_gov_candidate_idx"
    thresholds = script.get_revision("20261019_gov_thresholds")
    assert thresholds.down_revision == "20261018_hosted_credit"
    jira_callback = script.get_revision("20261020_jira_callback")
    assert jira_callback.down_revision == "20261019_gov_thresholds"
    mcp_inventory = script.get_revision("20261021_mcp_inventory")
    assert mcp_inventory.down_revision == "20261020_jira_callback"
    runner_models = script.get_revision("20261021_runner_models")
    assert runner_models.down_revision == "20261020_jira_callback"
    device_fleet = script.get_revision("20261021_device_fleet")
    assert device_fleet.down_revision == "20261020_jira_callback"
    fleet_models_merge = script.get_revision("20261021_fleet_models_merge")
    assert set(fleet_models_merge.down_revision) == {
        "20261021_runner_models",
        "20261021_device_fleet",
    }
    inventory_merge = script.get_revision("20261021_mcp_fleet_merge")
    assert set(inventory_merge.down_revision) == {
        "20261021_mcp_inventory",
        "20261021_fleet_models_merge",
    }
    weekly_email = script.get_revision("20261022_weekly_report_email")
    assert weekly_email.down_revision == "20261021_mcp_fleet_merge"
    retry_index = script.get_revision("20261023_gov_retry_idx")
    assert retry_index.down_revision == "20261022_weekly_report_email"
    value_cache = script.get_revision("20261023_weekly_value_cache")
    assert value_cache.down_revision == "20261023_gov_retry_idx"
    member_reports = script.get_revision("20261024_weekly_members")
    assert member_reports.down_revision == "20261022_weekly_report_email"
    report_merge = script.get_revision("20261024_weekly_report_merge")
    assert set(report_merge.down_revision) == {
        "20261023_gov_retry_idx",
        "20261024_weekly_members",
    }
    value_merge = script.get_revision("20261025_weekly_value_merge")
    assert set(value_merge.down_revision) == {
        "20261023_weekly_value_cache",
        "20261024_weekly_report_merge",
    }
    owner_consent = script.get_revision("20261022_owner_consent")
    assert owner_consent.down_revision == "20261024_weekly_report_merge"
    consent_value_merge = script.get_revision("20261026_value_consent_merge")
    assert set(consent_value_merge.down_revision) == {
        "20261025_weekly_value_merge",
        "20261022_owner_consent",
    }
    chat_support = script.get_revision("20261027_chat_support")
    assert chat_support.down_revision == "20261026_value_consent_merge"
    admin_analytics = script.get_revision("20261027_admin_analytics")
    assert admin_analytics.down_revision == "20261026_value_consent_merge"
    chat_admin_merge = script.get_revision("20261028_chat_admin_merge")
    assert set(chat_admin_merge.down_revision) == {
        "20261027_chat_support",
        "20261027_admin_analytics",
    }
    verification = script.get_revision("20261028_obs_verification")
    assert verification.down_revision == "20261028_chat_admin_merge"
    governance_spend = script.get_revision("20261018_gov_spend_idx")
    assert governance_spend.down_revision == "20261028_obs_verification"
    unsealed = script.get_revision("20261029_audit_unsealed_idx")
    assert unsealed.down_revision == "20261018_gov_spend_idx"
    model_totals = script.get_revision("20261030_gateway_model_idx")
    assert model_totals.down_revision == "20261029_audit_unsealed_idx"
    live_activity = script.get_revision("20261031_live_activity")
    assert live_activity.down_revision == "20261030_gateway_model_idx"
    code_tools = script.get_revision("20261031_code_tool_idx")
    assert code_tools.down_revision == "20261030_gateway_model_idx"
    activity_merge = script.get_revision("20261031_activity_merge")
    assert set(activity_merge.down_revision) == {
        "20261031_live_activity",
        "20261031_code_tool_idx",
    }
    customer_usage = script.get_revision("20261101_customer_usage_idx")
    assert customer_usage.down_revision == "20261031_activity_merge"
    agent_usage = script.get_revision("20261102_agent_usage_idx")
    assert agent_usage.down_revision == "20261101_customer_usage_idx"
    restore_usage = script.get_revision("20261102_restore_usage_action")
    assert restore_usage.down_revision == "20261102_agent_usage_idx"
    parse_retry = script.get_revision("20261103_gov_parse_retry")
    assert parse_retry.down_revision == "20261102_restore_usage_action"
    session_permissions = script.get_revision("20261104_session_write_perms")
    assert session_permissions.down_revision == "20261103_gov_parse_retry"
    work_tools = script.get_revision("20261105_fleet_work_tools")
    assert work_tools.down_revision == "20261104_session_write_perms"
    cache_input = script.get_revision("20261106_anthropic_cache_input")
    assert cache_input.down_revision == "20261105_fleet_work_tools"
    command_lookup = script.get_revision("20261106_control_command_idx")
    assert command_lookup.down_revision == "20261106_anthropic_cache_input"
    work_ref = script.get_revision("20261106_fleet_work_ref_idx")
    assert work_ref.down_revision == "20261106_control_command_idx"
    annotations = script.get_revision("20261107_mcp_tool_annotations")
    assert annotations.down_revision == "20261106_fleet_work_ref_idx"
    capture_retry = script.get_revision("20261107_gov_capture_retry")
    assert capture_retry.down_revision == "20261107_mcp_tool_annotations"
    subscription_principal = script.get_revision("20261108_agent_sub_principal")
    assert subscription_principal.down_revision == "20261107_gov_capture_retry"
    turn_state = script.get_revision("20261107_session_turn_state")
    assert turn_state.down_revision == "20261108_agent_sub_principal"
    reachable = script.get_revision("20261108_control_reachable")
    assert reachable.down_revision == "20261107_session_turn_state"
    totals = script.get_revision("20261109_agent_usage_totals")
    assert totals.down_revision == "20261108_control_reachable"
    assessment = script.get_revision("20261110_assessment_pr_identity")
    assert assessment.down_revision == "20261109_agent_usage_totals"
    prune = script.get_revision("20261110_prune_api_usage_idx")
    assert prune.down_revision == "20261110_assessment_pr_identity"
    credential_backfill = script.get_revision("20261110_credential_backfill")
    assert credential_backfill.down_revision == "20261110_prune_api_usage_idx"
    credential_columns = script.get_revision("20261111_credential_columns")
    assert credential_columns.down_revision == "20261110_credential_backfill"
    media_timing = script.get_revision("20261110_media_timing")
    assert media_timing.down_revision == "20261111_credential_columns"
    flow_log_gateway_idx = script.get_revision("20261112_flow_log_gateway_idx")
    assert flow_log_gateway_idx.down_revision == "20261110_media_timing"
    delivery_report_policy = script.get_revision("20261112_delivery_report_policy")
    assert delivery_report_policy.down_revision == "20261112_flow_log_gateway_idx"
    activity_meta_bytes = script.get_revision("20261112_activity_meta_bytes")
    assert activity_meta_bytes.down_revision == "20261112_delivery_report_policy"
    agent_usage_replan = script.get_revision("20261112_agent_usage_replan")
    assert agent_usage_replan.down_revision == "20261112_activity_meta_bytes"
    upstream_mcp_oauth = script.get_revision("20261110_upstream_mcp_oauth")
    assert upstream_mcp_oauth.down_revision == "20261112_agent_usage_replan"
    forge_reconciliation = script.get_revision("20261112_forge_reconciliation")
    assert forge_reconciliation.down_revision == "20261110_upstream_mcp_oauth"
    enterprise_sso = script.get_revision("20261112_enterprise_sso")
    assert enterprise_sso.down_revision == "20261112_forge_reconciliation"
    pull_request_details = script.get_revision("20261113_pull_request_details")
    assert pull_request_details.down_revision == "20261112_enterprise_sso"
    group_grants = script.get_revision("20261114_sso_group_grants")
    assert group_grants.down_revision == "20261113_pull_request_details"
    assert script.get_heads() == ["20261114_sso_group_grants"]
