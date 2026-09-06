"""Ordered per-flow model/harness routing from current issue labels."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow
from preloop.models.models import Flow
from preloop.models.models.ai_model import AIModel
from preloop.models.models.flow_execution import (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
    FlowExecution,
    resolve_execution_agent_selection,
)
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate, ModelRoutingConfig
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.model_routing import (
    ModelRoutingError,
    extract_trusted_labels,
    first_matching_rule,
    native_handoff_required,
    parse_model_routing,
    prepare_execution_routing,
    rule_matches_labels,
    strip_untrusted_overrides,
)

FAST_MODEL = uuid4()
DEFAULT_MODEL = uuid4()
FOREIGN_MODEL = uuid4()


def _rule(
    rule_id: str,
    *,
    any_labels=None,
    all_labels=None,
    model_id=FAST_MODEL,
    agent_type="codex",
):
    labels = {}
    if any_labels is not None:
        labels["any"] = any_labels
    if all_labels is not None:
        labels["all"] = all_labels
    return {
        "id": rule_id,
        "labels": labels,
        "ai_model_id": str(model_id),
        "agent_type": agent_type,
    }


def _policy(*rules):
    return {"version": 1, "rules": list(rules)}


class TestParseAndMatch:
    def test_absent_policy_is_none(self):
        assert parse_model_routing({}) is None
        assert parse_model_routing({"sandbox_type": "exec"}) is None
        assert parse_model_routing(None) is None

    def test_empty_rules_parse(self):
        config = parse_model_routing({"model_routing": {"version": 1, "rules": []}})
        assert config is not None
        assert config.rules == []

    def test_rejects_assessment_fields(self):
        with pytest.raises(ModelRoutingError):
            parse_model_routing(
                {
                    "model_routing": {
                        "version": 1,
                        "rules": [
                            {
                                **_rule("docs", any_labels=["documentation"]),
                                "issue_types": ["bug"],
                            }
                        ],
                    }
                }
            )

    def test_rejects_duplicate_ids(self):
        with pytest.raises((ModelRoutingError, ValidationError)):
            ModelRoutingConfig.model_validate(
                _policy(
                    _rule("dup", any_labels=["a"]),
                    _rule("dup", any_labels=["b"]),
                )
            )

    def test_first_match_wins(self):
        config = parse_model_routing(
            {
                "model_routing": _policy(
                    _rule("docs", any_labels=["documentation"]),
                    _rule("bugs", any_labels=["bug"]),
                )
            }
        )
        assert config is not None
        matched = first_matching_rule(config.rules, ["bug", "documentation", "backend"])
        assert matched is not None
        assert matched.id == "docs"

    def test_any_requires_one(self):
        config = parse_model_routing(
            {
                "model_routing": _policy(
                    _rule("docs", any_labels=["docs", "documentation"])
                )
            }
        )
        rule = config.rules[0]
        assert rule_matches_labels(rule, ["documentation"])
        assert not rule_matches_labels(rule, ["bug"])

    def test_all_requires_every(self):
        config = parse_model_routing(
            {"model_routing": _policy(_rule("combo", all_labels=["bug", "backend"]))}
        )
        rule = config.rules[0]
        assert rule_matches_labels(rule, ["bug", "backend", "p1"])
        assert not rule_matches_labels(rule, ["bug"])

    def test_any_and_all_combined(self):
        config = parse_model_routing(
            {
                "model_routing": _policy(
                    _rule(
                        "combo",
                        any_labels=["docs", "documentation"],
                        all_labels=["needs-review"],
                    )
                )
            }
        )
        rule = config.rules[0]
        assert rule_matches_labels(rule, ["documentation", "needs-review"])
        assert not rule_matches_labels(rule, ["documentation"])
        assert not rule_matches_labels(rule, ["needs-review"])

    def test_no_match_returns_none(self):
        config = parse_model_routing(
            {"model_routing": _policy(_rule("docs", any_labels=["documentation"]))}
        )
        assert first_matching_rule(config.rules, ["bug"]) is None

    def test_extract_labels_from_normalized_payload(self):
        labels = extract_trusted_labels(
            {
                "payload": {
                    "labels": ["bug", "backend"],
                    "issue": {"labels": [{"name": "github-only"}]},
                    "ai_model_id": str(FOREIGN_MODEL),
                    "assessment": {"issue_kind": "security"},
                }
            }
        )
        assert labels == ["bug", "backend", "github-only"]

    def test_strip_untrusted_overrides(self):
        event = {
            MATRIX_OVERRIDES_KEY: {
                "agent_type": "codex",
                "ai_model_id": str(FOREIGN_MODEL),
            },
            ROUTING_RECORD_KEY: {"source": "rule"},
            "ai_model_id": str(FOREIGN_MODEL),
            "assessment": {"issue_kind": "security"},
            "payload": {
                "labels": ["bug"],
                MATRIX_OVERRIDES_KEY: {"agent_type": "codex"},
                "ai_model_id": str(FOREIGN_MODEL),
            },
        }
        strip_untrusted_overrides(event)
        assert MATRIX_OVERRIDES_KEY not in event
        assert ROUTING_RECORD_KEY not in event
        assert "ai_model_id" not in event
        assert "assessment" not in event
        assert event["payload"]["labels"] == ["bug"]
        assert MATRIX_OVERRIDES_KEY not in event["payload"]
        assert "ai_model_id" not in event["payload"]


class TestNativeHandoff:
    def test_same_identity_does_not_handoff(self):
        assert not native_handoff_required(
            ("codex", str(FAST_MODEL)), ("codex", FAST_MODEL)
        )

    def test_model_change_requires_new_session(self):
        assert native_handoff_required(
            ("codex", str(FAST_MODEL)), ("codex", str(DEFAULT_MODEL))
        )

    def test_harness_change_requires_new_session(self):
        assert native_handoff_required(
            ("opencode", str(FAST_MODEL)), ("codex", str(FAST_MODEL))
        )


def _usable_model(
    db_session: Session, account_id, *, name="Fast", provider="openai"
) -> AIModel:
    model = AIModel(
        name=name,
        provider_name=provider,
        model_identifier="gpt-4o",
        account_id=account_id,
        api_key="sk-test",
    )
    db_session.add(model)
    db_session.flush()
    db_session.refresh(model)
    return model


def _flow(
    db_session: Session,
    test_user: User,
    *,
    routing=None,
    agent_type="codex",
    extra_config=None,
    ai_model_id=None,
) -> Flow:
    config = {"sandbox_type": "exec", "max_iterations": 9}
    if extra_config:
        config.update(extra_config)
    if routing is not None:
        config["model_routing"] = routing
    flow_in = FlowCreate(
        name=f"Routing Flow {uuid4().hex[:8]}",
        prompt_template="Handle {{payload.title}}",
        agent_type=agent_type,
        agent_config=config,
        ai_model_id=ai_model_id,
        trigger_event_source="github",
        trigger_event_types=["issue_opened"],
        account_id=test_user.account_id,
    )
    return crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=test_user.account_id
    )


def _patch_dispatch():
    return (
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new_callable=AsyncMock,
        ),
        patch.object(
            FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
        ),
    )


class TestPrepareAndTrigger:
    @pytest.mark.asyncio
    async def test_absent_policy_leaves_snapshot_unchanged(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        flow = _flow(db_session, test_user, ai_model_id=default.id)
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                test_mode=True,
                trigger_event_data={"payload": {"labels": ["bug"]}},
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        assert ROUTING_RECORD_KEY not in row.trigger_event_details
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details

    @pytest.mark.asyncio
    async def test_first_match_records_rule(self, db_session: Session, test_user: User):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id),
                _rule("bugs", any_labels=["bug"], model_id=default.id),
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={"payload": {"labels": ["bug", "documentation"]}},
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["source"] == "rule"
        assert record["rule_id"] == "docs"
        assert record["ai_model_id"] == str(fast.id)
        assert record["agent_type"] == "codex"
        assert record["label_snapshot"] == ["bug", "documentation"]

    @pytest.mark.asyncio
    async def test_default_when_no_rule_matches(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={"payload": {"labels": ["enhancement"]}},
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["source"] == "default"
        assert record["ai_model_id"] == str(default.id)
        assert "rule_id" not in record

    @pytest.mark.asyncio
    async def test_planted_matrix_is_ignored(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={
                    "payload": {"labels": ["documentation"]},
                    MATRIX_OVERRIDES_KEY: {
                        "agent_type": "opencode",
                        "ai_model_id": str(default.id),
                    },
                    "ai_model_id": str(default.id),
                    "assessment": {"issue_kind": "security"},
                },
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["rule_id"] == "docs"
        assert record["ai_model_id"] == str(fast.id)
        assert "assessment" not in row.trigger_event_details

    @pytest.mark.asyncio
    async def test_retry_pins_recorded_selection(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            first = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={"payload": {"labels": ["documentation"]}},
            )
        original = db_session.query(FlowExecution).filter_by(id=first["id"]).one()
        original.status = "FAILED"
        db_session.flush()

        flow.agent_config = {
            **flow.agent_config,
            "model_routing": _policy(
                _rule("bugs", any_labels=["bug"], model_id=default.id)
            ),
        }
        db_session.flush()

        with nats_patch, dispatch_patch:
            retry = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data=original.trigger_event_details,
                retry_of_execution_id=original.id,
            )
        row = db_session.query(FlowExecution).filter_by(id=retry["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["ai_model_id"] == str(fast.id)
        assert record["rule_id"] == "docs"
        assert record["source"] == "pinned"

    @pytest.mark.asyncio
    async def test_resume_pins_prior_execution_record(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            first = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={"payload": {"labels": ["documentation"]}},
            )
        original = db_session.query(FlowExecution).filter_by(id=first["id"]).one()
        flow.agent_config = {
            **flow.agent_config,
            "model_routing": _policy(
                _rule("bugs", any_labels=["bug"], model_id=default.id)
            ),
        }
        db_session.flush()
        with nats_patch, dispatch_patch:
            resumed = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={"payload": {"labels": ["bug"]}},
                source_execution_id=original.id,
            )
        row = db_session.query(FlowExecution).filter_by(id=resumed["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["ai_model_id"] == str(fast.id)
        assert record["handoff"] == "native_continue"
        assert record["source"] == "pinned"

    @pytest.mark.asyncio
    async def test_body_resume_plus_foreign_matrix_is_ignored(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        foreign = _usable_model(db_session, other.id, name="Foreign")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={
                    "payload": {"labels": ["documentation"]},
                    "_resume": {"anything": True},
                    MATRIX_OVERRIDES_KEY: {
                        "ai_model_id": str(foreign.id),
                        "agent_type": "codex",
                    },
                },
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["ai_model_id"] == str(fast.id)
        assert record["source"] == "rule"

    def test_trigger_endpoint_ignores_resume_and_foreign_matrix(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        foreign = _usable_model(db_session, other.id, name="Foreign")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = client.post(
                f"/api/v1/flows/{flow.id}/trigger",
                json={
                    "payload": {"labels": ["documentation"]},
                    "_resume": {"anything": True},
                    MATRIX_OVERRIDES_KEY: {
                        "ai_model_id": str(foreign.id),
                        "agent_type": "codex",
                    },
                },
            )
        assert response.status_code == 200, response.text
        row = db_session.query(FlowExecution).filter_by(id=response.json()["id"]).one()
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["ai_model_id"] == str(fast.id)
        assert str(foreign.id) not in str(row.trigger_event_details)

    @pytest.mark.asyncio
    async def test_injected_routing_record_is_stripped(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={
                    "payload": {"labels": ["bug"]},
                    ROUTING_RECORD_KEY: {
                        "schema_version": 1,
                        "ai_model_id": str(fast.id),
                        "agent_type": "codex",
                        "source": "rule",
                        "rule_id": "planted",
                    },
                },
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["source"] == "default"
        assert record["ai_model_id"] == str(default.id)
        assert record.get("rule_id") != "planted"

    @pytest.mark.asyncio
    async def test_body_resume_of_other_account_execution_is_not_pinned(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        other_model = _usable_model(db_session, other.id, name="OtherDefault")
        other_flow_in = FlowCreate(
            name=f"Other Flow {uuid4().hex[:8]}",
            prompt_template="Handle {{payload.title}}",
            agent_type="codex",
            agent_config={
                "model_routing": _policy(
                    _rule("docs", any_labels=["documentation"], model_id=other_model.id)
                )
            },
            ai_model_id=other_model.id,
            trigger_event_source="github",
            trigger_event_types=["issue_opened"],
            account_id=other.id,
        )
        other_flow = crud_flow.create(
            db=db_session, flow_in=other_flow_in, account_id=other.id
        )
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            foreign_run = await service.trigger_flow(
                flow_id=other_flow.id,
                trigger_event_data={"payload": {"labels": ["documentation"]}},
            )
            result = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data={
                    "payload": {"labels": ["bug"]},
                    "_resume": {"execution_id": str(foreign_run["id"])},
                },
            )
        row = db_session.query(FlowExecution).filter_by(id=result["id"]).one()
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["source"] == "default"
        assert record["ai_model_id"] == str(default.id)
        assert record["ai_model_id"] != str(other_model.id)

    @pytest.mark.asyncio
    async def test_source_execution_id_from_other_account_fails_closed(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        other_model = _usable_model(db_session, other.id, name="OtherDefault")
        other_flow_in = FlowCreate(
            name=f"Other Flow {uuid4().hex[:8]}",
            prompt_template="go",
            agent_type="codex",
            agent_config={},
            ai_model_id=other_model.id,
            trigger_event_source="github",
            trigger_event_types=["issue_opened"],
            account_id=other.id,
        )
        other_flow = crud_flow.create(
            db=db_session, flow_in=other_flow_in, account_id=other.id
        )
        flow = _flow(db_session, test_user, ai_model_id=default.id)
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            foreign_run = await service.trigger_flow(flow_id=other_flow.id)
            with pytest.raises(ModelRoutingError, match="source execution"):
                await service.trigger_flow(
                    flow_id=flow.id,
                    source_execution_id=foreign_run["id"],
                )

    @pytest.mark.asyncio
    async def test_authorized_matrix_cell_is_stored(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            result = await service.trigger_flow_matrix(
                flow_id=flow.id,
                matrix=[{"agent_type": "opencode", "ai_model_id": str(default.id)}],
                trigger_event_data={"payload": {"labels": ["documentation"]}},
            )
        row = (
            db_session.query(FlowExecution)
            .filter_by(id=result["executions"][0]["id"])
            .one()
        )
        cell = row.trigger_event_details[MATRIX_OVERRIDES_KEY]
        assert cell["ai_model_id"] == str(default.id)
        assert cell["agent_type"] == "opencode"
        assert ROUTING_RECORD_KEY not in row.trigger_event_details
        assert resolve_execution_agent_selection(
            row.trigger_event_details,
            flow_agent_type=flow.agent_type,
            flow_ai_model_id=flow.ai_model_id,
        ) == ("opencode", str(default.id))

    @pytest.mark.asyncio
    async def test_retry_of_matrix_cell_pins_persisted_matrix(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        service = FlowTriggerService(db_session)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            batch = await service.trigger_flow_matrix(
                flow_id=flow.id,
                matrix=[{"agent_type": "opencode", "ai_model_id": str(default.id)}],
            )
        original = (
            db_session.query(FlowExecution)
            .filter_by(id=batch["executions"][0]["id"])
            .one()
        )
        original.status = "FAILED"
        db_session.flush()
        with nats_patch, dispatch_patch:
            retry = await service.trigger_flow(
                flow_id=flow.id,
                trigger_event_data=original.trigger_event_details,
                retry_of_execution_id=original.id,
            )
        row = db_session.query(FlowExecution).filter_by(id=retry["id"]).one()
        assert row.trigger_event_details[MATRIX_OVERRIDES_KEY]["ai_model_id"] == str(
            default.id
        )
        assert row.trigger_event_details[MATRIX_OVERRIDES_KEY]["agent_type"] == (
            "opencode"
        )

    @pytest.mark.asyncio
    async def test_webhook_nested_payload_overrides_are_ignored(
        self, db_session: Session, test_user: User
    ):
        import secrets

        from fastapi import FastAPI

        from preloop.api.endpoints.flows import router
        from preloop.models.db.session import get_db_session as get_db
        from preloop.models.schemas.flow import WebhookConfig

        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        foreign = _usable_model(db_session, other.id, name="Foreign")
        webhook_secret = secrets.token_urlsafe(32)
        flow_in = FlowCreate(
            name=f"Webhook routing {uuid4().hex[:8]}",
            prompt_template="Process: {{trigger_event.payload.data}}",
            agent_type="codex",
            agent_config={
                "model_routing": _policy(
                    _rule("docs", any_labels=["documentation"], model_id=fast.id)
                )
            },
            ai_model_id=default.id,
            trigger_event_source="webhook",
            trigger_event_types=["webhook"],
            webhook_config=WebhookConfig(webhook_secret=webhook_secret),
            account_id=test_user.account_id,
        )
        flow = crud_flow.create(
            db=db_session, flow_in=flow_in, account_id=test_user.account_id
        )

        def override_get_db():
            try:
                yield db_session
            finally:
                pass

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = client.post(
                f"/webhooks/flows/{flow.id}/{webhook_secret}",
                json={
                    "labels": ["documentation"],
                    "_resume": {"anything": True},
                    MATRIX_OVERRIDES_KEY: {
                        "ai_model_id": str(foreign.id),
                        "agent_type": "codex",
                    },
                    ROUTING_RECORD_KEY: {
                        "schema_version": 1,
                        "ai_model_id": str(foreign.id),
                        "agent_type": "codex",
                        "source": "rule",
                    },
                    "ai_model_id": str(foreign.id),
                },
            )
        assert response.status_code == 200, response.text
        row = (
            db_session.query(FlowExecution)
            .filter_by(id=response.json()["execution_id"])
            .one()
        )
        payload = row.trigger_event_details.get("payload") or {}
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details
        assert MATRIX_OVERRIDES_KEY not in payload
        assert "ai_model_id" not in payload
        record = row.trigger_event_details[ROUTING_RECORD_KEY]
        assert record["ai_model_id"] == str(fast.id)
        assert record["source"] == "rule"

    def test_foreign_model_rejected_on_save(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"Other {uuid4().hex[:8]}"}
        )
        foreign = _usable_model(db_session, other.id, name="Foreign")
        response = client.post(
            "/api/v1/flows",
            json={
                "name": f"Bad routing {uuid4().hex[:8]}",
                "prompt_template": "go",
                "agent_type": "codex",
                "agent_config": {
                    "model_routing": _policy(
                        _rule("docs", any_labels=["documentation"], model_id=foreign.id)
                    )
                },
            },
        )
        assert response.status_code == 422
        assert "not found" in response.json()["detail"]

    def test_codex_incompatible_model_rejected_on_save(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        other_provider = AIModel(
            name="Other provider",
            provider_name="anthropic",
            model_identifier="claude-sonnet",
            account_id=test_user.account_id,
            api_key="sk-test",
        )
        db_session.add(other_provider)
        db_session.flush()
        response = client.post(
            "/api/v1/flows",
            json={
                "name": f"Codex mismatch {uuid4().hex[:8]}",
                "prompt_template": "go",
                "agent_type": "codex",
                "agent_config": {
                    "model_routing": _policy(
                        _rule(
                            "docs",
                            any_labels=["documentation"],
                            model_id=other_provider.id,
                            agent_type="codex",
                        )
                    )
                },
            },
        )
        assert response.status_code == 422
        assert "not usable" in response.json()["detail"]

    def test_save_preserves_unrelated_agent_config(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        response = client.post(
            "/api/v1/flows",
            json={
                "name": f"Keep config {uuid4().hex[:8]}",
                "prompt_template": "go",
                "agent_type": "codex",
                "ai_model_id": str(fast.id),
                "agent_config": {
                    "sandbox_type": "exec",
                    "max_iterations": 4,
                    "model_routing": _policy(
                        _rule("docs", any_labels=["documentation"], model_id=fast.id)
                    ),
                },
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["agent_config"]["sandbox_type"] == "exec"
        assert data["agent_config"]["max_iterations"] == 4
        assert data["agent_config"]["model_routing"]["rules"][0]["id"] == "docs"

    def test_trigger_endpoint_strips_planted_matrix(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        flow = _flow(db_session, test_user, ai_model_id=default.id)
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = client.post(
                f"/api/v1/flows/{flow.id}/trigger",
                json={
                    "payload": {"labels": ["bug"]},
                    MATRIX_OVERRIDES_KEY: {
                        "agent_type": "opencode",
                        "ai_model_id": str(default.id),
                    },
                },
            )
        assert response.status_code == 200, response.text
        row = db_session.query(FlowExecution).filter_by(id=response.json()["id"]).one()
        assert MATRIX_OVERRIDES_KEY not in row.trigger_event_details


class TestOrchestratorSelection:
    def test_routing_record_selects_model(self, db_session: Session, test_user: User):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(db_session, test_user, ai_model_id=default.id)
        orchestrator = FlowExecutionOrchestrator(
            db_session,
            flow_id=flow.id,
            trigger_event_data={
                ROUTING_RECORD_KEY: {
                    "schema_version": 1,
                    "ai_model_id": str(fast.id),
                    "agent_type": "opencode",
                    "source": "rule",
                    "rule_id": "docs",
                    "label_snapshot": ["documentation"],
                }
            },
            nats_client=AsyncMock(),
        )
        orchestrator._get_flow_details()
        assert orchestrator.agent_type == "opencode"
        assert str(orchestrator.ai_model.id) == str(fast.id)

    def test_matrix_still_wins_over_routing(self, db_session: Session, test_user: User):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(db_session, test_user, ai_model_id=default.id)
        assert resolve_execution_agent_selection(
            {
                MATRIX_OVERRIDES_KEY: {
                    "agent_type": "opencode",
                    "ai_model_id": str(fast.id),
                },
                ROUTING_RECORD_KEY: {
                    "agent_type": "codex",
                    "ai_model_id": str(default.id),
                },
            },
            flow_agent_type=flow.agent_type,
            flow_ai_model_id=flow.ai_model_id,
        ) == ("opencode", str(fast.id))

    def test_native_handoff_when_identity_changes(self):
        current = ("opencode", str(FAST_MODEL))
        prior = ("codex", str(FAST_MODEL))
        assert native_handoff_required(current, prior)

    def test_prepare_does_not_read_assessment(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        details = prepare_execution_routing(
            db_session,
            flow,
            {
                "payload": {
                    "labels": ["bug"],
                    "assessment": {"issue_kind": "documentation"},
                }
            },
        )
        record = details[ROUTING_RECORD_KEY]
        assert record["source"] == "default"
        assert record["ai_model_id"] == str(default.id)

    def test_prepare_ignores_body_resume_of_valid_prior(
        self, db_session: Session, test_user: User
    ):
        default = _usable_model(db_session, test_user.account_id, name="Default")
        fast = _usable_model(db_session, test_user.account_id, name="Fast")
        flow = _flow(
            db_session,
            test_user,
            ai_model_id=default.id,
            routing=_policy(
                _rule("docs", any_labels=["documentation"], model_id=fast.id)
            ),
        )
        prior_details = prepare_execution_routing(
            db_session,
            flow,
            {"payload": {"labels": ["documentation"]}},
        )
        prior = FlowExecution(
            flow_id=flow.id,
            status="COMPLETED",
            trigger_event_details=prior_details,
        )
        db_session.add(prior)
        db_session.flush()
        details = prepare_execution_routing(
            db_session,
            flow,
            {
                "payload": {"labels": ["bug"]},
                "_resume": {"execution_id": str(prior.id)},
                MATRIX_OVERRIDES_KEY: {
                    "ai_model_id": str(fast.id),
                    "agent_type": "codex",
                },
                ROUTING_RECORD_KEY: dict(prior_details[ROUTING_RECORD_KEY]),
            },
        )
        record = details[ROUTING_RECORD_KEY]
        assert MATRIX_OVERRIDES_KEY not in details
        assert record["source"] == "default"
        assert record["ai_model_id"] == str(default.id)
