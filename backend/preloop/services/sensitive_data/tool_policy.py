"""Sensitive-data rules on the MCP tool path (#1122).

``evaluate_tool_target`` runs the shared detectors over tool arguments
(before the call) or the tool result (after it) for every enabled rule in
scope and returns the first blocking decision: ``deny`` wins over
``require_approval``, ``notify`` never blocks. Audit rows carry the rule id,
the types found and a hash of the scanned text, never the text itself.

The caller decides what a decision means for the call. The model-gateway
targets of the same rules compile into ``ModelIORule`` objects (see
:func:`compile_model_io_rules`) so each path keeps one evaluator.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from preloop.services.policy.schema import (
    ConditionAction,
    DetectorTimeoutFailMode,
    ModelIODetectors,
    ModelIORule,
    PIIDetectorConfig,
    SensitiveDataConfig,
    SensitiveDataRule,
    SensitiveDataTarget,
    ToolCondition,
)
from preloop.services.policy_notices import PolicyNotice, schedule_policy_notice
from preloop.services.sensitive_data.detectors import (
    DetectorConfig,
    Match,
    detect,
    types_found,
)

logger = logging.getLogger(__name__)

#: ``rule_context.source`` for approvals raised by a sensitive-data rule.
SOURCE_SENSITIVE_DATA_RULE = "sensitive_data_rule"
#: Prefix of model I/O rule ids compiled from sensitive-data rules.
COMPILED_RULE_PREFIX = "sensitive-data:"

NOTIFY = ConditionAction.NOTIFY.value
DENY = ConditionAction.DENY.value
REQUIRE_APPROVAL = ConditionAction.REQUIRE_APPROVAL.value
REDACT = ConditionAction.REDACT.value
ALLOW = ConditionAction.ALLOW.value

MAX_SCANNED_PATHS = 50

# Same shape as the model I/O detector pool: hung detectors are abandoned,
# never joined, so a timeout cannot stall the tool call.
_DETECTOR_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="sensitive-data-detector"
)


def _sha256_hex(text: str) -> str:
    return hashlib.file_digest(
        io.BytesIO(text.encode("utf-8", errors="replace")), "sha256"
    ).hexdigest()


@dataclass
class ScanSummary:
    """Detector attributes for one payload (never the matched text)."""

    found: bool = False
    types_found: List[str] = field(default_factory=list)
    count: int = 0
    paths: List[str] = field(default_factory=list)
    text_sha256: str = ""
    timed_out: bool = False

    def bindings(self) -> Dict[str, Any]:
        """``pii.*`` attributes for rule conditions."""
        return {
            "found": bool(self.found),
            "types_found": list(self.types_found),
            "count": int(self.count),
            "paths": list(self.paths),
        }

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe summary for audit rows and approval tickets."""
        payload: Dict[str, Any] = {
            "pii.found": bool(self.found),
            "pii.types_found": list(self.types_found),
            "pii.count": int(self.count),
            "pii.paths": list(self.paths),
        }
        if self.timed_out:
            payload["detector_timeout"] = True
        return payload

    def restricted_to(self, types: Sequence[str]) -> "ScanSummary":
        """Same scan seen through one rule's type list."""
        selected = set(types)
        names = [item for item in self.types_found if item in selected]
        view = ScanSummary(
            found=bool(names),
            types_found=names,
            count=sum(self._counts.get(name, 0) for name in names),
            paths=[
                path
                for path in self.paths
                if any(name in self._path_types.get(path, ()) for name in names)
            ],
            text_sha256=self.text_sha256,
            timed_out=self.timed_out,
        )
        view._counts = {name: self._counts.get(name, 0) for name in names}
        view._path_types = {
            path: {name for name in kinds if name in selected}
            for path, kinds in self._path_types.items()
        }
        return view

    _counts: Dict[str, int] = field(default_factory=dict, repr=False)
    _path_types: Dict[str, set] = field(default_factory=dict, repr=False)


