"""Policy evaluation service for tool access control.

This module provides the logic for determining what action to take when a tool
is executed, based on tool access rules. It supports allow/deny/require_approval
actions with priority-based rule evaluation.
"""

import functools
import logging
import re
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_approval_workflow,
    crud_tool_configuration,
    crud_tool_access_rule,
)
from preloop.models.crud.account import get_meta_data_async
from preloop.models.crud.approval_workflow import (
    get_default_approval_workflow_async,
)
from preloop.models.crud.tool_configuration import (
    get_tool_config_by_id_async,
    get_tool_config_by_tool_name_async,
)
from preloop.models.crud.tool_access_rule import get_multi_by_config_async
from preloop.services.approval_rule_context import (
    SOURCE_RULE_EVALUATION_ERROR,
    SOURCE_SUBJECT_SCOPED_RULE,
    SOURCE_TOOL_ACCESS_RULE,
    SOURCE_TOOL_DEFAULT_WORKFLOW,
    build_rule_context,
)
from preloop.services.subject_governance import (
    get_scoped_tool_rules,
    is_tool_enabled_for_subject,
)

logger = logging.getLogger(__name__)

# Cap on ``.matches()`` patterns in the simple evaluator. Python ``re`` has
# no timeout; this bound and the compiled-pattern LRU cache are the ReDoS
# mitigation (they limit compile cost and reuse compiled objects, they do
# not bound match time on a hostile pattern that still fits the cap).
SIMPLE_MATCHES_PATTERN_MAX_LEN = 512


@functools.lru_cache(maxsize=128)
def _compile_simple_matches_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a simple-evaluator ``matches()`` pattern (LRU-cached)."""
    return re.compile(pattern)


_SIMPLE_MATCHES_ESCAPES = frozenset({"\\", '"', "'"})


def _decode_simple_matches_literal(text: str) -> str:
    """Decode ``\\\\``, ``\\\"``, and ``\\\\'`` in a ``matches()`` literal.

    CEL decodes those sequences in a quoted argument. The stored text must
    mean the same in simple mode, so ``\\\\.github`` becomes ``\\.github``.
    Other backslash sequences are left unchanged.
    """
    decoded: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if (
            char == "\\"
            and index + 1 < length
            and text[index + 1] in _SIMPLE_MATCHES_ESCAPES
        ):
            decoded.append(text[index + 1])
            index += 2
            continue
        decoded.append(char)
        index += 1
    return "".join(decoded)


def _simple_matches(value: Any, pattern: str) -> bool:
    """Return whether ``pattern`` occurs in ``value`` via ``re.search``.

    Missing or non-string fields are a non-match. Patterns longer than
    :data:`SIMPLE_MATCHES_PATTERN_MAX_LEN` raise ValueError.
    """
    if not isinstance(value, str):
        return False
    if len(pattern) > SIMPLE_MATCHES_PATTERN_MAX_LEN:
        raise ValueError(
            f"matches() pattern exceeds {SIMPLE_MATCHES_PATTERN_MAX_LEN} characters"
        )
    try:
        compiled = _compile_simple_matches_pattern(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid matches() pattern: {exc}") from exc
    return compiled.search(value) is not None


class PolicyDecision(tuple):
    """A policy outcome that is still the historical 3-tuple.

    Every caller unpacks ``action, approval_workflow_id, rule_description``,
    and several tests patch the evaluator with a plain tuple, so widening the
    return type would break them silently. This subclasses ``tuple`` with
    exactly those three items and hangs the new ``rule_context`` off the side:
    old unpacking keeps working unchanged, new callers read the attribute.

    Read ``rule_context`` with ``getattr(decision, "rule_context", None)``:
    a mocked evaluator returns a bare tuple, and a missing attribute must
    degrade to "no rule context recorded" rather than raise on the approval
    path.

    Attributes:
        rule_context: JSON-serialisable snapshot of the rule that produced a
            ``require_approval`` decision, or None when nothing gated the call
            (allow/deny) or no rule identity exists.
        source: Why this decision was produced (``tool_access_rule``,
            ``subject_scoped_rule``, ``rule_evaluation_error``, ...). Set
            whenever a rule wins, including allow and deny, so callers do
            not have to read ``rule_context``.
        stored_source: For a scoped rule, the rule dict's own ``source``
            field (``agent``, ``mcp``, ...), or None when absent.
    """

    # No __slots__: CPython rejects a nonempty __slots__ on a tuple subclass,
    # so rule_context lives in the instance dict.

    def __new__(
        cls,
        action: str,
        approval_workflow_id: Optional[Any],
        rule_description: Optional[str],
        rule_context: Optional[Dict[str, Any]] = None,
        *,
        source: Optional[str] = None,
        stored_source: Optional[str] = None,
    ) -> "PolicyDecision":
        """Build the 3-tuple and attach the rule context."""
        decision = super().__new__(
            cls, (action, approval_workflow_id, rule_description)
        )
        decision.rule_context = rule_context
        if source is None and isinstance(rule_context, dict):
            raw_source = rule_context.get("source")
            source = raw_source if isinstance(raw_source, str) else None
        decision.source = source
        decision.stored_source = stored_source
        return decision

    @property
    def action(self) -> str:
        """'allow', 'deny', or 'require_approval'."""
        return self[0]

    @property
    def approval_workflow_id(self) -> Optional[Any]:
        """Workflow to raise the approval against, when gating."""
        return self[1]

    @property
    def rule_description(self) -> Optional[str]:
        """Human-readable description of what decided this."""
        return self[2]


def _also_matched_rule_ids(
    rules: list[Any],
    *,
    start_index: int,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
) -> list[str]:
    """Ids of lower-priority rules that would also have matched.

    Informational only: the winning rule already decided. Reviewers use this
    to spot overlapping rules they did not intend. Errors are swallowed
    because a broken lower-priority rule must not affect a decision that has
    already been made by a higher-priority one.

    Args:
        rules: The full priority-ordered rule list.
        start_index: Index just past the winning rule.
        tool_args: Arguments the call was made with.
        context: Evaluation context.

    Returns:
        Rule ids as strings, in priority order. Empty when none also matched.
    """
    also: list[str] = []
    for rule in rules[start_index:]:
        try:
            if _evaluate_rule_condition(
                expression=rule.condition_expression,
                condition_type=rule.condition_type,
                tool_args=tool_args,
                context=context,
            ):
                also.append(str(rule.id))
        except Exception:  # pragma: no cover - best effort annotation only
            continue
    return also


def _matched_rule_context(
    rule: Any,
    *,
    rules: list[Any],
    index: int,
    tool_config: Any,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Snapshot of the rule that gated a call, or None when it did not gate.

    Shared by the sync and async evaluators so the two cannot drift into
    recording different things about the same rule. Only ``require_approval``
    produces a snapshot: allow and deny create no approval to attach one to.

    Args:
        rule: The winning rule.
        rules: The full priority-ordered rule list.
        index: Index of the winning rule within ``rules``.
        tool_config: The tool configuration the rules belong to.
        tool_args: Arguments the call was made with.
        context: Evaluation context.

    Returns:
        The snapshot dict, or None when the action is not require_approval.
    """
    if rule.action != "require_approval":
        return None
    return build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision=rule.action,
        rule_id=rule.id,
        rule_name=rule.description,
        expression=rule.condition_expression,
        expression_type=rule.condition_type,
        priority=rule.priority,
        tool_configuration_id=tool_config.id,
        also_matched_rule_ids=_also_matched_rule_ids(
            rules,
            start_index=index + 1,
            tool_args=tool_args,
            context=context,
        ),
    )


