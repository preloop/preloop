"""Model I/O content policy hook.

Gateway callers invoke two functions:

- ``enforce_request_policy`` before the provider is called
- ``enforce_response_policy`` after the provider returns, before bytes
  reach the client

Streaming uses ``wrap_stream_for_response_policy``, which buffers SSE
events until the assembled ``response.text`` can be evaluated. Denied
payloads are never replayed to the client. Buffer-until-assembled is
intentional: a ``model.response`` deny cannot retract tokens already
sent, so a rolling window is unsafe for deny/require_approval. The
cost is that time-to-first-token becomes time-to-last-token when
response rules exist.

Rules live on ``account.meta_data['model_io_rules']`` so the existing
Policies YAML editor and the console form share one store. When no
model I/O rules exist, evaluation returns allow, matching
``policy_evaluator._evaluate_loaded_access_rules`` ("No access rules
defined" / "No rules matched (default allow)").
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import crud_account, crud_approval_workflow
from preloop.services.approval_rule_context import (
    SOURCE_MODEL_IO_RULE,
    build_rule_context,
)
from preloop.services.model_content_detectors import (
    detect_injection,
    detect_moderation,
    detect_pii,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.policy.schema import ModelIORule
from preloop.services.policy_evaluator import (
    PolicyDecision,
    _log_policy_decision_async,
    evaluate_condition_against_bindings,
)

logger = logging.getLogger(__name__)

MODEL_IO_META_KEY = "model_io_rules"
CONTENT_POLICY_ERROR_CODE = "content_policy_denied"
CONTENT_POLICY_MESSAGE = "Blocked by content policy"

# Hung detectors are abandoned on timeout rather than joined. A small
# dedicated pool keeps ``ThreadPoolExecutor.__exit__`` from blocking the
# request on ``shutdown(wait=True)``.
_DETECTOR_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="model-io-detector"
)

_DETECTOR_PREFIXES = {
    "pii": ("pii.", "pii["),
    "injection": ("injection.", "injection["),
    "moderation": ("moderation.", "moderation["),
}


@dataclass
class DetectorSummary:
    """Detector attributes attached to a decision (never full prompts)."""

    pii_found: Optional[bool] = None
    pii_types_found: Optional[List[str]] = None
    injection_score: Optional[float] = None
    injection_matched_patterns: Optional[List[str]] = None
    moderation_flagged: Optional[bool] = None
    moderation_categories: Optional[List[str]] = None
    timed_out: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe subset for audit and approval tickets."""
        payload: Dict[str, Any] = {}
        if self.pii_found is not None:
            payload["pii.found"] = self.pii_found
            payload["pii.types_found"] = list(self.pii_types_found or [])
        if self.injection_score is not None:
            payload["injection.score"] = self.injection_score
            payload["injection.matched_patterns"] = list(
                self.injection_matched_patterns or []
            )
        if self.moderation_flagged is not None:
            payload["moderation.flagged"] = self.moderation_flagged
            payload["moderation.categories"] = list(self.moderation_categories or [])
        if self.timed_out:
            payload["detector_timeout"] = True
        return payload


@dataclass
class ModelIODecision:
    """Outcome of evaluating model.request or model.response rules."""

    action: str
    rule_id: Optional[str] = None
    rule_description: Optional[str] = None
    approval_workflow: Optional[str] = None
    detector_summary: Dict[str, Any] = field(default_factory=dict)
    text_sha256: Optional[str] = None
    expression: Optional[str] = None

    def to_policy_decision(self) -> PolicyDecision:
        """Adapt to the historical PolicyDecision 3-tuple."""
        return PolicyDecision(
            self.action, None, self.rule_description or "No model I/O rules defined"
        )


def load_model_io_rules(db: Session, account_id: Any) -> List[ModelIORule]:
    """Load model I/O rules from account metadata."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        return []
    return parse_model_io_rules((account.meta_data or {}).get(MODEL_IO_META_KEY))


def parse_model_io_rules(raw: Any) -> List[ModelIORule]:
    """Parse stored or YAML rule dicts into ``ModelIORule`` models."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    rules: List[ModelIORule] = []
    for item in raw:
        try:
            rules.append(ModelIORule.model_validate(item))
        except Exception as exc:
            logger.warning("Skipping invalid model I/O rule: %s", exc)
    return rules


