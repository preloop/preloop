"""Per-flow ordered model/harness routing from current issue labels.

Policy lives on ``flow.agent_config.model_routing`` (no migration). The
flow's selected ``ai_model_id`` / ``agent_type`` remain the required default.
Assessment predicates are not read in this slice: matching uses only
controller-extracted label names, never webhook-supplied model ids.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from uuid import UUID
from typing import Any, Dict, Iterable, List, Optional, Sequence

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_flow_execution
from preloop.models.models.flow_execution import (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
)
from preloop.models.schemas.flow import ModelRoutingConfig, ModelRoutingRule

logger = logging.getLogger(__name__)

AGENT_CONFIG_ROUTING_KEY = "model_routing"

# Keys that must never be treated as authorized model/harness overrides when
# they arrive on an untrusted event body (webhook, tracker, or an
# authenticated trigger). Presence of ``_resume`` in JSON is also not a
# trust signal; only a controller-owned function argument may pin selection.
_UNTRUSTED_OVERRIDE_KEYS = (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
    "ai_model_id",
    "assessment",
)

_DEFAULT_REASON = "No routing rule matched; using the flow selected model and harness."


class ModelRoutingError(ValueError):
    """Invalid routing policy or unusable selected model. Fail closed."""


def model_has_credential_source(model: models.AIModel) -> bool:
    """True when an AI model row has some way to authenticate at run time."""
    if model.credentials_secret_id or model.api_key:
        return True
    meta_data = model.meta_data if isinstance(model.meta_data, dict) else {}
    gateway = meta_data.get("gateway")
    return bool(isinstance(gateway, dict) and gateway.get("enabled"))


def model_usable_for_agent(model: models.AIModel, agent_type: str) -> bool:
    """True when the agent harness can actually reach this model.

    The codex harness talks to OpenAI directly, or to any other provider only
    through an explicit endpoint (custom provider / gateway); a model row
    without either fails inside the container. Other harnesses take the
    endpoint from the model row as-is, so the credential check suffices.
    """
    if not model_has_credential_source(model):
        return False
    if getattr(model, "model_kind", "llm") != "llm":
        return False
    if (agent_type or "").lower() == "codex":
        provider = (model.provider_name or "").strip().lower()
        if provider in ("openai", ""):
            return True
        meta_data = model.meta_data if isinstance(model.meta_data, dict) else {}
        gateway = meta_data.get("gateway")
        gateway_enabled = isinstance(gateway, dict) and gateway.get("enabled")
        return bool(model.api_endpoint or gateway_enabled)
    return True


def parse_model_routing(agent_config: Any) -> Optional[ModelRoutingConfig]:
    """Parse ``agent_config.model_routing`` or return None when absent.

    Raises:
        ModelRoutingError: If the stored document is present but invalid.
    """
    if not isinstance(agent_config, dict):
        return None
    raw = agent_config.get(AGENT_CONFIG_ROUTING_KEY)
    if raw is None:
        return None
    try:
        config = ModelRoutingConfig.model_validate(raw)
    except ValidationError as exc:
        raise ModelRoutingError(
            f"agent_config.model_routing is invalid: {exc}"
        ) from exc
    return config


def rule_matches_labels(rule: ModelRoutingRule, current_labels: Sequence[str]) -> bool:
    """Return True when ``rule`` matches the current label set (any AND all)."""
    present = {label for label in current_labels if label}
    any_labels = rule.labels.any or []
    all_labels = rule.labels.all or []
    if all_labels and not set(all_labels).issubset(present):
        return False
    if any_labels and not present.intersection(any_labels):
        return False
    if not any_labels and not all_labels:
        return False
    return True


def first_matching_rule(
    rules: Sequence[ModelRoutingRule], current_labels: Sequence[str]
) -> Optional[ModelRoutingRule]:
    """Return the first matching rule, or None when none match."""
    for rule in rules:
        if rule_matches_labels(rule, current_labels):
            return rule
    return None


def _label_name(item: Any) -> Optional[str]:
    """Return a label title from a string or GitHub/GitLab label object."""
    if isinstance(item, str) and item.strip():
        return item
    if isinstance(item, dict):
        name = item.get("name") or item.get("title")
        if isinstance(name, str) and name.strip():
            return name
    return None


def extract_trusted_labels(event_data: Optional[Dict[str, Any]]) -> List[str]:
    """Read one authoritative current-label array, including an empty array.

    Normalized ``payload.labels`` wins over provider snapshots. The singular
    ``label`` is an event delta, not current state, and is never merged in.
    """
    payload = (event_data or {}).get("payload")
    if not isinstance(payload, dict):
        return []
    for source in (
        payload,
        payload.get("issue"),
        payload.get("pull_request"),
        payload.get("object_attributes"),
    ):
        if isinstance(source, dict) and isinstance(source.get("labels"), list):
            return list(
                dict.fromkeys(
                    name for item in source["labels"] if (name := _label_name(item))
                )
            )
    return []


def strip_untrusted_overrides(event_data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop planted model/harness override keys from an event snapshot.

    Mutates and returns ``event_data``. Nested ``payload`` is stripped too so
    a webhook body cannot smuggle ``_matrix`` or ``ai_model_id``.
    """
    for key in _UNTRUSTED_OVERRIDE_KEYS:
        event_data.pop(key, None)
    payload = event_data.get("payload")
    if isinstance(payload, dict):
        for key in _UNTRUSTED_OVERRIDE_KEYS:
            payload.pop(key, None)
    return event_data