@dataclass
class ToolPolicyOutcome:
    """Decision for one target of one call."""

    action: str = ALLOW
    rule: Optional[SensitiveDataRule] = None
    summary: Optional[ScanSummary] = None
    scan: Optional[ScanSummary] = None
    reason: Optional[str] = None
    notices: List[tuple] = field(default_factory=list)
    #: Redact rules that matched: ``(rule, view)``. Never blocking.
    redactions: List[tuple] = field(default_factory=list)

    def upstream_redaction_types(self, config: SensitiveDataConfig) -> List[str]:
        """Types to strip from the upstream payload (rules with redact_upstream)."""
        types: List[str] = []
        for rule, _view in self.redactions:
            if not getattr(rule, "redact_upstream", False):
                continue
            for item in config.types_for_rule(rule):
                if item not in types:
                    types.append(item)
        return types

    @property
    def rule_id(self) -> Optional[str]:
        """Id of the deciding rule, when one decided."""
        return self.rule.id if self.rule is not None else None

    @property
    def approval_workflow(self) -> Optional[str]:
        """Workflow name on the deciding rule, when any."""
        return self.rule.approval_workflow if self.rule is not None else None

    def bindings(self) -> Dict[str, Any]:
        """``{"pii": {...}}`` for tool access-rule conditions."""
        scan = self.scan or ScanSummary()
        return {"pii": scan.bindings()}

    def detector_summary(self) -> Dict[str, Any]:
        """Summary of the deciding rule's view (or the full scan)."""
        summary = self.summary or self.scan
        return summary.as_dict() if summary is not None else {}

    def rule_context(self) -> Optional[Dict[str, Any]]:
        """Approval ``rule_context`` for a require_approval decision."""
        if self.rule is None:
            return None
        from preloop.services.approval_rule_context import build_rule_context

        summary = self.detector_summary()
        types = ", ".join(summary.get("pii.types_found") or []) or "none"
        return build_rule_context(
            source=SOURCE_SENSITIVE_DATA_RULE,
            decision=self.action,
            rule_id=self.rule.id,
            rule_name=self.rule.description or self.rule.id,
            explanation=(
                f"Sensitive data rule {self.rule.id} matched on "
                f"{self.reason or 'the payload'} (types: {types})."
            ),
            detector_summary=summary,
        )


# ---------------------------------------------------------------------------
# Payload flattening
# ---------------------------------------------------------------------------


def flatten_string_leaves(value: Any, prefix: str = "") -> List[tuple[str, str]]:
    """``(path, text)`` for every scalar leaf; keys are kept as the path.

    Numbers are scanned as their decimal text so a card or national id
    supplied as a JSON number (``{"card": 4111111111111111}``) is not
    missed. Booleans and ``None`` carry nothing.
    """
    leaves: List[tuple[str, str]] = []
    _collect_leaves(value, prefix, leaves)
    return leaves


