"""Read-only policy simulation using the firewall's decision cores."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_mcp_server, crud_tool_configuration
from preloop.services.model_content_policy import evaluate_model_io, load_model_io_rules
from preloop.services.policy import load_policy_from_string
from preloop.services.policy_evaluator import (
    _also_matched_rule_ids,
    _evaluate_loaded_access_rules,
    evaluate_policy_async,
)
from preloop.services.sensitive_data.policy_store import (
    detector_config_from,
    load_sensitive_data_config,
)
from preloop.services.sensitive_data.tool_policy import (
    compile_model_io_rules,
    evaluate_tool_target,
)


class DraftToolRule(BaseModel):
    """Unsaved rule with the same actions as a tool access rule."""

    action: Literal["allow", "deny", "require_approval"]
    condition_expression: str | None = Field(None, max_length=10000)
    condition_type: Literal["simple", "cel"] = "simple"
    description: str | None = Field(None, max_length=10000)
    is_enabled: bool = True


class PolicyEvaluationRequest(BaseModel):
    """One sample tool call and exactly one policy source."""

    name: str = Field(min_length=1, max_length=255)
    server: str = Field("builtin", min_length=1, max_length=255)
    args: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    draft_rule: DraftToolRule | None = None
    draft_yaml: str | None = Field(None, max_length=1000000)
    stored: bool = False
    model_target: Literal["model.request", "model.response"] = "model.request"
    model_text: str | None = Field(None, max_length=100000)

    @model_validator(mode="after")
    def validate_source(self) -> "PolicyEvaluationRequest":
        """Avoid silently choosing between conflicting sources."""
        if (
            sum([self.stored, self.draft_rule is not None, self.draft_yaml is not None])
            != 1
        ):
            raise ValueError("Provide exactly one of stored, draft_rule or draft_yaml")
        return self


class RuleCheck(BaseModel):
    """Condition result in evaluation order, including fail-closed errors."""

    id: str
    expression: str | None = None
    matched: bool
    error: str | None = None


class PolicyEvaluationResponse(BaseModel):
    """Simulation outcome; never dispatches a tool or creates an approval."""

    decision: str
    matched_rule: str | None = None
    description: str | None = None
    checked_rules: list[RuleCheck] = Field(default_factory=list)
    also_matched_rule_ids: list[str] = Field(default_factory=list)
    sensitive_data_decision: str | None = None
    model_decision: str | None = None


async def simulate_policy(
    request: PolicyEvaluationRequest,
    *,
    db: Session,
    account_id: UUID,
    user_id: UUID,
) -> PolicyEvaluationResponse:
    """Use the production matchers with recording explicitly disabled.

    A model sample is separate from tool arguments: model-I/O rules do not
    run against a serialized tool call in production either.
    """
    policy = None
    config = None
    rules: list[Any] = []
    trace: list[dict[str, Any]] = []
    context = {
        **request.context,
        "tool_name": request.name,
        "args": request.args,
        "user_id": str(user_id),
        "account_id": str(account_id),
        "execution_id": None,
        "trigger_event": {},
    }
    if request.draft_yaml is not None:
        policy, validation = load_policy_from_string(request.draft_yaml)
        if policy is None:
            raise ValueError("; ".join(error.message for error in validation.errors))
        tools = [
            t
            for t in policy.tools or []
            if t.name == request.name and t.source == request.server
        ]
        if len(tools) > 1:
            raise ValueError("More than one draft tool has this server and name")
        tool = tools[0] if tools else None
        config = models.ToolConfiguration(
            id=uuid4(),
            approval_workflow_id=tool.approval_workflow if tool else None,
        )
        for index, condition in enumerate(tool.conditions or [] if tool else []):
            rules.append(
                models.ToolAccessRule(
                    id=f"draft-{index + 1}",
                    priority=index,
                    action=condition.action,
                    condition_expression=condition.expression,
                    condition_type=condition.condition_type,
                    description=condition.description,
                    is_enabled=True,
                    approval_workflow_id=None,
                )
            )
        if tool and not tool.enabled:
            return PolicyEvaluationResponse(
                decision="deny", description="Tool disabled in draft policy"
            )
    elif request.draft_rule is not None:
        config = models.ToolConfiguration(id=uuid4(), approval_workflow_id=None)
        if request.draft_rule.is_enabled:
            rules = [
                models.ToolAccessRule(
                    id="draft-1",
                    priority=0,
                    approval_workflow_id=None,
                    **request.draft_rule.model_dump(),
                )
            ]

    sensitive_config = (
        policy.sensitive_data
        if policy
        else await run_in_threadpool(
            load_sensitive_data_config, db, account_id, strict=True
        )
        if request.stored
        else None
    )
    detector_config = detector_config_from(sensitive_config)
    sensitive = await run_in_threadpool(
        evaluate_tool_target,
        config=sensitive_config,
        detector_config=detector_config,
        target="tool.args",
        payload=request.args,
        tool_name=request.name,
        server_name=request.server,
        managed_agent_id=request.context.get("managed_agent_id"),
        record=False,
    )
    if sensitive.scan:
        from preloop.services.policy_evaluator import EXTRA_BINDINGS_KEY

        context[EXTRA_BINDINGS_KEY] = {"pii": sensitive.scan.bindings()}

    if request.stored:
        # Select the exact server's configuration rather than the first same-name tool.
        server = (
            await run_in_threadpool(
                crud_mcp_server.get_by_name,
                db,
                name=request.server,
                account_id=str(account_id),
            )
            if request.server != "builtin"
            else None
        )
        configs = await run_in_threadpool(
            crud_tool_configuration.get_multi_by_account,
            db,
            account_id=str(account_id),
            limit=100000,
        )
        selected = [
            c
            for c in configs
            if c.tool_name == request.name
            and (
                c.tool_source == "builtin"
                if request.server == "builtin"
                else server is not None and c.mcp_server_id == server.id
            )
        ]
        if len(selected) > 1:
            raise ValueError(
                "Stored tool configuration is ambiguous for this server and name"
            )
        if selected and selected[0].is_enabled is False:
            return PolicyEvaluationResponse(
                decision="deny", description="Stored tool is disabled"
            )
        decision = await evaluate_policy_async(
            db,
            tool_name=request.name,
            tool_args=request.args,
            account_id=account_id,
            user_id=user_id,
            tool_configuration_id=selected[0].id if selected else None,
            resolve_tool_configuration=bool(selected),
            subject_context=request.context,
            server_name=request.server,
            extra_bindings={"pii": sensitive.scan.bindings()}
            if sensitive.scan
            else None,
            record=False,
            trace=trace,
        )
    else:
        decision = _evaluate_loaded_access_rules(
            rules=rules,
            tool_config=config,
            tool_name=request.name,
            tool_args=request.args,
            context=context,
            account_id=account_id,
            user_id=user_id,
            execution_id=None,
            record=False,
            trace=trace,
        )
    matched = next((r["id"] for r in trace if r["matched"] or r["error"]), None)
    also = _also_matched_rule_ids(
        rules,
        start_index=next(
            (i + 1 for i, r in enumerate(rules) if str(r.id) == matched), len(rules)
        ),
        tool_args=request.args,
        context=context,
    )
    result = PolicyEvaluationResponse(
        decision=decision.action,
        matched_rule=matched,
        description=decision.rule_description,
        checked_rules=trace,
        also_matched_rule_ids=getattr(decision, "also_matched_rule_ids", also),
        sensitive_data_decision=sensitive.action,
    )
    # Blocking sensitive-data decisions precede ordinary access rules; notify/redact
    # proceed and do not override an access-rule deny or approval.
    if sensitive.action in {"deny", "require_approval"} or (
        result.decision == "allow" and sensitive.action in {"notify", "redact"}
    ):
        result.decision = sensitive.action
        result.matched_rule = sensitive.rule.id if sensitive.rule else None
    if request.model_text is not None:
        model_rules = (
            list(policy.model_io or [])
            if policy
            else await run_in_threadpool(load_model_io_rules, db, account_id)
            if request.stored
            else []
        )
        model_rules += compile_model_io_rules(
            sensitive_config, managed_agent_id=request.context.get("managed_agent_id")
        )
        ai_model = None
        if request.context.get("model"):
            ai_model = await run_in_threadpool(
                crud_ai_model.get,
                db,
                id=request.context["model"],
                account_id=str(account_id),
            )
            if ai_model is None:
                raise ValueError("Model not found in this account")
        model = await run_in_threadpool(
            evaluate_model_io,
            rules=model_rules,
            ai_model=ai_model,
            session_id=request.context.get("runtime_session_id"),
            target=request.model_target,
            text=request.model_text,
            detector_config=detector_config,
            account_id=account_id,
            user_id=user_id,
            record=False,
        )
        result.model_decision = model.action
        if result.decision in {"allow", "notify", "redact"} and model.action != "allow":
            result.decision, result.matched_rule = model.action, model.rule_id
    return result