def serialize_model_io_rules(rules: Sequence[ModelIORule]) -> List[Dict[str, Any]]:
    """Serialize rules for account.meta_data and YAML export."""
    return [rule.model_dump(exclude_none=True, mode="json") for rule in rules]


def replace_model_io_rules(
    db: Session, account_id: Any, rules: Sequence[ModelIORule]
) -> List[ModelIORule]:
    """Replace the account's model I/O rules and persist."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    meta = dict(account.meta_data or {})
    serialized = serialize_model_io_rules(rules)
    if serialized:
        meta[MODEL_IO_META_KEY] = serialized
    else:
        meta.pop(MODEL_IO_META_KEY, None)
    account.meta_data = meta
    flag_modified(account, "meta_data")
    db.add(account)
    db.flush()
    return list(rules)


def upsert_model_io_rule(
    db: Session, account_id: Any, rule: ModelIORule
) -> ModelIORule:
    """Create or replace one rule by id."""
    existing = load_model_io_rules(db, account_id)
    updated = [item for item in existing if item.id != rule.id]
    updated.append(rule)
    replace_model_io_rules(db, account_id, updated)
    return rule


def delete_model_io_rule(db: Session, account_id: Any, rule_id: str) -> bool:
    """Delete one rule by id. Returns True when a rule was removed."""
    existing = load_model_io_rules(db, account_id)
    remaining = [item for item in existing if item.id != rule_id]
    if len(remaining) == len(existing):
        return False
    replace_model_io_rules(db, account_id, remaining)
    return True


def canonical_request_text(
    messages: Optional[Sequence[Any]], payload: Optional[Dict[str, Any]] = None
) -> str:
    """Concatenate user-visible request text into the canonical field.

    ``request.text`` is the documented matching field. Message contents
    are joined with newlines. Responses-API ``input`` strings are
    appended when present.
    """
    parts: List[str] = []
    for message in messages or []:
        if isinstance(message, dict):
            parts.append(_content_to_text(message.get("content")))
        elif isinstance(message, str):
            parts.append(message)
    payload = payload or {}
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        parts.append(raw_input)
    return "\n".join(part for part in parts if part)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text_value = item.get("text") or item.get("content")
                if isinstance(text_value, str):
                    texts.append(text_value)
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts)
    return str(content)


def _text_privacy(text: str) -> str:
    """SHA-256 of scanned text. Never persist a raw preview (it may be PII)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _rule_enables_detector(rule: ModelIORule, name: str) -> bool:
    detectors = rule.detectors
    if detectors is not None:
        value = getattr(detectors, name, None)
        if value is True or (value is not None and value is not False):
            return True
    expressions = [condition.expression for condition in (rule.conditions or [])]
    prefixes = _DETECTOR_PREFIXES[name]
    return any(
        any(prefix in (expression or "") for prefix in prefixes)
        for expression in expressions
    )


def _pii_types_for_rule(rule: ModelIORule) -> List[str]:
    detectors = rule.detectors
    if detectors is None or detectors.pii in (None, False, True):
        return ["email", "phone", "credit_card"]
    return list(detectors.pii.types)


def _moderation_backend_for_rule(rule: ModelIORule) -> str:
    detectors = rule.detectors
    if detectors is None or detectors.moderation in (None, False, True):
        return "local"
    return detectors.moderation.backend


def _run_detectors(rule: ModelIORule, text: str) -> DetectorSummary:
    """Run only the detectors this rule enables."""
    summary = DetectorSummary()
    if _rule_enables_detector(rule, "pii"):
        result = detect_pii(text, _pii_types_for_rule(rule))
        summary.pii_found = result.found
        summary.pii_types_found = result.types_found
    if _rule_enables_detector(rule, "injection"):
        result = detect_injection(text)
        summary.injection_score = result.score
        summary.injection_matched_patterns = result.matched_patterns
    if _rule_enables_detector(rule, "moderation"):
        result = detect_moderation(text, _moderation_backend_for_rule(rule))
        summary.moderation_flagged = result.flagged
        summary.moderation_categories = result.categories
    return summary