def _collect_leaves(value: Any, path: str, out: List[tuple[str, str]]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        if value:
            out.append((path or "$", value))
    elif isinstance(value, (int, float)):
        out.append((path or "$", str(value)))
    elif isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if isinstance(key_text, str) and key_text.startswith("_preloop_"):
                continue
            _collect_leaves(item, f"{path}.{key_text}" if path else key_text, out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _collect_leaves(item, f"{path}[{index}]", out)


def result_text(result: Any) -> str:
    """Text an agent would read from a tool result.

    Covers the plain string a proxied wrapper returns, ``ToolResult`` and
    raw MCP content lists (text blocks joined, structured content as JSON).
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        return "\n".join(_block_text(item) for item in result if _block_text(item))
    parts: List[str] = []
    content = getattr(result, "content", None)
    if isinstance(content, (list, tuple)):
        parts.extend(_block_text(item) for item in content if _block_text(item))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, (dict, list)):
        try:
            parts.append(json.dumps(structured, default=str, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(structured))
    if parts:
        return "\n".join(parts)
    if isinstance(result, dict):
        try:
            return json.dumps(result, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(block, dict):
        value = block.get("text")
        return value if isinstance(value, str) else ""
    return ""


# ---------------------------------------------------------------------------
# Rule selection and scanning
# ---------------------------------------------------------------------------


def applicable_rules(
    config: Optional[SensitiveDataConfig],
    target: str,
    *,
    tool_name: Optional[str],
    server_name: Optional[str],
    managed_agent_id: Optional[str],
) -> List[SensitiveDataRule]:
    """Enabled rules that watch ``target`` and whose scope covers this call."""
    if config is None:
        return []
    return [
        rule
        for rule in config.enabled_rules()
        if target in rule.target_values()
        and rule.scope.matches(
            tool_name=tool_name,
            server_name=server_name,
            managed_agent_id=managed_agent_id,
        )
    ]


def has_tool_rules(
    config: Optional[SensitiveDataConfig],
    *,
    tool_name: Optional[str],
    server_name: Optional[str],
    managed_agent_id: Optional[str],
) -> bool:
    """True when any tool target has a rule in scope (cheap pre-check)."""
    return bool(
        applicable_rules(
            config,
            SensitiveDataTarget.TOOL_ARGS.value,
            tool_name=tool_name,
            server_name=server_name,
            managed_agent_id=managed_agent_id,
        )
        or applicable_rules(
            config,
            SensitiveDataTarget.TOOL_RESULT.value,
            tool_name=tool_name,
            server_name=server_name,
            managed_agent_id=managed_agent_id,
        )
    )


def _scan_leaves(
    leaves: Sequence[tuple[str, str]],
    detector_config: DetectorConfig,
) -> ScanSummary:
    """Run the detectors over every leaf and fold the matches into a summary."""
    all_matches: List[Match] = []
    counts: Dict[str, int] = {}
    path_types: Dict[str, set] = {}
    paths: List[str] = []
    joined: List[str] = []
    for path, text in leaves:
        joined.append(text)
        matches = detect(text, detector_config)
        if not matches:
            continue
        all_matches.extend(matches)
        if path not in path_types:
            path_types[path] = set()
            paths.append(path)
        for match in matches:
            counts[match.type] = counts.get(match.type, 0) + 1
            path_types[path].add(match.type)
    names = types_found(all_matches)
    summary = ScanSummary(
        found=bool(names),
        types_found=names,
        count=len(all_matches),
        paths=paths[:MAX_SCANNED_PATHS],
        text_sha256=_sha256_hex("\n".join(joined)),
    )
    summary._counts = counts
    summary._path_types = path_types
    return summary


def scan_with_timeout(
    leaves: Sequence[tuple[str, str]],
    detector_config: DetectorConfig,
    timeout_ms: int,
) -> ScanSummary:
    """Scan under a hard timeout; the worker is abandoned, never joined."""
    future = _DETECTOR_POOL.submit(_scan_leaves, leaves, detector_config)
    try:
        return future.result(timeout=max(timeout_ms, 1) / 1000.0)
    except concurrent.futures.TimeoutError:
        logger.warning("Sensitive data detector timeout timeout_ms=%s", timeout_ms)
        return ScanSummary(
            timed_out=True, text_sha256=_sha256_hex("\n".join(t for _, t in leaves))
        )


def _timeout_mode(rule: SensitiveDataRule) -> str:
    return str(getattr(rule.on_detector_timeout, "value", rule.on_detector_timeout))


def evaluate_tool_target(
    *,
    config: Optional[SensitiveDataConfig],
    detector_config: Optional[DetectorConfig],
    target: str,
    payload: Any,
    tool_name: str,
    server_name: Optional[str],
    managed_agent_id: Optional[str],
    account_id: Any = None,
    user_id: Any = None,
    correlation_id: Optional[str] = None,
    rules: Optional[Sequence[SensitiveDataRule]] = None,
    record: bool = True,
) -> ToolPolicyOutcome:
    """Evaluate every in-scope rule for ``target`` against ``payload``.

    Args:
        config: The account's ``sensitive_data`` block.
        detector_config: Account detector configuration (custom entries).
        target: ``tool.args`` or ``tool.result``.
        payload: Tool arguments (dict) or the tool result (any shape).
        tool_name: Client-visible tool name.
        server_name: MCP server name, ``preloop-mcp`` for builtins.
        managed_agent_id: Calling agent, when known.
        account_id, user_id, correlation_id: Audit attribution.
        rules: Pre-selected rules (skips scope resolution when given).

    Returns:
        :class:`ToolPolicyOutcome`. ``allow`` with ``scan=None`` means no
        rule was in scope and no detector ran.
    """
    selected = (
        list(rules)
        if rules is not None
        else applicable_rules(
            config,
            target,
            tool_name=tool_name,
            server_name=server_name,
            managed_agent_id=managed_agent_id,
        )
    )
    if not selected or config is None:
        return ToolPolicyOutcome()

    leaves = (
        flatten_string_leaves(payload)
        if target == SensitiveDataTarget.TOOL_ARGS.value
        else [("result", result_text(payload))]
    )
    leaves = [(path, text) for path, text in leaves if text]
    base_config = detector_config or DetectorConfig()
    union_types: List[str] = []
    for rule in selected:
        for item in config.types_for_rule(rule):
            if item not in union_types:
                union_types.append(item)
    timeout_ms = max(rule.detector_timeout_ms for rule in selected)
    scan = (
        scan_with_timeout(leaves, base_config.with_types(union_types), timeout_ms)
        if leaves
        else ScanSummary(text_sha256=_sha256_hex(""))
    )

    outcome = ToolPolicyOutcome(scan=scan)
    label = (
        "tool arguments"
        if target == SensitiveDataTarget.TOOL_ARGS.value
        else ("the tool result")
    )
    for rule in selected:
        action = rule.action_value()
        if scan.timed_out:
            if action == NOTIFY:
                continue
            if _timeout_mode(rule) == DetectorTimeoutFailMode.DENY.value:
                outcome.action = DENY
                outcome.rule = rule
                outcome.summary = scan
                outcome.reason = f"detector timeout on rule {rule.id}"
                _audit(
                    outcome, target, tool_name, account_id, user_id, correlation_id
                ) if record else None
                return _finish(
                    outcome,
                    record=record,
                    target=target,
                    tool_name=tool_name,
                    account_id=account_id,
                    user_id=user_id,
                    correlation_id=correlation_id,
                )
            continue
        view = scan.restricted_to(config.types_for_rule(rule))
        if not view.found:
            continue
        if action == NOTIFY:
            outcome.notices.append((rule, view))
            continue
        if action == REDACT:
            outcome.redactions.append((rule, view))
            continue
        outcome.action = action
        outcome.rule = rule
        outcome.summary = view
        outcome.reason = label
        _audit(
            outcome, target, tool_name, account_id, user_id, correlation_id
        ) if record else None
        # Notices gathered before this blocking rule are still emitted.
        return _finish(
            outcome,
            record=record,
            target=target,
            tool_name=tool_name,
            account_id=account_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
    if outcome.notices:
        outcome.action = NOTIFY
        outcome.rule, outcome.summary = outcome.notices[0]
        outcome.reason = label
    elif outcome.redactions:
        outcome.action = REDACT
        outcome.rule, outcome.summary = outcome.redactions[0]
        outcome.reason = label
    return _finish(
        outcome,
        record=record,
        target=target,
        tool_name=tool_name,
        account_id=account_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )


def _finish(
    outcome: ToolPolicyOutcome,
    *,
    target: Optional[str] = None,
    tool_name: Optional[str] = None,
    account_id: Any = None,
    user_id: Any = None,
    correlation_id: Optional[str] = None,
    record: bool = True,
) -> ToolPolicyOutcome:
    """Emit notices and redaction rows, then return the outcome."""
    if not record:
        return outcome
    for rule, view in outcome.redactions:
        try:
            _audit_rule(
                rule,
                view,
                action=REDACT,
                target=target or "",
                tool_name=tool_name or "",
                account_id=account_id,
                user_id=user_id,
                correlation_id=correlation_id,
            )
        except Exception:  # noqa: BLE001 - redaction audit must not fail the call
            logger.warning("Sensitive data redaction audit failed", exc_info=True)
    for rule, view in outcome.notices:
        try:
            _audit_rule(
                rule,
                view,
                action=NOTIFY,
                target=target or "",
                tool_name=tool_name or "",
                account_id=account_id,
                user_id=user_id,
                correlation_id=correlation_id,
            )
            notice = _build_notice(
                rule,
                view,
                target=target or "",
                tool_name=tool_name or "",
                account_id=account_id,
                user_id=user_id,
            )
            if notice is not None:
                schedule_policy_notice(notice)
        except Exception:  # noqa: BLE001 - notify must never fail the call
            logger.warning("Sensitive data notice failed", exc_info=True)
    return outcome


def _uuid_or_none(value: Any) -> Optional[UUID]:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _build_notice(
    rule: SensitiveDataRule,
    view: ScanSummary,
    *,
    target: str,
    tool_name: str,
    account_id: Any,
    user_id: Any,
) -> Optional[PolicyNotice]:
    account_uuid = _uuid_or_none(account_id)
    if account_uuid is None:
        return None
    types = ", ".join(view.types_found) or "none"
    where = f" at {', '.join(view.paths)}" if view.paths else ""
    return PolicyNotice(
        account_id=account_uuid,
        user_id=_uuid_or_none(user_id),
        target=target,
        rule_id=rule.id,
        rule_description=rule.description,
        text_sha256=view.text_sha256,
        # Types and paths only. The excerpt is persisted, so it never holds
        # the matched value.
        excerpt=f"{tool_name}: {types} ({view.count} match(es)){where}",
        approval_workflow=rule.approval_workflow,
    )


def _audit(
    outcome: ToolPolicyOutcome,
    target: str,
    tool_name: str,
    account_id: Any,
    user_id: Any,
    correlation_id: Optional[str],
) -> None:
    if outcome.rule is None or outcome.summary is None:
        return
    try:
        _audit_rule(
            outcome.rule,
            outcome.summary,
            action=outcome.action,
            target=target,
            tool_name=tool_name,
            account_id=account_id,
            user_id=user_id,
            correlation_id=correlation_id,
            reason=outcome.reason,
        )
    except Exception:  # noqa: BLE001 - audit must not change the decision
        logger.warning("Sensitive data audit failed", exc_info=True)


def _audit_rule(
    rule: SensitiveDataRule,
    view: ScanSummary,
    *,
    action: str,
    target: str,
    tool_name: str,
    account_id: Any,
    user_id: Any,
    correlation_id: Optional[str],
    reason: Optional[str] = None,
) -> None:
    """One decision row: rule id, types found, hash. Never the text."""
    account_uuid = _uuid_or_none(account_id)
    if account_uuid is None:
        return
    from preloop.services.policy_evaluator import _log_policy_decision_async

    summary = view.as_dict()
    extra: Dict[str, Any] = {
        "source": SOURCE_SENSITIVE_DATA_RULE,
        "rule_id": rule.id,
        "target": target,
        "types_found": list(view.types_found),
        "detector_summary": summary,
        "text_sha256": view.text_sha256,
    }
    if reason:
        extra["reason"] = reason
    if action == REDACT:
        extra["redaction_counts"] = {
            name: view._counts.get(name, 0) for name in view.types_found
        }
        extra["redact_upstream"] = bool(getattr(rule, "redact_upstream", False))
    _log_policy_decision_async(
        account_id=account_uuid,
        tool_name=tool_name,
        action=action,
        rule_description=rule.description
        or f"Sensitive data rule {rule.id} matched on {target}",
        condition_matched=rule.id,
        tool_args={"text_sha256": view.text_sha256},
        user_id=_uuid_or_none(user_id),
        correlation_id=correlation_id,
        extra_details=extra,
    )


# ---------------------------------------------------------------------------
# Model targets compile to model I/O rules
# ---------------------------------------------------------------------------


def compile_model_io_rules(
    config: Optional[SensitiveDataConfig],
    *,
    managed_agent_id: Optional[str] = None,
) -> List[ModelIORule]:
    """Model targets of enabled rules as ``ModelIORule`` objects.

    One compiled rule per (rule, model target). Ids are prefixed so they
    cannot collide with hand-written rules and are recognisable in audit
    rows. ``scope.agents`` is honoured; tool and server scopes do not apply
    to the gateway.
    """
    if config is None:
        return []
    compiled: List[ModelIORule] = []
    for rule in config.enabled_rules():
        if not rule.has_model_target():
            continue
        # Only the agent list applies on the gateway; tool and server lists
        # describe MCP calls and are ignored here.
        if rule.scope.agents and (
            managed_agent_id is None or str(managed_agent_id) not in rule.scope.agents
        ):
            continue
        types = config.types_for_rule(rule)
        for target in rule.target_values():
            if target not in (
                SensitiveDataTarget.MODEL_REQUEST.value,
                SensitiveDataTarget.MODEL_RESPONSE.value,
            ):
                continue
            compiled.append(
                ModelIORule(
                    id=f"{COMPILED_RULE_PREFIX}{rule.id}:{target}",
                    target=target,
                    enabled=True,
                    description=rule.description or f"Sensitive data rule {rule.id}",
                    approval_workflow=rule.approval_workflow,
                    detectors=ModelIODetectors(pii=PIIDetectorConfig(types=types)),
                    detector_timeout_ms=rule.detector_timeout_ms,
                    on_detector_timeout=rule.on_detector_timeout,
                    # Only requests are rewritten upstream; a compiled
                    # response rule redacts stored copies only.
                    redact_upstream=bool(getattr(rule, "redact_upstream", False))
                    and target == SensitiveDataTarget.MODEL_REQUEST.value,
                    conditions=[
                        ToolCondition(
                            expression="pii.found == true",
                            action=rule.action,
                            description=rule.description,
                        )
                    ],
                )
            )
    return compiled


def merge_model_io_rules(
    stored: Iterable[ModelIORule], compiled: Iterable[ModelIORule]
) -> List[ModelIORule]:
    """Hand-written rules first, then compiled ones (first match wins)."""
    return list(stored) + list(compiled)


def redact_tool_result(result: Any, detector_config: DetectorConfig) -> Any:
    """Return the result the agent receives with matches replaced.

    Strings are redacted in place; a ``ToolResult`` keeps its shape with
    text blocks and structured content rewritten; raw content lists are
    rebuilt as text blocks.
    """
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    from preloop.services.sensitive_data.redact import redact_structure, redact_text

    if result is None:
        return result
    if isinstance(result, str):
        return redact_text(result, detector_config)[0]
    content = getattr(result, "content", None)
    if isinstance(content, (list, tuple)):
        blocks = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                blocks.append(
                    TextContent(type="text", text=redact_text(text, detector_config)[0])
                )
            else:
                blocks.append(block)
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, (dict, list)):
            structured = redact_structure(structured, detector_config)[0]
        return ToolResult(
            content=blocks,
            structured_content=structured if isinstance(structured, dict) else None,
            is_error=bool(getattr(result, "is_error", False)),
        )
    if isinstance(result, (list, tuple)):
        rebuilt = []
        for item in result:
            text = _block_text(item)
            if text:
                rebuilt.append(
                    TextContent(type="text", text=redact_text(text, detector_config)[0])
                )
            else:
                rebuilt.append(item)  # images, resources: kept unchanged
        return rebuilt
    return redact_text(str(result), detector_config)[0]
