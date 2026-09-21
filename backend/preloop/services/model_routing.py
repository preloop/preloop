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
from preloop.models.schemas.flow import (
    ModelByLabelConfig,
    ModelByLabelRule,
    ModelRoutingConfig,
    ModelRoutingRule,
)
from preloop.services.runner_service import (
    _account_default_runner_pool,
    _explicit_pool,
    _is_server_pool,
)

logger = logging.getLogger(__name__)

AGENT_CONFIG_ROUTING_KEY = "model_routing"

#: The short label-to-model shape (#851). Evaluated after ``model_routing``
#: by the same engine, so an operator who writes both gets one answer.
AGENT_CONFIG_BY_LABEL_KEY = "model_by_label"

#: Prefix for the rule id a desugared label rule reports on the execution.
#: It is not a stored id: ``model_by_label`` entries are keyed by their label.
BY_LABEL_RULE_PREFIX = "by-label:"

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


def parse_model_by_label(agent_config: Any) -> Optional[ModelByLabelConfig]:
    """Parse ``agent_config.model_by_label`` or return None when absent.

    Raises:
        ModelRoutingError: If the stored document is present but invalid.
    """
    if not isinstance(agent_config, dict):
        return None
    raw = agent_config.get(AGENT_CONFIG_BY_LABEL_KEY)
    if raw is None:
        return None
    try:
        return ModelByLabelConfig.model_validate(raw)
    except ValidationError as exc:
        raise ModelRoutingError(
            f"agent_config.model_by_label is invalid: {exc}"
        ) from exc


def by_label_rules(
    config: Optional[ModelByLabelConfig], flow: models.Flow
) -> List[tuple[ModelRoutingRule, ModelByLabelRule]]:
    """Desugar ``model_by_label`` entries into ordinary routing rules (#851).

    The short shape says "this label, that model, that effort". It is the
    same decision the ``model_routing`` engine already makes, so it is turned
    into rules and handed to that engine rather than being matched by a
    second implementation that could disagree with it. An entry that omits
    the model or the harness inherits the flow's selection, which is what
    "run the flow's model but think harder" has to mean.

    Args:
        config: Parsed ``model_by_label`` list, or None.
        flow: Flow whose selection fills in what an entry leaves out.

    Returns:
        Pairs of (desugared rule, original entry), in stored order.

    Raises:
        ModelRoutingError: An entry has nothing to run on, because it names
            neither a model nor a harness and the flow selected none either.
    """
    if config is None:
        return []
    default_model_id = flow.ai_model_id
    default_type = (getattr(flow, "agent_type", "") or "").strip().lower() or None
    desugared: List[tuple[ModelRoutingRule, ModelByLabelRule]] = []
    for index, entry in enumerate(config.root, start=1):
        model_id = entry.ai_model_id or default_model_id
        agent_type = entry.agent_type or default_type
        if not model_id or not agent_type:
            raise ModelRoutingError(
                f"model_by_label rule '{entry.label}' cannot run: it names "
                "no model or harness and the flow has no selected model and "
                "harness to fall back on"
            )
        desugared.append(
            (
                ModelRoutingRule(
                    id=f"by-label-{index}",
                    labels={"any": [entry.label]},
                    ai_model_id=model_id,
                    agent_type=agent_type,
                ),
                entry,
            )
        )
    return desugared


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


def _require_hosted_routing_harness(agent_type: str) -> str:
    """Reject native Cursor in rules/matrices; it is a flow-default runtime."""
    from preloop.agents.factory import SUPPORTED_AGENT_TYPES

    harness = (agent_type or "").strip().lower()
    if harness == "cursor":
        raise ModelRoutingError(
            "Routing rules and eval matrices cannot select agent_type "
            "'cursor'; native Cursor is the flow default with a named host "
            "profile on a private runner"
        )
    if harness not in SUPPORTED_AGENT_TYPES:
        raise ModelRoutingError(
            f"agent_type '{agent_type}' is not supported; "
            f"supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
        )
    return harness


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
    harness = _require_hosted_routing_harness(agent_type)
    model = crud_ai_model.get(db, id=_model_uuid(ai_model_id))
    if not _account_can_use_model(model, account_id):
        raise ModelRoutingError(f"ai_model_id '{ai_model_id}' not found")
    assert model is not None
    if not model_usable_for_agent(model, harness):
        raise ModelRoutingError(
            f"ai_model_id '{ai_model_id}' is not usable with agent_type '{harness}'"
        )
    return model


def _require_environment_profile_harness(agent_config: Any, agent_type: str) -> None:
    """Reject a selected harness that cannot run the flow's environment profile."""
    if not isinstance(agent_config, dict) or not agent_config.get(
        "environment_profile"
    ):
        return
    from preloop.services.flow_environment import resolve_profile

    harness = (agent_type or "").strip().lower()
    try:
        resolve_profile(agent_config, agent_type=harness, runner="server")
    except ValueError as exc:
        if str(exc) == "environment_harness_mismatch":
            raise ModelRoutingError(
                "environment_profile "
                f"{agent_config['environment_profile']!r} does not support "
                f"agent_type {harness!r}"
            ) from exc
        raise ModelRoutingError(str(exc)) from exc