def _run_detectors_with_timeout(rule: ModelIORule, text: str) -> DetectorSummary:
    """Run detectors with the rule's hard timeout.

    The future is submitted on a process-level pool so this function can
    return on timeout without ``shutdown(wait=True)`` joining a hung
    detector. The abandoned worker is left to finish or be collected at
    process exit.
    """
    timeout_s = max(rule.detector_timeout_ms, 1) / 1000.0
    future = _DETECTOR_POOL.submit(_run_detectors, rule, text)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Model I/O detector timeout rule_id=%s timeout_ms=%s",
            rule.id,
            rule.detector_timeout_ms,
        )
        return DetectorSummary(timed_out=True)


def _timeout_fail_mode(rule: ModelIORule) -> str:
    value = rule.on_detector_timeout
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _build_bindings(
    *,
    target: str,
    text: str,
    ai_model: Any,
    session_id: Optional[str],
    summary: DetectorSummary,
) -> Dict[str, Any]:
    model_id = ""
    provider = ""
    name = ""
    if ai_model is not None:
        model_id = str(getattr(ai_model, "id", "") or "")
        provider = str(getattr(ai_model, "provider_name", "") or "")
        name = str(
            getattr(ai_model, "model_identifier", None)
            or getattr(ai_model, "name", "")
            or ""
        )
    bindings: Dict[str, Any] = {
        "model": {"id": model_id, "provider": provider, "name": name},
        "session": {"id": session_id or ""},
        "request": {"text": text if target == "model.request" else ""},
        "response": {"text": text if target == "model.response" else ""},
        "pii": {
            "found": bool(summary.pii_found),
            "types_found": list(summary.pii_types_found or []),
        },
        "injection": {
            "score": float(summary.injection_score or 0.0),
            "matched_patterns": list(summary.injection_matched_patterns or []),
        },
        "moderation": {
            "flagged": bool(summary.moderation_flagged),
            "categories": list(summary.moderation_categories or []),
        },
    }
    return bindings


def evaluate_model_io(
    *,
    rules: Sequence[ModelIORule],
    target: str,
    text: str,
    ai_model: Any = None,
    session_id: Optional[str] = None,
    account_id: Optional[Any] = None,
    user_id: Optional[Any] = None,
) -> ModelIODecision:
    """Evaluate model I/O rules for one target.

    First matching enabled rule condition wins. No matching rule: allow.
    Detector timeout follows ``on_detector_timeout`` (default deny).
    """
    digest = _text_privacy(text)
    if not rules:
        return ModelIODecision(
            action="allow",
            rule_description="No model I/O rules defined",
            text_sha256=digest,
        )
    matching = [rule for rule in rules if rule.enabled and str(rule.target) == target]
    if not matching:
        return ModelIODecision(
            action="allow",
            rule_description="No rules matched (default allow)",
            text_sha256=digest,
        )

    for rule in matching:
        summary = _run_detectors_with_timeout(rule, text)
        if summary.timed_out:
            fail_mode = _timeout_fail_mode(rule)
            if fail_mode == "deny":
                decision = ModelIODecision(
                    action="deny",
                    rule_id=rule.id,
                    rule_description=f"Detector timeout on rule {rule.id}",
                    detector_summary=summary.as_dict(),
                    text_sha256=digest,
                )
                _audit_decision(account_id, user_id, target, decision, rule)
                return decision
            continue

        bindings = _build_bindings(
            target=target,
            text=text,
            ai_model=ai_model,
            session_id=session_id,
            summary=summary,
        )
        for condition in rule.conditions:
            condition_type = condition.condition_type
            if hasattr(condition_type, "value"):
                condition_type = condition_type.value
            try:
                matches = evaluate_condition_against_bindings(
                    condition.expression,
                    str(condition_type or "simple"),
                    bindings,
                )
            except Exception as exc:
                logger.error("Model I/O condition error rule_id=%s: %s", rule.id, exc)
                decision = ModelIODecision(
                    action="deny",
                    rule_id=rule.id,
                    rule_description=f"Rule evaluation error: {exc}",
                    detector_summary=summary.as_dict(),
                    text_sha256=digest,
                )
                _audit_decision(account_id, user_id, target, decision, rule)
                return decision
            if not matches:
                continue
            action = condition.action
            if hasattr(action, "value"):
                action = action.value
            decision = ModelIODecision(
                action=str(action),
                rule_id=rule.id,
                rule_description=condition.description
                or rule.description
                or f"Rule matched: {condition.expression}",
                approval_workflow=rule.approval_workflow,
                detector_summary=summary.as_dict(),
                text_sha256=digest,
                expression=condition.expression,
            )
            _audit_decision(account_id, user_id, target, decision, rule)
            return decision

    return ModelIODecision(
        action="allow",
        rule_description="No rules matched (default allow)",
        text_sha256=digest,
    )