def _account_can_use_model(model: Optional[models.AIModel], account_id: Any) -> bool:
    if model is None:
        return False
    if model.account_id is None:
        return True
    if account_id is None:
        return False
    return str(model.account_id) == str(account_id)


def _model_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ModelRoutingError("ai_model_id must be a valid UUID") from exc


def load_usable_model(
    db: Session,
    *,
    ai_model_id: Any,
    agent_type: str,
    account_id: Any,
) -> models.AIModel:
    """Load an account-visible model that the harness can actually use.

    Raises:
        ModelRoutingError: Foreign, missing, or incompatible model.
    """
    from preloop.agents.factory import SUPPORTED_AGENT_TYPES

    harness = (agent_type or "").strip().lower()
    if harness not in SUPPORTED_AGENT_TYPES:
        raise ModelRoutingError(
            f"agent_type '{agent_type}' is not supported; "
            f"supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
        )
    model = crud_ai_model.get(db, id=_model_uuid(ai_model_id))
    if not _account_can_use_model(model, account_id):
        raise ModelRoutingError(f"ai_model_id '{ai_model_id}' not found")
    assert model is not None
    if not model_usable_for_agent(model, harness):
        raise ModelRoutingError(
            f"ai_model_id '{ai_model_id}' is not usable with agent_type '{harness}'"
        )
    return model


def validate_default_selection(
    db: Session, flow: models.Flow, *, agent_type: str, ai_model_id: Any
) -> None:
    """Validate defaults/pinned identity without widening rule or matrix targets.

    A named private Cursor profile uses the runner's local credentials and
    model map. The runtime still owns native capability checks and rejection of
    unsupported resume/publication paths. This forward-compatible boundary has
    no dependency on the optional native-runner implementation.
    """
    harness = (agent_type or "").strip().lower()
    if harness != "cursor":
        if ai_model_id is not None:
            load_usable_model(
                db,
                ai_model_id=ai_model_id,
                agent_type=harness,
                account_id=flow.account_id,
            )
        return
    config = flow.agent_config if isinstance(flow.agent_config, dict) else {}
    profile = config.get("host_exec_profile")
    pool = flow.runner_pool
    if (
        not isinstance(profile, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile.strip()) is None
        or not isinstance(pool, str)
        or not pool.strip()
        or pool.strip().lower() == "server"
    ):
        raise ModelRoutingError(
            "Cursor defaults require a named host profile and explicit private runner pool"
        )
    if ai_model_id is None:
        return
    model = crud_ai_model.get(db, id=_model_uuid(ai_model_id))
    if not _account_can_use_model(model, flow.account_id):
        raise ModelRoutingError(f"ai_model_id '{ai_model_id}' not found")
    if getattr(model, "model_kind", "llm") != "llm":
        raise ModelRoutingError("Private Cursor defaults require an LLM model")