def validate_default_selection(
    db: Session, flow: models.Flow, *, agent_type: str, ai_model_id: Any
) -> None:
    """Validate defaults/pinned identity without widening rule or matrix targets.

    A named private Cursor profile uses the runner's local credentials and
    model map. The pool check matches ``resolve_runner_pool`` for the
    flow-level and account-default steps (not the "any online runner" auto
    fallback): ``flow.runner_pool``, then ``account.default_runner_pool``.
    The runtime still owns native capability checks and rejection of
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
    pool = _explicit_pool(getattr(flow, "runner_pool", None))
    if pool is None:
        pool = _explicit_pool(_account_default_runner_pool(flow, db))
    if (
        not isinstance(profile, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile.strip()) is None
        or pool is None
        or _is_server_pool(pool)
    ):
        raise ModelRoutingError(
            "Cursor defaults require a named host profile and a private "
            "runner pool (flow.runner_pool or account.default_runner_pool)"
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
    by_label = parse_model_by_label(agent_config)
    if by_label is not None:
        # Validated without the flow's defaults, which a create request has
        # not stored yet: only the targets an entry names itself can be
        # checked here, and resolve time checks the rest.
        for entry in by_label.root:
            if entry.agent_type:
                _require_environment_profile_harness(agent_config, entry.agent_type)
            if entry.ai_model_id is None:
                continue
            if entry.agent_type:
                load_usable_model(
                    db,
                    ai_model_id=entry.ai_model_id,
                    agent_type=entry.agent_type,
                    account_id=account_id,
                )
                continue
            # No harness named, so harness fit is the flow's business at
            # resolve time. Ownership is still checked here: a model id from
            # another account must never be storable.
            model = crud_ai_model.get(db, id=_model_uuid(entry.ai_model_id))
            if not _account_can_use_model(model, account_id):
                raise ModelRoutingError(f"ai_model_id '{entry.ai_model_id}' not found")
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
        _require_environment_profile_harness(agent_config, rule.agent_type)
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
    matched_label: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
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
    if matched_label:
        record["matched_label"] = matched_label
    if reasoning_effort:
        record["reasoning_effort"] = reasoning_effort
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


def is_model_usable_and_gateway_enabled(
    db: Session,
    flow: models.Flow,
    ai_model_id: Any,
    agent_type: str,
) -> bool:
    """Check whether a model exists, is account-visible, and usable by the harness.

    Also checks that if gateway is configured, gateway.enabled is not False.
    """
    if not ai_model_id:
        return False
    try:
        model_id = _model_uuid(ai_model_id)
    except ModelRoutingError:
        return False
    model = crud_ai_model.get(db, id=model_id)
    if model is None:
        return False
    if not _account_can_use_model(model, getattr(flow, "account_id", None)):
        return False
    meta_data = model.meta_data if isinstance(model.meta_data, dict) else {}
    gateway = meta_data.get("gateway")
    if isinstance(gateway, dict) and gateway.get("enabled") is False:
        return False
    harness = (agent_type or "").strip().lower()
    if harness != "cursor":
        return model_usable_for_agent(model, harness)
    return getattr(model, "model_kind", "llm") == "llm"


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
    if not isinstance(cell, dict):
        raise ModelRoutingError("authorized matrix cell must be an object")
    derived: List[str] = list(cell.get("derived") or [])
    if not cell.get("agent_type") and flow.agent_type:
        cell["agent_type"] = flow.agent_type
        if "agent_type" not in derived:
            derived.append("agent_type")
    if not cell.get("ai_model_id") and flow.ai_model_id:
        cell["ai_model_id"] = str(flow.ai_model_id)
        if "ai_model_id" not in derived:
            derived.append("ai_model_id")
    elif cell.get("ai_model_id"):
        cell["ai_model_id"] = str(cell["ai_model_id"])
    if derived:
        cell["derived"] = derived
    agent_type = cell.get("agent_type")
    if agent_type:
        harness = _require_hosted_routing_harness(agent_type)
        cell["agent_type"] = harness
        _require_environment_profile_harness(
            getattr(flow, "agent_config", None), harness
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
    agent_config = getattr(flow, "agent_config", None)
    config = parse_model_routing(agent_config)
    account_id = getattr(flow, "account_id", None)
    # One ordered list, one first-match decision. The explicit rules come
    # first because they are the richer shape an operator reached for on
    # purpose; the short label rules follow in stored order (#851).
    label_pairs = by_label_rules(parse_model_by_label(agent_config), flow)
    label_entries = {rule.id: entry for rule, entry in label_pairs}
    ordered_rules = list(config.rules if config else []) + [
        rule for rule, _ in label_pairs
    ]
    matched = first_matching_rule(ordered_rules, labels)
    if matched is not None:
        load_usable_model(
            db,
            ai_model_id=matched.ai_model_id,
            agent_type=matched.agent_type,
            account_id=account_id,
        )
        _require_environment_profile_harness(agent_config, matched.agent_type)
        entry = label_entries.get(matched.id)
        if entry is None:
            return _record(
                ai_model_id=matched.ai_model_id,
                agent_type=matched.agent_type.strip().lower(),
                source="rule",
                reason=f"Matched routing rule '{matched.id}'.",
                label_snapshot=labels,
                rule_id=matched.id,
            )
        reason = f"Matched label rule for '{entry.label}'."
        if entry.reasoning_effort:
            reason += f" Reasoning effort {entry.reasoning_effort}."
        return _record(
            ai_model_id=matched.ai_model_id,
            agent_type=matched.agent_type.strip().lower(),
            source="label",
            reason=reason,
            label_snapshot=labels,
            rule_id=matched.id,
            matched_label=entry.label,
            reasoning_effort=entry.reasoning_effort,
        )

    default_type = (flow.agent_type or "").strip().lower() or None
    default_model_id = flow.ai_model_id
    if default_type == "cursor" or (ordered_rules and default_model_id is not None):
        validate_default_selection(
            db, flow, ai_model_id=default_model_id, agent_type=default_type or "codex"
        )
    elif ordered_rules and default_type:
        from preloop.agents.factory import SUPPORTED_AGENT_TYPES

        if default_type not in SUPPORTED_AGENT_TYPES:
            raise ModelRoutingError(
                f"agent_type '{default_type}' is not supported; "
                f"supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
            )
    if default_type:
        _require_environment_profile_harness(
            getattr(flow, "agent_config", None), default_type
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
    _require_environment_profile_harness(
        getattr(flow, "agent_config", None), agent_type
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
        persisted_record = _persisted_routing_record(source_execution)
        if persisted_matrix is None and persisted_record is None:
            raise ModelRoutingError(
                "Prior execution model/harness identity is unavailable; start an explicit new execution."
            )

        if pin_kind == "retry":
            if persisted_matrix is not None:
                # Acceptance criteria 3, 4, 5:
                # Strip derived overrides so they re-resolve from the current flow.
                # Keep index, batch_id, and user-supplied agent_type.
                # If user-supplied ai_model_id is deleted or no longer gateway-enabled,
                # fall back to flow model with a note.
                matrix_retry = dict(persisted_matrix)
                # Pre-#803 cells have no derived marker and stay pinned.
                derived = set(matrix_retry.get("derived") or [])
                for key in derived:
                    matrix_retry.pop(key, None)
                matrix_retry.pop("derived", None)

                note = None
                if "ai_model_id" in matrix_retry:
                    candidate_model_id = matrix_retry["ai_model_id"]
                    candidate_harness = (
                        matrix_retry.get("agent_type") or flow.agent_type or "codex"
                    )
                    if not is_model_usable_and_gateway_enabled(
                        db, flow, candidate_model_id, candidate_harness
                    ):
                        matrix_retry["ai_model_id"] = (
                            str(flow.ai_model_id) if flow.ai_model_id else None
                        )
                        note = f"Matrix model override {candidate_model_id} unavailable or gateway-disabled; fell back to flow model."

                validated = validate_authorized_matrix(db, flow, matrix_retry)
                details[MATRIX_OVERRIDES_KEY] = validated

                reason = f"Retry of execution {source_execution.id}."
                if note:
                    reason += f" {note}"
                labels = extract_trusted_labels(details)
                details[ROUTING_RECORD_KEY] = _record(
                    ai_model_id=validated.get("ai_model_id"),
                    agent_type=validated.get("agent_type"),
                    source="retry",
                    reason=reason,
                    label_snapshot=labels,
                )
            else:
                # Acceptance criteria 1, 2, 6:
                # Re-resolve model and harness from current flow and write fresh _model_routing
                fresh_record = resolve_routing_record(db, flow, details)
                prior_model_id = (persisted_record or {}).get("ai_model_id")
                prior_note = None
                if prior_model_id:
                    try:
                        prior_uuid = _model_uuid(prior_model_id)
                        prior_model = crud_ai_model.get(db, id=prior_uuid)
                        if not _account_can_use_model(
                            prior_model, getattr(flow, "account_id", None)
                        ):
                            prior_note = f"Prior model {prior_model_id} is retired or unavailable."
                    except Exception:
                        prior_note = (
                            f"Prior model {prior_model_id} is retired or unavailable."
                        )

                fresh_record["source"] = "retry"
                reason = f"Retry of execution {source_execution.id}. Re-resolved model and harness from current flow."
                if fresh_record.get("rule_id"):
                    reason += f" Matched routing rule '{fresh_record['rule_id']}'."
                if prior_note:
                    reason += f" {prior_note}"
                fresh_record["reason"] = reason
                details[ROUTING_RECORD_KEY] = fresh_record

            return details

        # For continuation or other pin_kind
        if persisted_matrix is not None:
            require_persisted_identity(persisted_matrix)
            details[MATRIX_OVERRIDES_KEY] = validate_authorized_matrix(
                db, flow, persisted_matrix
            )
        if persisted_record is not None:
            pinned = dict(persisted_record)
            pinned["source"] = "pinned"
            if pin_kind == "continuation":
                pinned["handoff"] = "native_continue"
            details[ROUTING_RECORD_KEY] = revalidate_routing_record(db, flow, pinned)
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