def _audit_decision(
    account_id: Optional[Any],
    user_id: Optional[Any],
    target: str,
    decision: ModelIODecision,
    rule: ModelIORule,
) -> None:
    if account_id is None:
        return
    if decision.action == "allow":
        return
    try:
        account_uuid = (
            account_id if isinstance(account_id, UUID) else UUID(str(account_id))
        )
        user_uuid = None
        if user_id is not None:
            user_uuid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    except (TypeError, ValueError):
        return
    extra = {
        "rule_id": decision.rule_id,
        "detector_summary": decision.detector_summary,
        "text_sha256": decision.text_sha256,
    }
    _log_policy_decision_async(
        account_id=account_uuid,
        tool_name=target,
        action=decision.action,
        rule_description=decision.rule_description,
        condition_matched=rule.id,
        tool_args={
            "text_sha256": decision.text_sha256,
        },
        user_id=user_uuid,
        extra_details=extra,
    )


def _gateway_error(provider: str, decision: ModelIODecision) -> ModelGatewayAPIError:
    suffix = f" (rule {decision.rule_id})" if decision.rule_id else ""
    return ModelGatewayAPIError(
        provider=provider,  # type: ignore[arg-type]
        status_code=403,
        message=f"{CONTENT_POLICY_MESSAGE}{suffix}",
        code=CONTENT_POLICY_ERROR_CODE,
        error_type="permission_error",
    )


def _approval_arguments(decision: ModelIODecision, target: str) -> Dict[str, Any]:
    """Approval ticket payload. Hash and detector summary, never raw text."""
    return {
        "target": target,
        "rule_id": decision.rule_id,
        "detector_summary": decision.detector_summary,
        "text_sha256": decision.text_sha256,
    }


def _resolve_workflow_id(
    db: Session, account_id: Any, workflow_name: Optional[str]
) -> Optional[str]:
    if not workflow_name:
        workflow = crud_approval_workflow.get_default(db, account_id=account_id)
        return str(workflow.id) if workflow else None
    workflow = crud_approval_workflow.get_by_name(
        db, account_id=account_id, name=workflow_name
    )
    if workflow:
        return str(workflow.id)
    return None


async def hold_for_model_io_approval(
    *,
    db: Session,
    account_id: Any,
    target: str,
    decision: ModelIODecision,
) -> bool:
    """Hold on the existing tool-approval workflow.

    Awaits ``require_approval`` on the current event loop, the same way
    tool gates wait. Sync gateway callers drive this via
    ``_await_model_io_hold`` so a running loop is never blocked with
    ``Future.result()``.

    Returns True when approved. False when declined, expired, or the
    workflow is missing (fail closed).
    """
    workflow_id = _resolve_workflow_id(db, account_id, decision.approval_workflow)
    if not workflow_id:
        logger.error(
            "model I/O require_approval has no workflow rule_id=%s",
            decision.rule_id,
        )
        return False

    rule_context = build_rule_context(
        source=SOURCE_MODEL_IO_RULE,
        decision="require_approval",
        rule_id=decision.rule_id,
        rule_name=decision.rule_description or decision.rule_id,
        expression=decision.expression,
        explanation=(
            f"Model I/O rule {decision.rule_id} required approval. "
            f"Detectors: {decision.detector_summary}"
        ),
        detector_summary=decision.detector_summary,
    )

    from preloop.services.approval_helper import require_approval

    approved, _message = await require_approval(
        tool_name=target,
        tool_source="builtin",
        account_id=str(account_id),
        arguments=_approval_arguments(decision, target),
        workflow_id=workflow_id,
        rule_context=rule_context,
    )
    return approved