def validate_stored_model_routing(
    db: Session, agent_config: Any, account_id: Any
) -> Optional[ModelRoutingConfig]:
    """Validate a stored policy, including model ownership and harness fit.

    Raises:
        ModelRoutingError: Invalid document or unusable rule target.
    """
    config = parse_model_routing(agent_config)
    if config is None:
        return None
    for rule in config.rules:
        load_usable_model(
            db,
            ai_model_id=rule.ai_model_id,
            agent_type=rule.agent_type,
            account_id=account_id,
        )
    return config


def _record(
    *,
    ai_model_id: Any,
    agent_type: Optional[str],
    source: str,
    reason: str,
    label_snapshot: Iterable[str],
    rule_id: Optional[str] = None,
    handoff: Optional[str] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema_version": 1,
        "ai_model_id": str(ai_model_id) if ai_model_id is not None else None,
        "agent_type": agent_type,
        "source": source,
        "reason": reason,
        "label_snapshot": list(label_snapshot),
    }
    if rule_id:
        record["rule_id"] = rule_id
    if handoff:
        record["handoff"] = handoff
    return record


def _is_routing_record(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and bool(value.get("agent_type") or value.get("ai_model_id"))
    )


def load_source_execution_for_flow(
    db: Session, flow: models.Flow, execution_id: Any
) -> Optional[models.FlowExecution]:
    """Load a persisted execution that belongs to this flow and account.

    Caller-supplied ids that do not exist, belong to another flow, or
    belong to another account return None. JSON fields never authorize
    this lookup; the caller must pass a controller-owned id.

    Args:
        db: Database session.
        flow: Flow that must own the source execution.
        execution_id: Candidate persisted execution id.

    Returns:
        The matching execution, or None when lineage checks fail.
    """
    try:
        execution_id = UUID(str(execution_id))
    except (ValueError, TypeError, AttributeError):
        return None
    prior = crud_flow_execution.get(
        db, id=str(execution_id), account_id=getattr(flow, "account_id", None)
    )
    if prior is None or str(prior.flow_id) != str(flow.id):
        return None
    return prior


def _persisted_details(execution: models.FlowExecution) -> Dict[str, Any]:
    details = execution.trigger_event_details
    return details if isinstance(details, dict) else {}


def _persisted_routing_record(
    execution: models.FlowExecution,
) -> Optional[Dict[str, Any]]:
    record = _persisted_details(execution).get(ROUTING_RECORD_KEY)
    if not _is_routing_record(record):
        return None
    return dict(record)


def _persisted_matrix(execution: models.FlowExecution) -> Optional[Dict[str, Any]]:
    matrix = _persisted_details(execution).get(MATRIX_OVERRIDES_KEY)
    if not isinstance(matrix, dict):
        return None
    if matrix.get("agent_type") or matrix.get("ai_model_id") or "index" in matrix:
        return dict(matrix)
    return None