def _get_audit_service():
    """Get the audit service instance (lazy import to avoid circular deps)."""
    try:
        from plugins.audit.service import get_audit_service

        return get_audit_service()
    except ImportError:
        logger.debug("Audit service not available")
        return None


def _get_db_factory():
    """Get a database session factory for async audit logging."""
    try:
        from preloop.models.db.session import get_session_factory

        def _create_session():
            """Create a session that the caller is responsible for closing."""
            factory = get_session_factory()
            return factory()

        return _create_session
    except ImportError:
        return None


def _log_policy_decision_async(
    account_id: uuid.UUID,
    tool_name: str,
    action: str,
    rule_description: Optional[str] = None,
    condition_matched: Optional[str] = None,
    tool_args: Optional[Dict[str, Any]] = None,
    user_id: Optional[uuid.UUID] = None,
    execution_id: Optional[uuid.UUID] = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a policy decision asynchronously (fire-and-forget).

    This helper function wraps the audit service call to log policy decisions
    without blocking the main execution flow.

    Args:
        account_id: Account ID
        tool_name: Name of the tool being evaluated
        action: Policy decision ('allow', 'deny', 'require_approval')
        rule_description: Description of the rule that matched
        condition_matched: The condition expression that matched
        tool_args: Tool arguments that were evaluated
        user_id: User ID (if available)
        execution_id: Flow execution ID (if applicable)
        correlation_id: Correlation ID for grouping related audit events
    """
    try:
        audit_service = _get_audit_service()
        if not audit_service:
            return

        db_factory = _get_db_factory()
        if not db_factory:
            return

        audit_service.log_policy_decision_async(
            db_factory=db_factory,
            account_id=account_id,
            tool_name=tool_name,
            action=action,
            rule_description=rule_description,
            condition_matched=condition_matched,
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
    except Exception as e:
        logger.debug(f"Failed to log policy decision to audit: {e}")


def _evaluate_rule_candidates(
    *,
    rules: list[Any],
    tool_name: str,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
    account_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    execution_id: Optional[uuid.UUID],
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
    default_approval_workflow_id: Optional[Any] = None,
) -> Optional[PolicyDecision]:
    for index, rule in enumerate(rules):
        is_enabled = (
            rule.get("is_enabled", True) if isinstance(rule, dict) else rule.is_enabled
        )
        if not is_enabled:
            continue
        condition_expression = (
            rule.get("condition_expression")
            if isinstance(rule, dict)
            else rule.condition_expression
        )
        condition_type = (
            rule.get("condition_type", "simple")
            if isinstance(rule, dict)
            else rule.condition_type
        )
        action = rule.get("action") if isinstance(rule, dict) else rule.action
        approval_workflow_id = (
            rule.get("approval_workflow_id")
            if isinstance(rule, dict)
            else rule.approval_workflow_id
        ) or default_approval_workflow_id
        description = (
            rule.get("description") if isinstance(rule, dict) else rule.description
        )
        try:
            matches = _evaluate_rule_condition(
                expression=condition_expression,
                condition_type=str(condition_type or "simple"),
                tool_args=tool_args,
                context=context,
            )
            if not matches:
                continue
            rule_desc = (
                description or condition_expression or f"Scoped rule {index + 1}"
            )
            _log_policy_decision_async(
                account_id=account_id,
                tool_name=tool_name,
                action=action,
                rule_description=rule_desc,
                condition_matched=condition_expression,
                tool_args=tool_args,
                user_id=user_id,
                execution_id=execution_id,
                correlation_id=correlation_id,
                extra_details=extra_details,
            )
            rule_context = None
            if action == "require_approval":
                rule_context = build_rule_context(
                    source=SOURCE_SUBJECT_SCOPED_RULE,
                    decision=action,
                    rule_id=(
                        rule.get("id")
                        if isinstance(rule, dict)
                        else getattr(rule, "id", None)
                    ),
                    rule_name=description,
                    expression=condition_expression,
                    expression_type=str(condition_type or "simple"),
                    priority=(
                        rule.get("priority")
                        if isinstance(rule, dict)
                        else getattr(rule, "priority", None)
                    ),
                )
            stored_source = None
            if isinstance(rule, dict):
                raw_source = rule.get("source")
                if isinstance(raw_source, str) and raw_source:
                    stored_source = raw_source
            return PolicyDecision(
                action,
                approval_workflow_id,
                rule_desc,
                rule_context,
                source=SOURCE_SUBJECT_SCOPED_RULE,
                stored_source=stored_source,
            )
        except Exception as e:
            error_desc = f"Rule evaluation error: {e} (failing closed)"
            _log_policy_decision_async(
                account_id=account_id,
                tool_name=tool_name,
                action="require_approval",
                rule_description=error_desc,
                condition_matched=condition_expression,
                tool_args=tool_args,
                user_id=user_id,
                execution_id=execution_id,
                correlation_id=correlation_id,
                extra_details=extra_details,
            )
            return PolicyDecision(
                "require_approval",
                approval_workflow_id,
                error_desc,
                build_rule_context(
                    source=SOURCE_RULE_EVALUATION_ERROR,
                    decision="require_approval",
                    rule_name=description or None,
                    expression=condition_expression,
                    expression_type=str(condition_type or "simple"),
                    explanation=(
                        "A scoped governance rule could not be evaluated, so "
                        "Preloop failed closed and asked for approval instead "
                        "of deciding on its own."
                    ),
                ),
            )
    return None


def _evaluate_loaded_access_rules(
    *,
    rules: list[Any],
    tool_config: Any,
    tool_name: str,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
    account_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    execution_id: Optional[uuid.UUID],
    default_workflow_id_for_account: Optional[Any] = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    """Evaluate already-loaded ToolAccessRule rows.

    Shared by the sync and async evaluators so match order, fail-closed,
    and the legacy no-rules path cannot drift. Callers keep their own I/O.
    """
    logger.info(
        f"Policy evaluation for '{tool_name}': found {len(rules)} access rules, "
        f"tool_config.approval_workflow_id={tool_config.approval_workflow_id}"
    )

    if not rules:
        if tool_config.approval_workflow_id:
            logger.warning(
                f"LEGACY PATH: tool '{tool_name}' has approval_workflow_id="
                f"{tool_config.approval_workflow_id} but no access rules. "
                f"ALL calls will require approval regardless of arguments. "
                f"Add access rules with conditions to enable conditional approval."
            )
            _log_policy_decision_async(
                account_id=account_id,
                tool_name=tool_name,
                action="require_approval",
                rule_description="Tool has approval workflow configured (legacy mode)",
                tool_args=tool_args,
                user_id=user_id,
                execution_id=execution_id,
                correlation_id=correlation_id,
                extra_details=extra_details,
            )
            return PolicyDecision(
                "require_approval",
                tool_config.approval_workflow_id,
                "Tool has approval workflow configured (legacy mode)",
                build_rule_context(
                    source=SOURCE_TOOL_DEFAULT_WORKFLOW,
                    decision="require_approval",
                    rule_name="Tool default policy",
                    tool_configuration_id=tool_config.id,
                ),
            )
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No access rules defined",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
        return PolicyDecision("allow", None, "No access rules defined")

    for index, rule in enumerate(rules):
        try:
            logger.info(
                f"Evaluating rule {rule.id} (priority={rule.priority}, "
                f"action={rule.action}): condition_type={rule.condition_type}, "
                f"expression={rule.condition_expression!r}, args={tool_args}"
            )
            matches = _evaluate_rule_condition(
                expression=rule.condition_expression,
                condition_type=rule.condition_type,
                tool_args=tool_args,
                context=context,
            )
            logger.info(f"Rule {rule.id} evaluated: matches={matches}")

            if matches:
                logger.info(
                    f"Rule matched: {rule.description or rule.condition_expression} "
                    f"-> action={rule.action}"
                )

                approval_workflow_id = None
                if rule.action == "require_approval":
                    approval_workflow_id = (
                        rule.approval_workflow_id
                        or tool_config.approval_workflow_id
                        or default_workflow_id_for_account
                    )

                rule_desc = (
                    rule.description or f"Rule matched: {rule.condition_expression}"
                )

                _log_policy_decision_async(
                    account_id=account_id,
                    tool_name=tool_name,
                    action=rule.action,
                    rule_description=rule_desc,
                    condition_matched=rule.condition_expression,
                    tool_args=tool_args,
                    user_id=user_id,
                    execution_id=execution_id,
                    correlation_id=correlation_id,
                    extra_details=extra_details,
                )

                rule_context = _matched_rule_context(
                    rule,
                    rules=rules,
                    index=index,
                    tool_config=tool_config,
                    tool_args=tool_args,
                    context=context,
                )

                return PolicyDecision(
                    rule.action,
                    approval_workflow_id,
                    rule_desc,
                    rule_context,
                    source=SOURCE_TOOL_ACCESS_RULE,
                )

        except Exception as e:
            logger.error(
                f"Error evaluating rule {rule.id}: {e}. "
                f"Failing closed with require_approval for security."
            )
            error_desc = f"Rule evaluation error: {e} (failing closed)"
            _log_policy_decision_async(
                account_id=account_id,
                tool_name=tool_name,
                action="require_approval",
                rule_description=error_desc,
                condition_matched=rule.condition_expression,
                tool_args=tool_args,
                user_id=user_id,
                execution_id=execution_id,
                correlation_id=correlation_id,
                extra_details=extra_details,
            )
            return PolicyDecision(
                "require_approval",
                tool_config.approval_workflow_id or default_workflow_id_for_account,
                error_desc,
                build_rule_context(
                    source=SOURCE_RULE_EVALUATION_ERROR,
                    decision="require_approval",
                    rule_id=rule.id,
                    rule_name=rule.description,
                    expression=rule.condition_expression,
                    expression_type=rule.condition_type,
                    priority=rule.priority,
                    tool_configuration_id=tool_config.id,
                ),
            )

    _log_policy_decision_async(
        account_id=account_id,
        tool_name=tool_name,
        action="allow",
        rule_description="No rules matched (default allow)",
        tool_args=tool_args,
        user_id=user_id,
        execution_id=execution_id,
        correlation_id=correlation_id,
        extra_details=extra_details,
    )
    return PolicyDecision("allow", None, "No rules matched (default allow)")


def evaluate_policy(
    db: Session,
    tool_name: str,
    tool_args: Dict[str, Any],
    account_id: uuid.UUID,
    tool_configuration_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    execution_id: Optional[uuid.UUID] = None,
    trigger_event: Optional[Dict[str, Any]] = None,
    subject_context: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    """Evaluate tool access policy and determine the action to take.

    This function implements the policy evaluation logic:
    1. Find the tool configuration
    2. Load all ToolAccessRule records for the tool, ordered by priority (lower first)
    3. For each enabled rule, evaluate the condition
    4. Return the action of the first matching rule
    5. If no rules match, return 'allow' (default allow)

    Args:
        db: Database session.
        tool_name: Name of the tool being executed.
        tool_args: Arguments passed to the tool.
        account_id: Account ID.
        tool_configuration_id: Optional tool configuration ID (for lookup).
        user_id: Optional user ID (for condition evaluation context).
        execution_id: Optional execution ID (for condition evaluation context).
        trigger_event: Optional trigger event data (for condition evaluation context).

    Returns:
        A :class:`PolicyDecision`, which unpacks as the historical 3-tuple
        (action, approval_workflow_id, matched_rule_description) and also
        carries ``.rule_context`` describing the rule that gated the call.
        - action: 'allow', 'deny', or 'require_approval'
        - approval_workflow_id: Policy ID to use if action is 'require_approval'
        - matched_rule_description: Description of the matched rule (or reason for default)
        - rule_context: Snapshot of the winning rule for 'require_approval'
          decisions, else None. Persisted on the approval request so the
          approver can see WHICH rule fired and what its expression was.
    """
    account = crud_account.get(db, id=account_id)
    account_meta = (account.meta_data or {}) if account else {}

    if not is_tool_enabled_for_subject(
        account_meta, tool_name=tool_name, subject_context=subject_context or {}
    ):
        return PolicyDecision(
            "deny", None, "Tool disabled by agent or API key configuration"
        )

    scoped_rules = get_scoped_tool_rules(
        account_meta,
        tool_name=tool_name,
        subject_context=subject_context or {},
    )

    # Get tool configuration
    if tool_configuration_id:
        tool_config = crud_tool_configuration.get(
            db, id=tool_configuration_id, account_id=account_id
        )
    else:
        tool_config = crud_tool_configuration.get_by_tool_name(
            db, account_id=account_id, tool_name=tool_name
        )

    # Resolve the account's default approval workflow (if any) up front so it
    # can serve as the implicit fallback for ``require_approval`` rules that
    # don't pin a specific workflow.
    default_workflow = crud_approval_workflow.get_default(db, account_id=account_id)
    default_workflow_id_for_account = default_workflow.id if default_workflow else None

    context = {
        "tool_name": tool_name,
        "args": tool_args,
        "user_id": str(user_id) if user_id else None,
        "account_id": str(account_id),
        "execution_id": str(execution_id) if execution_id else None,
        "trigger_event": trigger_event or {},
        "api_key_id": (subject_context or {}).get("api_key_id"),
        "managed_agent_id": (subject_context or {}).get("managed_agent_id"),
        "runtime_session_id": (subject_context or {}).get("runtime_session_id"),
        "runtime_principal_type": (subject_context or {}).get("runtime_principal_type"),
        "runtime_principal_id": (subject_context or {}).get("runtime_principal_id"),
        "runtime_principal_name": (subject_context or {}).get("runtime_principal_name"),
    }
    scoped_decision = _evaluate_rule_candidates(
        rules=scoped_rules,
        tool_name=tool_name,
        tool_args=tool_args,
        context=context,
        account_id=account_id,
        user_id=user_id,
        execution_id=execution_id,
        default_approval_workflow_id=(
            (tool_config.approval_workflow_id if tool_config else None)
            or default_workflow_id_for_account
        ),
    )
    if scoped_decision is not None:
        return scoped_decision

    if scoped_rules:
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No scoped rules matched (default allow for subject)",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
        )
        return PolicyDecision(
            "allow", None, "No scoped rules matched (default allow for subject)"
        )

    if not tool_config:
        # No configuration found, default allow
        # Log the policy decision (fire-and-forget)
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No tool configuration found",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
        )
        return PolicyDecision("allow", None, "No tool configuration found")

    # Load all access rules for this tool, ordered by priority (lower first)
    rules = crud_tool_access_rule.get_multi_by_config(
        db,
        config_id=tool_config.id,
        account_id=account_id,
        enabled_only=True,
    )

    return _evaluate_loaded_access_rules(
        rules=rules,
        tool_config=tool_config,
        tool_name=tool_name,
        tool_args=tool_args,
        context=context,
        account_id=account_id,
        user_id=user_id,
        execution_id=execution_id,
        default_workflow_id_for_account=default_workflow_id_for_account,
    )


def _evaluate_rule_condition(
    expression: Optional[str],
    condition_type: str,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
) -> bool:
    """Evaluate a rule condition.

    Args:
        expression: The condition expression to evaluate.
        condition_type: Type of condition ('simple' or 'cel').
        tool_args: Tool arguments to evaluate against.
        context: Additional context for evaluation.

    Returns:
        True if condition matches, False otherwise.
    """
    if not expression:
        # No expression means always match (catch-all rule)
        return True

    if condition_type == "simple":
        return _evaluate_simple_condition(expression, tool_args)
    elif condition_type == "cel":
        return _evaluate_cel_condition(expression, tool_args, context)
    else:
        raise ValueError(f"Unknown condition type: {condition_type}")


def _evaluate_simple_condition(expression: str, tool_args: Dict[str, Any]) -> bool:
    """Evaluate a simple condition expression.

    Supported expressions:
        - args.field == 'value'
        - args.field != 'value'
        - args.field > number
        - args.field < number
        - args.field >= number
        - args.field <= number
        - args.field.contains('substring')
        - args.field.matches('regex')

    ``.matches()`` uses Python ``re.search``. Python's ``re`` has no
    timeout. The 512-character pattern cap and the compiled-pattern LRU
    cache are the ReDoS mitigation: they bound compile cost and reuse
    compiled objects. They do not bound match time on a hostile pattern
    that still fits the cap. Oversized patterns raise ValueError rather
    than evaluating.

    Args:
        expression: Simple condition expression.
        tool_args: Tool arguments to evaluate against.

    Returns:
        True if condition matches, False otherwise.

    Raises:
        ValueError: If expression is invalid or unsupported.
    """
    expression = expression.strip()

    # Handle boolean literals before any normalisation. Users configure
    # catch-all rules with a bare 'true' (or 'false'); prepending 'args.'
    # would turn these into unparsable field references.
    lowered = expression.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    # Normalise: if the expression doesn't start with 'args.', prepend it.
    # Users often configure rules via the UI with just the field name, e.g.
    # "amount > 300" instead of "args.amount > 300".
    if not expression.startswith("args."):
        expression = f"args.{expression}"

    # Handle .contains() method
    contains_pattern = r"^args\.(\w+(?:\.\w+)*)\.contains\s*\(\s*['\"](.+?)['\"]\s*\)$"
    contains_match = re.match(contains_pattern, expression)
    if contains_match:
        field_path = contains_match.group(1)
        substring = contains_match.group(2)
        value = _get_nested_value(tool_args, field_path)
        if value is None:
            return False
        if isinstance(value, str):
            return substring in value
        elif isinstance(value, list):
            return substring in value
        return False

    matches_pattern = (
        r"^args\.(\w+(?:\.\w+)*)\.matches\s*\(\s*(['\"])"
        r"((?:\\.|(?!\2).)*)\2\s*\)$"
    )
    matches_match = re.match(matches_pattern, expression)
    if matches_match:
        field_path = matches_match.group(1)
        pattern = _decode_simple_matches_literal(matches_match.group(3))
        value = _get_nested_value(tool_args, field_path)
        return _simple_matches(value, pattern)

    # Handle comparison operators
    # Order matters: >= and <= must be checked before > and <
    comparison_pattern = r"^args\.(\w+(?:\.\w+)*)\s*(==|!=|>=|<=|>|<)\s*(.+)$"
    comparison_match = re.match(comparison_pattern, expression)
    if comparison_match:
        field_path = comparison_match.group(1)
        operator = comparison_match.group(2)
        raw_value = comparison_match.group(3).strip()

        # Parse the right-hand value
        rhs_value = _parse_value(raw_value)

        # Get the left-hand value from args
        lhs_value = _get_nested_value(tool_args, field_path)

        return _compare_values(lhs_value, operator, rhs_value)

    raise ValueError(f"Unsupported simple expression format: {expression}")


def _get_nested_value(data: Dict[str, Any], path: str) -> Any:
    """Get a nested value from a dictionary using dot notation.

    Args:
        data: Dictionary to search.
        path: Dot-separated path (e.g., 'nested.field.value').

    Returns:
        The value at the path, or None if not found.
    """
    parts = path.split(".")
    current = data

    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    return current


def _parse_value(raw_value: str) -> Any:
    """Parse a raw value string into a Python value.

    Args:
        raw_value: String representation of a value.

    Returns:
        Parsed value (string, int, float, bool, or None).
    """
    # Strip whitespace
    raw_value = raw_value.strip()

    # Check for quoted string
    if (raw_value.startswith("'") and raw_value.endswith("'")) or (
        raw_value.startswith('"') and raw_value.endswith('"')
    ):
        return raw_value[1:-1]

    # Check for boolean
    if raw_value.lower() == "true":
        return True
    if raw_value.lower() == "false":
        return False

    # Check for null/None
    if raw_value.lower() in ("null", "none"):
        return None

    # Try to parse as number
    try:
        if "." in raw_value:
            return float(raw_value)
        return int(raw_value)
    except ValueError:
        pass

    # Return as string if nothing else matches
    return raw_value


def _compare_values(lhs: Any, operator: str, rhs: Any) -> bool:
    """Compare two values with the given operator.

    Args:
        lhs: Left-hand side value.
        operator: Comparison operator.
        rhs: Right-hand side value.

    Returns:
        Result of the comparison.
    """
    if lhs is None:
        # None comparisons
        if operator == "==":
            return rhs is None
        elif operator == "!=":
            return rhs is not None
        return False

    # For ordering comparisons, coerce numeric-looking operands to numbers.
    # SECURITY: without this, an agent could defeat a numeric rule such as
    # ``args.amount > 300`` by sending ``amount`` as the *string* "1000000":
    # ``"1000000" > 300`` raises TypeError, which was swallowed to a
    # non-match and fell through to the default-allow, skipping the intended
    # approval/deny. Coercing keeps the comparison meaningful across the JSON
    # string/number ambiguity.
    if operator in (">", "<", ">=", "<="):
        lhs_num = _coerce_number(lhs)
        rhs_num = _coerce_number(rhs)
        if lhs_num is not None and rhs_num is not None:
            lhs, rhs = lhs_num, rhs_num

    try:
        if operator == "==":
            return lhs == rhs
        elif operator == "!=":
            return lhs != rhs
        elif operator == ">":
            return lhs > rhs
        elif operator == "<":
            return lhs < rhs
        elif operator == ">=":
            return lhs >= rhs
        elif operator == "<=":
            return lhs <= rhs
        else:
            raise ValueError(f"Unknown operator: {operator}")
    except TypeError:
        # Type mismatch in comparison
        return False


def _coerce_number(value: Any) -> Optional[float]:
    """Return ``value`` as a float if it is numeric or a numeric string.

    Bools are intentionally excluded (``True`` is not a quantity here) and
    non-numeric strings return ``None`` so the caller keeps the original
    values.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def evaluate_condition_against_bindings(
    expression: str,
    condition_type: str,
    bindings: Dict[str, Any],
) -> bool:
    """Evaluate a simple or CEL condition against a named binding dict.

    Used by model I/O rules so CEL can write ``pii.found`` and
    ``injection.score`` instead of wrapping everything under ``args``.
    Tool evaluation still uses ``_evaluate_simple_condition`` /
    ``_evaluate_cel_condition`` and is unchanged.

    Args:
        expression: Condition expression.
        condition_type: ``simple`` or ``cel``.
        bindings: Root attributes (model, session, request, pii, ...).

    Returns:
        True when the condition matches.

    Raises:
        ValueError: If the expression or condition type is invalid.
    """
    if not expression or not expression.strip():
        return True
    if condition_type == "simple":
        return _evaluate_simple_condition_on_bindings(expression, bindings)
    if condition_type == "cel":
        return _evaluate_cel_condition_on_bindings(expression, bindings)
    raise ValueError(f"Unknown condition type: {condition_type}")


def _evaluate_simple_condition_on_bindings(
    expression: str, bindings: Dict[str, Any]
) -> bool:
    """Evaluate a simple comparison against a root binding dict."""
    expression = expression.strip()
    lowered = expression.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    contains_pattern = r"^(\w+(?:\.\w+)*)\.contains\s*\(\s*['\"](.+?)['\"]\s*\)$"
    contains_match = re.match(contains_pattern, expression)
    if contains_match:
        field_path = contains_match.group(1)
        substring = contains_match.group(2)
        value = _get_nested_value(bindings, field_path)
        if value is None:
            return False
        if isinstance(value, str):
            return substring in value
        if isinstance(value, list):
            return substring in value
        return False

    matches_pattern = (
        r"^(\w+(?:\.\w+)*)\.matches\s*\(\s*(['\"])((?:\\.|(?!\2).)*)\2\s*\)$"
    )
    matches_match = re.match(matches_pattern, expression)
    if matches_match:
        field_path = matches_match.group(1)
        pattern = _decode_simple_matches_literal(matches_match.group(3))
        value = _get_nested_value(bindings, field_path)
        return _simple_matches(value, pattern)

    comparison_pattern = r"^(\w+(?:\.\w+)*)\s*(==|!=|>=|<=|>|<)\s*(.+)$"
    comparison_match = re.match(comparison_pattern, expression)
    if comparison_match:
        field_path = comparison_match.group(1)
        operator = comparison_match.group(2)
        raw_value = comparison_match.group(3).strip()
        rhs_value = _parse_value(raw_value)
        lhs_value = _get_nested_value(bindings, field_path)
        return _compare_values(lhs_value, operator, rhs_value)

    raise ValueError(f"Unsupported simple expression format: {expression}")


def _evaluate_cel_condition_on_bindings(
    expression: str, bindings: Dict[str, Any]
) -> bool:
    """Evaluate a CEL expression against a root binding dict."""
    try:
        import celpy

        env = celpy.Environment()
        ast = env.compile(expression)
        program = env.program(ast)
        activation = celpy.json_to_cel(bindings)
        result = program.evaluate(activation)
        return bool(result)
    except Exception as e:
        logger.error(f"CEL evaluation error for expression '{expression}': {e}")
        raise ValueError(f"CEL evaluation failed: {str(e)}")


def _evaluate_cel_condition(
    expression: str,
    tool_args: Dict[str, Any],
    context: Dict[str, Any],
) -> bool:
    """Evaluate a CEL condition expression.

    This uses the CEL (Common Expression Language) evaluator for more
    complex expressions. This is intended for enterprise users who need
    advanced condition logic.

    Args:
        expression: CEL expression to evaluate.
        tool_args: Tool arguments to evaluate against.
        context: Additional context for evaluation.

    Returns:
        True if condition matches, False otherwise.
    """
    try:
        import celpy

        # Create CEL environment
        env = celpy.Environment()
        ast = env.compile(expression)
        program = env.program(ast)

        # Evaluate with tool arguments (convert to CEL types)
        activation = celpy.json_to_cel({"args": tool_args})
        result = program.evaluate(activation)

        return bool(result)

    except Exception as e:
        logger.error(f"CEL evaluation error for expression '{expression}': {e}")
        raise ValueError(f"CEL evaluation failed: {str(e)}")


def evaluate_cel_expression(expression: str, tool_args: Dict[str, Any]) -> bool:
    """Evaluate a CEL expression for testing purposes.

    This is a simplified version that's used by the test endpoint
    to validate CEL expressions before saving them.

    Args:
        expression: CEL expression to evaluate.
        tool_args: Sample tool arguments to test against.

    Returns:
        True if expression matches, False otherwise.

    Raises:
        Exception: If expression is invalid or evaluation fails.
    """
    return _evaluate_cel_condition(expression, tool_args, {})


def evaluate_simple_expression(expression: str, tool_args: Dict[str, Any]) -> bool:
    """Evaluate a simple expression for testing purposes.

    This is used by the test endpoint to validate simple expressions
    before saving them.

    Args:
        expression: Simple expression to evaluate.
        tool_args: Sample tool arguments to test against.

    Returns:
        True if expression matches, False otherwise.

    Raises:
        ValueError: If expression is invalid or unsupported.
    """
    return _evaluate_simple_condition(expression, tool_args)


# Async version for use with async database sessions
async def evaluate_policy_async(
    db,  # AsyncSession
    tool_name: str,
    tool_args: Dict[str, Any],
    account_id: uuid.UUID,
    tool_configuration_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    execution_id: Optional[uuid.UUID] = None,
    trigger_event: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
    subject_context: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    """Async version of evaluate_policy.

    See evaluate_policy for full documentation.
    """
    account_meta_data = await get_meta_data_async(db, account_id=str(account_id))

    if not is_tool_enabled_for_subject(
        account_meta_data, tool_name=tool_name, subject_context=subject_context or {}
    ):
        return PolicyDecision(
            "deny", None, "Tool disabled by agent or API key configuration"
        )

    scoped_rules = get_scoped_tool_rules(
        account_meta_data,
        tool_name=tool_name,
        subject_context=subject_context or {},
    )

    # Get tool configuration
    if tool_configuration_id:
        tool_config = await get_tool_config_by_id_async(
            db, id=tool_configuration_id, account_id=account_id
        )
    else:
        tool_config = await get_tool_config_by_tool_name_async(
            db, account_id=account_id, tool_name=tool_name
        )

    # Resolve the account's default approval workflow (if any) up front so it
    # can serve as the implicit fallback for ``require_approval`` rules that
    # don't pin a specific workflow. Without this fallback the system would
    # silently auto-approve, which is the opposite of the user's intent.
    default_workflow = await get_default_approval_workflow_async(
        db, account_id=account_id
    )
    default_workflow_id_for_account = default_workflow.id if default_workflow else None

    context = {
        "tool_name": tool_name,
        "args": tool_args,
        "user_id": str(user_id) if user_id else None,
        "account_id": str(account_id),
        "execution_id": str(execution_id) if execution_id else None,
        "trigger_event": trigger_event or {},
        "api_key_id": (subject_context or {}).get("api_key_id"),
        "managed_agent_id": (subject_context or {}).get("managed_agent_id"),
        "runtime_session_id": (subject_context or {}).get("runtime_session_id"),
        "runtime_principal_type": (subject_context or {}).get("runtime_principal_type"),
        "runtime_principal_id": (subject_context or {}).get("runtime_principal_id"),
        "runtime_principal_name": (subject_context or {}).get("runtime_principal_name"),
    }
    scoped_decision = _evaluate_rule_candidates(
        rules=scoped_rules,
        tool_name=tool_name,
        tool_args=tool_args,
        context=context,
        account_id=account_id,
        user_id=user_id,
        execution_id=execution_id,
        correlation_id=correlation_id,
        extra_details=extra_details,
        default_approval_workflow_id=(
            (tool_config.approval_workflow_id if tool_config else None)
            or default_workflow_id_for_account
        ),
    )
    if scoped_decision is not None:
        return scoped_decision

    if scoped_rules:
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No scoped rules matched (default allow for subject)",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
        return PolicyDecision(
            "allow", None, "No scoped rules matched (default allow for subject)"
        )

    if not tool_config:
        # Log the policy decision (fire-and-forget)
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No tool configuration found",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
        return PolicyDecision("allow", None, "No tool configuration found")

    # Load all access rules for this tool, ordered by priority (lower first)
    rules = await get_multi_by_config_async(
        db,
        config_id=tool_config.id,
        account_id=account_id,
        enabled_only=True,
    )

    return _evaluate_loaded_access_rules(
        rules=rules,
        tool_config=tool_config,
        tool_name=tool_name,
        tool_args=tool_args,
        context=context,
        account_id=account_id,
        user_id=user_id,
        execution_id=execution_id,
        default_workflow_id_for_account=default_workflow_id_for_account,
        correlation_id=correlation_id,
        extra_details=extra_details,
    )