def _await_model_io_hold(awaitable: Any) -> bool:
    """Drive ``hold_for_model_io_approval`` from sync gateway methods.

    FastAPI ``def`` endpoints and Starlette ``iterate_in_threadpool`` run
    the gateway off the event loop, so ``asyncio.run`` is safe there. If
    a loop is already running, blocking ``Future.result()`` would freeze
    the worker; callers in that context must ``await`` the coroutine.
    Patched sync mocks (tests) are returned as-is.
    """
    if not asyncio.iscoroutine(awaitable):
        return bool(awaitable)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return bool(asyncio.run(awaitable))
    raise RuntimeError(
        "model I/O require_approval cannot block a running event loop; "
        "await hold_for_model_io_approval from async callers"
    )


def _session_id_from_gateway(gateway: Any) -> Optional[str]:
    return getattr(gateway, "_client_session_id", None) or getattr(
        gateway, "_resolved_runtime_session_id", None
    )


def _apply_decision(
    *,
    gateway: Any,
    decision: ModelIODecision,
    target: str,
    provider: str,
    before_provider: bool,  # noqa: ARG001 - reserved for audit context
) -> None:
    if decision.action == "allow":
        return
    if decision.action == "require_approval":
        approved = _await_model_io_hold(
            hold_for_model_io_approval(
                db=gateway.db,
                account_id=gateway.auth_context.user.account_id,
                target=target,
                decision=decision,
            )
        )
        if approved:
            return
        raise _gateway_error(provider, decision)
    if decision.action == "deny":
        raise _gateway_error(provider, decision)
    raise _gateway_error(provider, decision)


def enforce_request_policy(
    gateway: Any,
    *,
    payload: Dict[str, Any],
    ai_model: Any,
    messages: Optional[Sequence[Any]],
    provider: str,
) -> None:
    """Evaluate model.request rules before the provider call."""
    account_id = gateway.auth_context.user.account_id
    rules = load_model_io_rules(gateway.db, account_id)
    if not any(rule.enabled and str(rule.target) == "model.request" for rule in rules):
        return
    text = canonical_request_text(messages, payload)
    decision = evaluate_model_io(
        rules=rules,
        target="model.request",
        text=text,
        ai_model=ai_model,
        session_id=_session_id_from_gateway(gateway),
        account_id=account_id,
        user_id=getattr(gateway.auth_context.user, "id", None),
    )
    _apply_decision(
        gateway=gateway,
        decision=decision,
        target="model.request",
        provider=provider,
        before_provider=True,
    )


def enforce_response_policy(
    gateway: Any,
    *,
    payload: Dict[str, Any],  # noqa: ARG001 - kept for call-site symmetry
    ai_model: Any,
    response_text: str,
    provider: str,
) -> None:
    """Evaluate model.response rules before bytes reach the client."""
    account_id = gateway.auth_context.user.account_id
    rules = load_model_io_rules(gateway.db, account_id)
    if not any(rule.enabled and str(rule.target) == "model.response" for rule in rules):
        return
    decision = evaluate_model_io(
        rules=rules,
        target="model.response",
        text=response_text or "",
        ai_model=ai_model,
        session_id=_session_id_from_gateway(gateway),
        account_id=account_id,
        user_id=getattr(gateway.auth_context.user, "id", None),
    )
    _apply_decision(
        gateway=gateway,
        decision=decision,
        target="model.response",
        provider=provider,
        before_provider=False,
    )