def validate_authorized_matrix(
    db: Session, flow: models.Flow, cell: Dict[str, Any]
) -> Dict[str, Any]:
    """Re-check account/harness for a controller-validated eval matrix cell.

    Empty cells (flow defaults) are allowed. Foreign or missing models fail
    closed. Credential reachability is not required here: eval grids may
    include rows that obtain keys at runtime, matching ``_validate_matrix``.

    Args:
        db: Database session.
        flow: Flow that owns the batch.
        cell: Matrix cell already accepted by the controller.

    Returns:
        The same cell dict.

    Raises:
        ModelRoutingError: Unsupported harness or non-account-visible model.
    """
    from preloop.agents.factory import SUPPORTED_AGENT_TYPES

    if not isinstance(cell, dict):
        raise ModelRoutingError("authorized matrix cell must be an object")
    cell["agent_type"] = cell.get("agent_type") or flow.agent_type
    model_id = cell.get("ai_model_id") or flow.ai_model_id
    cell["ai_model_id"] = str(model_id) if model_id else None
    agent_type = cell.get("agent_type")
    if agent_type:
        harness = str(agent_type).strip().lower()
        cell["agent_type"] = harness
        if harness not in SUPPORTED_AGENT_TYPES:
            raise ModelRoutingError(
                f"agent_type '{agent_type}' is not supported; "
                f"supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
            )
    ai_model_id = cell.get("ai_model_id")
    if ai_model_id:
        model = crud_ai_model.get(db, id=_model_uuid(ai_model_id))
        if not _account_can_use_model(model, getattr(flow, "account_id", None)):
            raise ModelRoutingError(f"ai_model_id '{ai_model_id}' not found")
    return cell


