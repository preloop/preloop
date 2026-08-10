"""Policy evaluation service for tool access control.

This module provides the logic for determining what action to take when a tool
is executed, based on tool access rules. It supports allow/deny/require_approval
actions with priority-based rule evaluation.
"""

import logging
import re
import uuid
from typing import Any, Dict, Optional, Tuple

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
from preloop.services.subject_governance import (
    get_scoped_tool_rules,
    is_tool_enabled_for_subject,
)

logger = logging.getLogger(__name__)


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
) -> Optional[Tuple[str, Optional[Any], Optional[str]]]:
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
            return action, approval_workflow_id, rule_desc
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
            return "require_approval", approval_workflow_id, error_desc
    return None


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
) -> Tuple[str, Optional[uuid.UUID], Optional[str]]:
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
        Tuple of (action, approval_workflow_id, matched_rule_description).
        - action: 'allow', 'deny', or 'require_approval'
        - approval_workflow_id: Policy ID to use if action is 'require_approval'
        - matched_rule_description: Description of the matched rule (or reason for default)
    """
    account = crud_account.get(db, id=account_id)
    account_meta = (account.meta_data or {}) if account else {}

    if not is_tool_enabled_for_subject(
        account_meta, tool_name=tool_name, subject_context=subject_context or {}
    ):
        return "deny", None, "Tool disabled by agent or API key configuration"

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
        return "allow", None, "No scoped rules matched (default allow for subject)"

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
        return "allow", None, "No tool configuration found"

    # Load all access rules for this tool, ordered by priority (lower first)
    rules = crud_tool_access_rule.get_multi_by_config(
        db,
        config_id=tool_config.id,
        account_id=account_id,
        enabled_only=True,
    )

    logger.info(
        f"Policy evaluation for '{tool_name}': found {len(rules)} access rules, "
        f"tool_config.approval_workflow_id={tool_config.approval_workflow_id}"
    )

    if not rules:
        # No rules defined, check for legacy approval_workflow_id on tool config
        if tool_config.approval_workflow_id:
            # Legacy behavior: tool has approval workflow but no rules
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
            )
            return (
                "require_approval",
                tool_config.approval_workflow_id,
                "Tool has approval workflow configured (legacy mode)",
            )
        _log_policy_decision_async(
            account_id=account_id,
            tool_name=tool_name,
            action="allow",
            rule_description="No access rules defined",
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
        )
        return "allow", None, "No access rules defined"

    # Evaluate rules in priority order
    for rule in rules:
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

                # Determine approval workflow ID for require_approval action
                approval_workflow_id = None
                if rule.action == "require_approval":
                    # Prefer the rule-level policy; fall back to tool config
                    # default; finally fall back to the account default so a
                    # ``require_approval`` rule never silently auto-approves.
                    approval_workflow_id = (
                        rule.approval_workflow_id
                        or tool_config.approval_workflow_id
                        or default_workflow_id_for_account
                    )

                rule_desc = (
                    rule.description or f"Rule matched: {rule.condition_expression}"
                )

                # Log the policy decision (fire-and-forget)
                _log_policy_decision_async(
                    account_id=account_id,
                    tool_name=tool_name,
                    action=rule.action,
                    rule_description=rule_desc,
                    condition_matched=rule.condition_expression,
                    tool_args=tool_args,
                    user_id=user_id,
                    execution_id=execution_id,
                )

                return (
                    rule.action,
                    approval_workflow_id,
                    rule_desc,
                )

        except Exception as e:
            # SECURITY: Fail closed on evaluation errors
            # Rather than skipping to the next rule (which could lead to default-allow),
            # we require approval when rule evaluation fails. This ensures that:
            # 1. Malformed rules don't silently get bypassed
            # 2. Unexpected errors don't result in unauthorized access
            # 3. The action is conservative (require human review) rather than permissive
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
            )
            return (
                "require_approval",
                tool_config.approval_workflow_id or default_workflow_id_for_account,
                error_desc,
            )

    # No rules matched, default allow
    _log_policy_decision_async(
        account_id=account_id,
        tool_name=tool_name,
        action="allow",
        rule_description="No rules matched (default allow)",
        tool_args=tool_args,
        user_id=user_id,
        execution_id=execution_id,
    )
    return "allow", None, "No rules matched (default allow)"


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
) -> Tuple[str, Optional[uuid.UUID], Optional[str]]:
    """Async version of evaluate_policy.

    See evaluate_policy for full documentation.
    """
    account_meta_data = await get_meta_data_async(db, account_id=str(account_id))

    if not is_tool_enabled_for_subject(
        account_meta_data, tool_name=tool_name, subject_context=subject_context or {}
    ):
        return "deny", None, "Tool disabled by agent or API key configuration"

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
        return "allow", None, "No scoped rules matched (default allow for subject)"

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
        return "allow", None, "No tool configuration found"

    # Load all access rules for this tool, ordered by priority (lower first)
    rules = await get_multi_by_config_async(
        db,
        config_id=tool_config.id,
        account_id=account_id,
        enabled_only=True,
    )

    if not rules:
        # No rules defined, check for legacy approval_workflow_id on tool config
        if tool_config.approval_workflow_id:
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
            return (
                "require_approval",
                tool_config.approval_workflow_id,
                "Tool has approval workflow configured (legacy mode)",
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
        return "allow", None, "No access rules defined"

    # Evaluate rules in priority order
    for rule in rules:
        try:
            matches = _evaluate_rule_condition(
                expression=rule.condition_expression,
                condition_type=rule.condition_type,
                tool_args=tool_args,
                context=context,
            )

            if matches:
                logger.info(
                    f"Rule matched: {rule.description or rule.condition_expression} "
                    f"-> action={rule.action}"
                )

                approval_workflow_id = None
                if rule.action == "require_approval":
                    # Prefer the rule's own approval_workflow_id; fall back to
                    # the tool config's legacy approval_workflow_id; finally fall
                    # back to the account's default workflow so a bare
                    # ``require_approval`` rule never silently auto-approves.
                    approval_workflow_id = (
                        rule.approval_workflow_id
                        or tool_config.approval_workflow_id
                        or default_workflow_id_for_account
                    )

                rule_desc = (
                    rule.description or f"Rule matched: {rule.condition_expression}"
                )

                # Log the policy decision (fire-and-forget)
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

                return (
                    rule.action,
                    approval_workflow_id,
                    rule_desc,
                )

        except Exception as e:
            # SECURITY: Fail closed on evaluation errors
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
            return (
                "require_approval",
                tool_config.approval_workflow_id or default_workflow_id_for_account,
                error_desc,
            )

    # No rules matched, default allow
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
    return "allow", None, "No rules matched (default allow)"