_SSE_DATA_RE = re.compile(r"^data:\s*(.*)$", re.MULTILINE)


def extract_stream_text(event: str) -> str:
    """Pull assistant text from one SSE event.

    Parses chat/completions, OpenAI Responses, and Anthropic message
    shapes explicitly so a fallback cannot double-count the same delta.
    """
    text, _is_snapshot = _extract_stream_fragment(event)
    return text


def _extract_stream_fragment(event: str) -> tuple[str, bool]:
    """Return ``(text, is_full_snapshot)`` for one SSE event.

    Snapshot events (Responses ``response.completed``) carry the full
    assembled ``output_text``. Callers that also collected incremental
    deltas must prefer the snapshot to avoid concatenating the full
    text on top of the deltas.
    """
    parts: List[str] = []
    is_snapshot = False
    for match in _SSE_DATA_RE.finditer(event):
        raw = match.group(1).strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")

        # OpenAI chat/completions (and LiteLLM OpenAI-shape streams).
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    parts.append(content)
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                msg_content = message.get("content")
                if isinstance(msg_content, str):
                    parts.append(msg_content)
            continue

        # OpenAI Responses API: incremental output_text.delta is a string.
        # Completed events nest the full text under response.output_text.
        if event_type == "response.completed":
            resp = payload.get("response")
            if isinstance(resp, dict) and isinstance(resp.get("output_text"), str):
                parts.append(resp["output_text"])
                is_snapshot = True
            continue
        if isinstance(payload.get("delta"), str):
            parts.append(payload["delta"])
            continue

        # Anthropic messages: content_block_delta.delta.text, not the
        # same object via a content_block fallback (that double-counted).
        delta_obj = payload.get("delta")
        if event_type == "content_block_delta" or (
            isinstance(delta_obj, dict) and delta_obj.get("type") == "text_delta"
        ):
            if isinstance(delta_obj, dict) and isinstance(delta_obj.get("text"), str):
                parts.append(delta_obj["text"])
            continue
        content_block = payload.get("content_block")
        if isinstance(content_block, dict) and isinstance(
            content_block.get("text"), str
        ):
            # content_block_start often has empty text; appending "" is fine.
            parts.append(content_block["text"])
    return "".join(parts), is_snapshot


def wrap_stream_for_response_policy(
    events: Iterator[str],
    *,
    gateway: Any,
    payload: Dict[str, Any],
    ai_model: Any,
    provider: str,
) -> Iterator[str]:
    """Buffer-until-assembled streaming enforcement.

    When no model.response rules exist, events pass through unchanged.
    Otherwise the upstream stream is fully buffered, policy runs on the
    assembled text, and only an allowed stream is replayed. A deny yields
    an SSE error event and never the blocked payload.

    Full buffering is required for deny/require_approval: tokens already
    sent cannot be retracted, so a rolling window cannot enforce those
    actions. Clients see time-to-first-token equal time-to-last-token
    when any ``model.response`` rule is enabled.
    """
    account_id = gateway.auth_context.user.account_id
    rules = load_model_io_rules(gateway.db, account_id)
    if not any(rule.enabled and str(rule.target) == "model.response" for rule in rules):
        yield from events
        return

    buffered: List[str] = []
    delta_parts: List[str] = []
    snapshot_text: Optional[str] = None
    try:
        for event in events:
            buffered.append(event)
            fragment, is_snapshot = _extract_stream_fragment(event)
            if is_snapshot:
                snapshot_text = fragment
            elif fragment:
                delta_parts.append(fragment)
    except ModelGatewayAPIError:
        raise

    assembled = snapshot_text if snapshot_text is not None else "".join(delta_parts)
    try:
        enforce_response_policy(
            gateway,
            payload=payload,
            ai_model=ai_model,
            response_text=assembled,
            provider=provider,
        )
    except ModelGatewayAPIError as exc:
        error_event = gateway._openai_stream_error_event(exc, exc)
        yield error_event
        yield gateway._sse_done()
        return
    yield from buffered
