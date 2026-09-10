"""DORA agent-slice exports: register content, incident content, digest.

The fixture below plants one row of every record type both exports know
about, so a change that drops a class fails here rather than in an auditor's
spreadsheet.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models import models
from preloop.models.models.budget import BudgetPeriod, BudgetPolicy
from preloop.services import dora_exports
from preloop.services.dora_exports import (
    ASSET_COLUMNS,
    INCIDENT_COLUMNS,
    DoraExportError,
    build_asset_register,
    build_incident_candidates,
)

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
NAIVE_NOW = NOW.replace(tzinfo=None)


def _naive(offset_hours: float) -> datetime:
    return NAIVE_NOW + timedelta(hours=offset_hours)


@pytest.fixture
def estate(db_session, test_user):
    """One of everything: six asset classes, five incident classes."""
    account_id = test_user.account_id
    created = {"account_id": account_id, "user": test_user}

    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=account_id,
        runtime_session_id=None,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id="agent-session-1",
        display_name="Reconciliation agent",
        enrolled_via="runtime_session_token",
        enrollment_hostname="ops-laptop-7",
        owner_user_id=test_user.id,
        lifecycle_state="active",
        lifecycle_updated_at=_naive(-48),
        last_seen_at=_naive(-1),
    )
    db_session.add(agent)
    db_session.flush()
    created["agent"] = agent

    server = models.MCPServer(
        id=uuid4(),
        account_id=account_id,
        name="ledger-mcp",
        url="https://mcp.internal.example/ledger",
        transport="http-streaming",
        auth_type="oauth",
        status="active",
    )
    db_session.add(server)
    db_session.flush()
    created["server"] = server

    workflow = models.ApprovalWorkflow(
        id=uuid4(), account_id=account_id, name="Two-person ledger writes"
    )
    db_session.add(workflow)
    db_session.flush()

    tool = models.ToolConfiguration(
        id=uuid4(),
        account_id=account_id,
        tool_name="ledger_post",
        tool_source="mcp",
        mcp_server_id=server.id,
        is_enabled=True,
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool)
    db_session.flush()
    created["tool"] = tool

    db_session.add(
        models.ToolAccessRule(
            id=uuid4(),
            account_id=account_id,
            tool_configuration_id=tool.id,
            condition_type="simple",
            condition_expression="args.amount > 1000",
            action="deny",
            priority=10,
            is_enabled=True,
        )
    )

    model = models.AIModel(
        id=uuid4(),
        account_id=account_id,
        name="gpt-house",
        provider_name="OpenAI",
        model_identifier="gpt-5.4",
        api_endpoint="https://api.openai.com/v1",
        is_default=True,
    )
    db_session.add(model)
    db_session.flush()
    created["model"] = model

    runner = models.FlowRunner(
        id=uuid4(),
        account_id=account_id,
        registered_by_user_id=test_user.id,
        name="runner-eu-1",
        hostname="runner-eu-1.internal",
        os="linux",
        arch="arm64",
        labels=["eu-west"],
        status="online",
        last_heartbeat=NOW - timedelta(minutes=3),
        token_hash="0" * 64,
    )
    db_session.add(runner)
    db_session.flush()
    created["runner"] = runner

    db_session.add(
        BudgetPolicy(
            id=uuid4(),
            account_id=account_id,
            subject_type="managed_agent",
            subject_id=agent.id,
            model_alias="gpt-house",
            period=BudgetPeriod.monthly,
            hard_limit_usd=250.0,
            soft_limit_usd=200.0,
        )
    )
    db_session.add(
        BudgetPolicy(
            id=uuid4(),
            account_id=account_id,
            subject_type="account",
            subject_id=None,
            model_alias=None,
            period=BudgetPeriod.monthly,
            hard_limit_usd=5000.0,
        )
    )

    # --- incident candidates, one of each class ---
    flow = models.Flow(
        id=uuid4(),
        account_id=account_id,
        name="Nightly reconciliation",
        prompt_template="reconcile",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    created["flow"] = flow

    execution = models.FlowExecution(
        id=uuid4(),
        flow_id=flow.id,
        status="FAILED",
        start_time=_naive(-3),
        end_time=_naive(-2),
        error_message="tool call timed out after 300s",
        failure_category="tool_timeout",
        agent_session_reference="agent-session-1",
    )
    db_session.add(execution)
    db_session.flush()
    created["execution"] = execution

    halt = models.AuditLog(
        id=uuid4(),
        account_id=str(account_id),
        user_id=test_user.id,
        action="kill_switch_activated",
        resource_type="account",
        resource_id=str(account_id),
        status="success",
        details={
            "scope": "gateway",
            "scopes": ["gateway"],
            "reason": "provider incident",
        },
        timestamp=_naive(-4),
    )
    deny = models.AuditLog(
        id=uuid4(),
        account_id=str(account_id),
        user_id=test_user.id,
        action="policy_deny",
        resource_type="tool_call",
        resource_id="ledger_post",
        status="denied",
        details={
            "correlation_id": "corr-deny-1",
            "tool_name": "ledger_post",
            "rule_description": "amount over 1000 requires a human",
            "runtime_session_id": None,
        },
        timestamp=_naive(-5),
    )
    budget = models.AuditLog(
        id=uuid4(),
        account_id=str(account_id),
        user_id=test_user.id,
        action="model_gateway_request",
        resource_type="model_gateway",
        resource_id="usage-1",
        status="budget_denied",
        details={
            "model_alias": "gpt-house",
            "provider_name": "OpenAI",
            "status_code": 403,
            "error_detail": "monthly hard limit reached",
            "runtime_principal_type": "claude_code",
            "runtime_principal_id": "agent-session-1",
        },
        timestamp=_naive(-6),
    )
    db_session.add_all([halt, deny, budget])

    usage = models.ApiUsage(
        id=uuid4(),
        account_id=account_id,
        user_id=test_user.id,
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=503,
        duration=1.5,
        ai_model_id=model.id,
        model_alias="gpt-house",
        provider_name="OpenAI",
        upstream_request_id="req_upstream_1",
        error_class="upstream_overloaded",
        runtime_principal_type="claude_code",
        runtime_principal_id="agent-session-1",
        timestamp=_naive(-7),
    )
    ok_usage = models.ApiUsage(
        id=uuid4(),
        account_id=account_id,
        user_id=test_user.id,
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.9,
        ai_model_id=model.id,
        model_alias="gpt-house",
        provider_name="OpenAI",
        timestamp=_naive(-8),
    )
    db_session.add_all([usage, ok_usage])
    created["usage"] = usage
    db_session.commit()
    return created


def _rows_by_type(export):
    grouped: dict[str, list[dict]] = {}
    for row in export.rows:
        grouped.setdefault(row["record_type"], []).append(row)
    return grouped


class TestAssetRegister:
    def test_every_record_type_is_present(self, db_session, test_user, estate):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        grouped = _rows_by_type(export)
        assert set(grouped) == set(dora_exports.ASSET_RECORD_TYPES)
        assert export.manifest["counts"] == {
            "agent": 1,
            "tool": 1,
            "mcp_server": 1,
            "model": 1,
            "provider": 1,
            "runner_host": 1,
        }
        assert export.manifest["total_rows"] == len(export.rows) == 6

    def test_agent_row_carries_owner_first_seen_and_policy(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        agent = _rows_by_type(export)["agent"][0]
        assert agent["name"] == "Reconciliation agent"
        assert agent["owner_user_id"] == str(test_user.id)
        assert agent["owner_username"] == (test_user.username or test_user.email)
        assert agent["first_seen"] is not None
        assert agent["last_seen"].endswith("Z")
        assert agent["attached_policy_count"] == 1
        assert agent["attached_policies"][0].startswith("budget:")

    def test_tool_row_lists_rule_and_approval_workflow(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        tool = _rows_by_type(export)["tool"][0]
        assert tool["parent_asset_id"] == str(estate["server"].id)
        assert any(
            policy.startswith("rule:deny") for policy in tool["attached_policies"]
        )
        assert "approval_workflow:Two-person ledger writes" in tool["attached_policies"]

    def test_server_inherits_the_controls_of_its_tools(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        server = _rows_by_type(export)["mcp_server"][0]
        assert server["asset_kind"] == "http-streaming/oauth"
        assert server["location"] == "https://mcp.internal.example/ledger"
        assert any(
            "via ledger_post" in policy for policy in server["attached_policies"]
        )
        assert server["attached_policy_count"] == len(server["attached_policies"])

    def test_provider_is_a_derived_row_with_a_stable_id(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        grouped = _rows_by_type(export)
        provider = grouped["provider"][0]
        model = grouped["model"][0]
        assert provider["asset_id"] == "provider:openai"
        assert model["parent_asset_id"] == provider["asset_id"]
        assert provider["last_seen"] is not None, "gateway usage dates the provider"
        assert provider["source_table"].endswith("(derived)")

    def test_runner_host_row(self, db_session, test_user, estate):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        runner = _rows_by_type(export)["runner_host"][0]
        assert runner["location"] == "runner-eu-1.internal"
        assert runner["asset_kind"] == "linux arm64"
        assert runner["owner_user_id"] == str(test_user.id)
        assert runner["lifecycle_state"] == "online"

    def test_ee_only_columns_are_present_and_declared_absent(
        self, db_session, test_user, estate
    ):
        """The column never disappears; the manifest says why it is empty."""
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        assert "last_config_change_at" in export.manifest["columns"]
        assert all(row["last_config_change_at"] is None for row in export.rows)
        absent = {item["field"] for item in export.manifest["edition"]["fields_absent"]}
        assert absent == {"last_config_change_at", "last_config_change_by"}
        assert export.manifest["edition"]["edition"] == "oss"

    def test_manifest_names_the_articles_and_the_scope(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        feeds = " ".join(export.manifest["feeds"])
        assert "Art. 8" in feeds and "Art. 28" in feeds
        assert "agent slice" in export.manifest["scope"]

    def test_account_wide_budget_is_reported_once_not_on_every_row(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        assert len(export.manifest["account_policies"]) == 1
        assert "5000" in export.manifest["account_policies"][0]

    def test_other_accounts_are_not_in_the_register(
        self, db_session, test_user, estate
    ):
        other = models.Account(id=uuid4())
        db_session.add(other)
        db_session.flush()
        db_session.add(
            models.MCPServer(
                id=uuid4(),
                account_id=other.id,
                name="not-ours",
                url="https://elsewhere.example",
                transport="http-streaming",
                auth_type="none",
                status="active",
            )
        )
        db_session.commit()
        export = build_asset_register(
            db_session, account=test_user.account, generated_at=NOW
        )
        assert all(row["name"] != "not-ours" for row in export.rows)


class TestIncidentCandidates:
    def _export(self, db_session, test_user, fmt="json"):
        return build_incident_candidates(
            db_session,
            account=test_user.account,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(hours=1),
            export_format=fmt,
            generated_at=NOW,
        )

    def test_every_record_type_is_present(self, db_session, test_user, estate):
        export = self._export(db_session, test_user)
        assert export.manifest["counts"] == {
            "execution_failure": 1,
            "kill_switch_activation": 1,
            "policy_deny": 1,
            "budget_breach": 1,
            "gateway_upstream_failure": 1,
        }

    def test_rows_are_ordered_by_time(self, db_session, test_user, estate):
        export = self._export(db_session, test_user)
        stamps = [row["occurred_at"] for row in export.rows]
        assert stamps == sorted(stamps)

    def test_execution_failure_carries_correlation_and_agent(
        self, db_session, test_user, estate
    ):
        export = self._export(db_session, test_user)
        row = _rows_by_type(export)["execution_failure"][0]
        assert row["correlation_id"] == str(estate["execution"].id)
        assert row["correlation_source"] == "flow_execution.id"
        assert row["agent_id"] == str(estate["agent"].id)
        assert row["agent_name"] == "Reconciliation agent"
        assert row["platform_category"] == "tool_timeout"
        assert row["detail"] == "tool call timed out after 300s"

    def test_gateway_failure_uses_the_upstream_request_id(
        self, db_session, test_user, estate
    ):
        export = self._export(db_session, test_user)
        row = _rows_by_type(export)["gateway_upstream_failure"][0]
        assert row["correlation_id"] == "req_upstream_1"
        assert row["correlation_source"] == "api_usage.upstream_request_id"
        assert row["provider"] == "OpenAI"
        assert row["agent_id"] == str(estate["agent"].id)

    def test_successful_calls_are_not_candidates(self, db_session, test_user, estate):
        export = self._export(db_session, test_user)
        assert all(row["status_code"] != 200 for row in export.rows)

    def test_our_own_halt_is_not_an_upstream_failure(
        self, db_session, test_user, estate
    ):
        """A request refused by the kill switch is the halt, not the provider."""
        db_session.add(
            models.ApiUsage(
                id=uuid4(),
                account_id=test_user.account_id,
                endpoint="/v1/chat/completions",
                method="POST",
                status_code=503,
                duration=0.01,
                error_class="kill_switch",
                timestamp=_naive(-2),
            )
        )
        db_session.commit()
        export = self._export(db_session, test_user)
        assert export.manifest["counts"]["gateway_upstream_failure"] == 1

    def test_no_classification_columns_are_emitted(self, db_session, test_user, estate):
        export = self._export(db_session, test_user)
        columns = set(export.manifest["columns"])
        assert not columns & {"severity", "is_major", "client_impact", "classification"}
        assert "entity" in export.manifest["classification"]

    def test_period_boundary_is_half_open(self, db_session, test_user, estate):
        """[start, end): consecutive periods tile without double counting."""
        boundary = _naive(-4).replace(tzinfo=UTC)
        before = build_incident_candidates(
            db_session,
            account=test_user.account,
            start=NOW - timedelta(days=1),
            end=boundary,
            generated_at=NOW,
        )
        after = build_incident_candidates(
            db_session,
            account=test_user.account,
            start=boundary,
            end=NOW + timedelta(hours=1),
            generated_at=NOW,
        )
        assert before.manifest["counts"]["kill_switch_activation"] == 0
        assert after.manifest["counts"]["kill_switch_activation"] == 1

    def test_end_before_start_is_refused(self, db_session, test_user):
        with pytest.raises(DoraExportError) as error:
            build_incident_candidates(
                db_session,
                account=test_user.account,
                start=NOW,
                end=NOW - timedelta(days=1),
            )
        assert error.value.code == "invalid_period"

    def test_oss_declares_policy_denies_unrecorded(self, db_session, test_user, estate):
        """Zero denies on OSS means unrecorded, and the manifest says so."""
        export = self._export(db_session, test_user)
        absent = export.manifest["edition"]["record_types_absent"]
        assert absent[0]["record_type"] == "policy_deny"
        assert "Enterprise" in absent[0]["reason"]

    def test_other_accounts_are_not_in_the_candidates(
        self, db_session, test_user, estate
    ):
        other = models.Account(id=uuid4())
        db_session.add(other)
        db_session.flush()
        db_session.add(
            models.AuditLog(
                id=uuid4(),
                account_id=str(other.id),
                action="kill_switch_activated",
                resource_type="account",
                status="success",
                details={"scope": "all", "reason": "not ours"},
                timestamp=_naive(-4),
            )
        )
        db_session.commit()
        export = self._export(db_session, test_user)
        assert export.manifest["counts"]["kill_switch_activation"] == 1


class TestRendering:
    def test_csv_header_matches_the_column_list(self, db_session, test_user, estate):
        export = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        text = export.body.decode("utf-8")
        assert text.startswith(",".join(ASSET_COLUMNS))
        assert "\r\n" in text, "RFC 4180 line terminator"
        parsed = list(csv.DictReader(io.StringIO(text)))
        assert len(parsed) == 6
        assert parsed[0]["record_type"] == "agent"

    def test_csv_joins_list_cells_and_empties_nulls(
        self, db_session, test_user, estate
    ):
        export = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        parsed = {
            row["record_type"]: row
            for row in csv.DictReader(io.StringIO(export.body.decode("utf-8")))
        }
        assert "; " in parsed["tool"]["attached_policies"]
        assert parsed["agent"]["last_config_change_at"] == ""

    def test_incident_csv_columns(self, db_session, test_user, estate):
        export = build_incident_candidates(
            db_session,
            account=test_user.account,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(hours=1),
            export_format="csv",
            generated_at=NOW,
        )
        header = export.body.decode("utf-8").split("\r\n")[0]
        assert header == ",".join(INCIDENT_COLUMNS)

    def test_json_body_is_the_rows(self, db_session, test_user, estate):
        export = build_asset_register(
            db_session,
            account=test_user.account,
            export_format="json",
            generated_at=NOW,
        )
        parsed = json.loads(export.body)
        assert isinstance(parsed, list)
        assert parsed[0]["record_type"] == "agent"

    def test_unsupported_format_is_refused(self, db_session, test_user):
        with pytest.raises(DoraExportError) as error:
            build_asset_register(
                db_session, account=test_user.account, export_format="xlsx"
            )
        assert error.value.code == "unsupported_format"


class TestManifestDigest:
    def test_member_digest_matches_the_body(self, db_session, test_user, estate):
        export = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        member = export.manifest["members"][0]
        assert member["name"] == "asset-register.csv"
        assert member["size_bytes"] == len(export.body)
        assert member["sha256"] == hashlib.sha256(export.body).hexdigest()

    def test_members_digest_is_the_evidence_pack_computation(
        self, db_session, test_user, estate
    ):
        """Same digest as #511 and #559, so one verifier covers all three."""
        export = build_incident_candidates(
            db_session,
            account=test_user.account,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(hours=1),
            export_format="json",
            generated_at=NOW,
        )
        expected = hashlib.sha256(
            canonical_manifest_json(export.manifest["members"])
        ).hexdigest()
        assert export.manifest["members_digest"] == expected

    def test_json_rows_recompute_the_member_digest(self, db_session, test_user, estate):
        """A verifier can hash the parsed rows and get the member digest back."""
        export = build_asset_register(
            db_session,
            account=test_user.account,
            export_format="json",
            generated_at=NOW,
        )
        rehashed = hashlib.sha256(
            canonical_manifest_json(json.loads(export.body))
        ).hexdigest()
        assert rehashed == export.manifest["members"][0]["sha256"]

    def test_two_exports_of_the_same_data_agree(self, db_session, test_user, estate):
        first = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        second = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        assert first.manifest == second.manifest
        assert first.sha256 == second.sha256

    def test_a_changed_row_changes_the_digest(self, db_session, test_user, estate):
        before = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        estate["agent"].display_name = "Renamed agent"
        db_session.commit()
        after = build_asset_register(
            db_session, account=test_user.account, export_format="csv", generated_at=NOW
        )
        assert after.sha256 != before.sha256
        assert after.manifest["members_digest"] != before.manifest["members_digest"]
