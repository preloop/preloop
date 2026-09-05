"""Matched-rule context carried onto approval requests.

Why this exists: when a human reviews an approval request they see the tool
name and the argument values, but not WHICH rule demanded the approval. That
makes a boundary case (``args.amount == threshold``) indistinguishable from a
mid-band one, and it hides a mis-scoped rule at exactly the moment someone is
in a position to notice it.

This module builds a small, durable, JSON-serialisable record of the winning
rule so it can be persisted on ``ApprovalRequest.rule_context`` and rendered
on every approval surface. It is deliberately a *description of what matched*,
never a risk score or a recommendation: we know which expression evaluated
true, and nothing more.

Absence is meaningful. Approvals raised without any rule evaluation (the
``request_approval`` builtin, historical rows created before this field
existed) carry ``None`` and surfaces must simply omit the block rather than
invent an explanation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

#: A rule row in ``tool_access_rules`` matched the call.
SOURCE_TOOL_ACCESS_RULE = "tool_access_rule"

#: A subject-scoped governance rule (stored on the account's meta_data and
#: scoped to an agent or API key) matched the call.
SOURCE_SUBJECT_SCOPED_RULE = "subject_scoped_rule"

#: No rule matched: the tool configuration itself pins an approval workflow,
#: so every call to the tool is gated regardless of arguments.
SOURCE_TOOL_DEFAULT_WORKFLOW = "tool_default_workflow"

#: A rule could not be evaluated (malformed expression, bad argument types).
#: Policy evaluation fails closed, so the approval exists because of the
#: failure, not because a condition was met.
SOURCE_RULE_EVALUATION_ERROR = "rule_evaluation_error"

#: The approval came from an agent's own permission hook (e.g. Claude Code
#: PreToolUse escalating an "ask" verdict). Preloop's tool access rules are
#: not consulted on that path.
SOURCE_AGENT_PERMISSION_HOOK = "agent_permission_hook"

#: A model.request or model.response content policy required approval.
SOURCE_MODEL_IO_RULE = "model_io_rule"

#: Every ``source`` value a caller may persist.
KNOWN_SOURCES = frozenset(
    {
        SOURCE_TOOL_ACCESS_RULE,
        SOURCE_SUBJECT_SCOPED_RULE,
        SOURCE_TOOL_DEFAULT_WORKFLOW,
        SOURCE_RULE_EVALUATION_ERROR,
        SOURCE_AGENT_PERMISSION_HOOK,
        SOURCE_MODEL_IO_RULE,
    }
)

#: Plain statements for the cases where no named rule fired. These say what
#: actually happened; they do not imply anyone assessed the call.
_DEFAULT_EXPLANATIONS = {
    SOURCE_TOOL_DEFAULT_WORKFLOW: (
        "No access rule matched. This tool is configured to require approval "
        "for every call, whatever the arguments are."
    ),
    SOURCE_RULE_EVALUATION_ERROR: (
        "An access rule could not be evaluated, so Preloop failed closed and "
        "asked for approval instead of deciding on its own."
    ),
    SOURCE_AGENT_PERMISSION_HOOK: (
        "The agent's own permission hook escalated this call for human "
        "approval. No Preloop access rule was evaluated for it."
    ),
    SOURCE_MODEL_IO_RULE: (
        "A model request or response content policy required approval."
    ),
}

#: Label of last resort when a rule has neither description nor expression.
_GENERIC_LABELS = {
    SOURCE_TOOL_ACCESS_RULE: "Access rule",
    SOURCE_SUBJECT_SCOPED_RULE: "Scoped governance rule",
    SOURCE_TOOL_DEFAULT_WORKFLOW: "Tool default policy",
    SOURCE_RULE_EVALUATION_ERROR: "Rule evaluation error",
    SOURCE_AGENT_PERMISSION_HOOK: "Agent permission hook",
    SOURCE_MODEL_IO_RULE: "Model content policy",
}

#: Identifiers referenced through ``args.`` in an expression, e.g. the
#: ``amount`` in ``args.amount > 1000``. Used to point the reviewer at the
#: argument the rule actually looked at.
_ARGS_REFERENCE = re.compile(r"\bargs\.([A-Za-z_][A-Za-z0-9_]*)")

#: Bare leading identifier for the shorthand form users may write in rules
#: ("amount > 300" instead of "args.amount > 300"). Boolean literals are
#: catch-alls (``_evaluate_simple_condition`` accepts bare ``true``/``false``)
#: and are not argument names.
_BARE_REFERENCE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_BOOLEAN_LITERALS = frozenset({"true", "false"})


def referenced_args(expression: Optional[str]) -> List[str]:
    """Return argument names the expression mentions, in first-seen order.

    Best effort and purely presentational: a name here means the expression
    text mentions it, not that it decided the outcome.
    """
    if not expression:
        return []
    names: List[str] = []
    for match in _ARGS_REFERENCE.finditer(expression):
        name = match.group(1)
        if name not in names:
            names.append(name)
    if not names:
        bare = _BARE_REFERENCE.match(expression)
        if bare and bare.group(1).lower() not in _BOOLEAN_LITERALS:
            names.append(bare.group(1))
    return names


def build_rule_context(
    *,
    source: str,
    decision: str,
    rule_id: Optional[Any] = None,
    rule_name: Optional[str] = None,
    expression: Optional[str] = None,
    expression_type: Optional[str] = None,
    priority: Optional[int] = None,
    explanation: Optional[str] = None,
    also_matched_rule_ids: Optional[Iterable[Any]] = None,
    tool_configuration_id: Optional[Any] = None,
    detector_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the JSON payload persisted on ``ApprovalRequest.rule_context``.

    Args:
        source: One of the ``SOURCE_*`` constants above.
        decision: The policy action that produced the approval. In practice
            always ``"require_approval"``; recorded so a future action does
            not silently reuse this field with a different meaning.
        rule_id: Identifier of the winning rule, when it has one. Subject
            scoped rules live in JSON config and have none.
        rule_name: Human label for the rule. Falls back to the expression,
            then to a generic label for the source.
        expression: The condition expression as written by the operator.
        expression_type: ``"cel"`` or ``"simple"``.
        priority: The rule's evaluation priority (lower runs first).
        explanation: Overrides the default plain statement for the source.
        also_matched_rule_ids: Lower-priority rules that would also have
            matched. Informational only: the winning rule is the one that
            decided.
        tool_configuration_id: Tool config the rule belongs to, so surfaces
            can link to where the rule is edited.

    Returns:
        A dict with no ``None`` values, safe to store as JSONB.

    Raises:
        ValueError: If ``source`` is not a known source constant.
    """
    if source not in KNOWN_SOURCES:
        raise ValueError(f"Unknown approval rule context source: {source!r}")

    label = (rule_name or "").strip() or (expression or "").strip()
    if not label:
        label = _GENERIC_LABELS.get(source, "Approval rule")

    context: Dict[str, Any] = {
        "source": source,
        "decision": decision,
        "rule_name": label,
    }

    explanation_text = explanation or _DEFAULT_EXPLANATIONS.get(source)
    if explanation_text:
        context["explanation"] = explanation_text
    if rule_id is not None:
        context["rule_id"] = str(rule_id)
    if expression:
        context["expression"] = expression
        references = referenced_args(expression)
        if references:
            context["referenced_args"] = references
    if expression_type:
        context["expression_type"] = expression_type
    if priority is not None:
        context["priority"] = int(priority)
    if tool_configuration_id is not None:
        context["tool_configuration_id"] = str(tool_configuration_id)
    also = [str(rule) for rule in (also_matched_rule_ids or [])]
    if also:
        context["also_matched_rule_ids"] = also
    if detector_summary:
        context["detector_summary"] = detector_summary

    return context