def resolve_routing_record(
    db: Session,
    flow: models.Flow,
    event_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the controller routing record for this execution.

    The flow default is recorded even without a policy so later retries can
    prove their identity. Routing never silently substitutes another model.
    """
    labels = extract_trusted_labels(event_data)
    config = parse_model_routing(getattr(flow, "agent_config", None))
    account_id = getattr(flow, "account_id", None)
    matched = first_matching_rule(config.rules, labels) if config else None
    if matched is not None:
        load_usable_model(
            db,
            ai_model_id=matched.ai_model_id,
            agent_type=matched.agent_type,
            account_id=account_id,
        )
        return _record(
            ai_model_id=matched.ai_model_id,
            agent_type=matched.agent_type.strip().lower(),
            source="rule",
            reason=f"Matched routing rule '{matched.id}'.",
            label_snapshot=labels,
            rule_id=matched.id,
        )

    default_type = (flow.agent_type or "").strip().lower() or None
    default_model_id = flow.ai_model_id
    if default_type == "cursor" or (
        config and config.rules and default_model_id is not None
    ):
        validate_default_selection(
            db, flow, ai_model_id=default_model_id, agent_type=default_type or "codex"
        )
    elif config and config.rules and default_type:
        from preloop.agents.factory import SUPPORTED_AGENT_TYPES

        if default_type not in SUPPORTED_AGENT_TYPES:
            raise ModelRoutingError(
                f"agent_type '{default_type}' is not supported; "
                f"supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
            )
    return _record(
        ai_model_id=default_model_id,
        agent_type=default_type,
        source="default",
        reason=_DEFAULT_REASON,
        label_snapshot=labels,
    )


def revalidate_routing_record(
    db: Session, flow: models.Flow, record: Dict[str, Any]
) -> Dict[str, Any]:
    """Fail closed if a pinned record's model is no longer usable."""
    require_persisted_identity(record)
    agent_type = record["agent_type"]
    ai_model_id = record.get("ai_model_id")
    if ai_model_id:
        validate_default_selection(
            db, flow, ai_model_id=ai_model_id, agent_type=agent_type
        )
    return record


def prepare_execution_routing(
    db: Session,
    flow: models.Flow,
    event_data: Optional[Dict[str, Any]],
    *,
    source_execution: Optional[models.FlowExecution] = None,
    authorized_matrix: Optional[Dict[str, Any]] = None,
    pin_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Strip untrusted overrides and attach a trusted routing record.

    Caller-controlled fields are always untrusted, including authenticated
    request bodies. ``_resume`` / ``_matrix`` / ``_model_routing`` in the
    snapshot never authorize a selection. Pinning requires a persisted
    ``source_execution`` already loaded with account and lineage checks.
    Eval cells must be passed as ``authorized_matrix`` after API validation.

    Args:
        db: Database session.
        flow: Flow whose ``agent_config.model_routing`` is consulted.
        event_data: Trigger snapshot to copy and sanitize.
        source_execution: Persisted prior execution to pin, or None.
        authorized_matrix: Controller-validated eval cell to store, or None.
        pin_kind: ``retry`` or ``continuation`` when pinning; ignored otherwise.

    Returns:
        A sanitized snapshot with a frozen model/harness selection, from an
        authorized matrix, a persisted source, a matching rule, or defaults.

    Raises:
        ModelRoutingError: Unusable pinned model or unauthorized matrix cell.
    """
    details: Dict[str, Any] = deepcopy(event_data or {})
    strip_untrusted_overrides(details)

    if authorized_matrix is not None:
        details[MATRIX_OVERRIDES_KEY] = validate_authorized_matrix(
            db, flow, dict(authorized_matrix)
        )
        return details

    if source_execution is not None:
        if str(source_execution.flow_id) != str(flow.id):
            raise ModelRoutingError("source execution does not belong to this flow")
        persisted_matrix = _persisted_matrix(source_execution)
        if persisted_matrix is not None:
            require_persisted_identity(persisted_matrix)
            details[MATRIX_OVERRIDES_KEY] = validate_authorized_matrix(
                db, flow, persisted_matrix
            )
        persisted_record = _persisted_routing_record(source_execution)
        if persisted_record is not None:
            pinned = dict(persisted_record)
            pinned["source"] = "pinned"
            if pin_kind == "continuation":
                pinned["handoff"] = "native_continue"
            details[ROUTING_RECORD_KEY] = revalidate_routing_record(db, flow, pinned)
        if persisted_matrix is None and persisted_record is None:
            raise ModelRoutingError(
                "Prior execution model/harness identity is unavailable; start an explicit new execution."
            )
        return details

    record = resolve_routing_record(db, flow, details)
    if record is not None:
        details[ROUTING_RECORD_KEY] = record
    return details


def native_handoff_required(
    current: tuple[Optional[str], Optional[str]],
    prior: tuple[Optional[str], Optional[str]],
) -> bool:
    """True when model or harness changed and native restore must not run."""
    current_type, current_model = current
    prior_type, prior_model = prior
    current_type = (current_type or "").strip().lower() or None
    prior_type = (prior_type or "").strip().lower() or None
    current_model = str(current_model) if current_model else None
    prior_model = str(prior_model) if prior_model else None
    return (current_type, current_model) != (prior_type, prior_model)


def require_persisted_identity(record: Dict[str, Any]) -> tuple[str, str]:
    """Require a complete proven selection without consulting live defaults."""
    if (
        not isinstance(record, dict)
        or not record.get("agent_type")
        or not record.get("ai_model_id")
    ):
        raise ModelRoutingError(
            "Prior execution model/harness identity is incomplete; start an explicit new execution."
        )
    return str(record["agent_type"]), _model_uuid(record["ai_model_id"])


def validate_native_resume_identity(
    db: Session, flow: models.Flow, details: Dict[str, Any], resume: Dict[str, Any]
) -> None:
    """Reject unproven or changed identities before any native session restore."""
    prior = load_source_execution_for_flow(db, flow, resume.get("execution_id"))
    if prior is None:
        raise ModelRoutingError(
            "Native resume source identity is unavailable for this flow"
        )
    prior_details = _persisted_details(prior)
    prior_selection = require_persisted_identity(
        prior_details.get(MATRIX_OVERRIDES_KEY)
        or prior_details.get(ROUTING_RECORD_KEY)
        or {}
    )
    current_selection = require_persisted_identity(
        details.get(MATRIX_OVERRIDES_KEY) or details.get(ROUTING_RECORD_KEY) or {}
    )
    if native_handoff_required(current_selection, prior_selection):
        raise ModelRoutingError(
            "Native resume model/harness identity changed; start an explicit new execution."
        )
    revalidate_routing_record(
        db,
        flow,
        {"agent_type": current_selection[0], "ai_model_id": current_selection[1]},
    )
