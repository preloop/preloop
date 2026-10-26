"""Install the default Delivery Report schedule when an account connects a tracker.

An account needs one report flow, regardless of how many trackers it connects.
The policy still decides which repositories are in scope and whether a run may
call a model. This function is safe to call again on reconnect.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.flow_presets import FLOW_PRESETS, PRESET_SLUGS
from preloop.models.crud import crud_delivery_report_policy, crud_flow
from preloop.models.schemas.flow import FlowCreate
from preloop.services.delivery_report import is_delivery_report_flow, resolve_policy
from preloop.utils.hashing import compute_content_hash

logger = logging.getLogger(__name__)
PRESET_SLUG = "delivery-report"


def ensure_default_report_flow(db: Session, *, account_id: UUID) -> Any | None:
    """Create one enabled report flow with the account's effective defaults.

    Args:
        db: The transaction that connects a tracker.
        account_id: The connected tracker's account.

    Returns:
        The existing or new flow, or None when the preset is unavailable.
    """
    # Both tracker endpoints commit after this call. Hold the account row
    # through the lookup and insert so concurrent connections install one flow.
    crud_delivery_report_policy.lock_account_for_activation(db, account_id=account_id)
    for flow in crud_flow.get_by_account(db, account_id=account_id, limit=1000):
        if is_delivery_report_flow(flow):
            return flow

    name = PRESET_SLUGS.get(PRESET_SLUG)
    if name is None:
        logger.warning("Delivery Report preset unavailable; no schedule created")
        return None
    preset_config = next(
        (item for item in FLOW_PRESETS if item.get("name") == name), None
    )
    if preset_config is None:
        logger.warning("Delivery Report preset unavailable; no schedule created")
        return None

    source = crud_flow.get_global_preset_by_name(db, name=name)
    model_source = source or SimpleNamespace(
        ai_model_id=None, agent_type=preset_config["agent_type"]
    )
    model_id = None
    try:
        from preloop.api.endpoints.flows import _resolve_clone_model_binding

        model_id = _resolve_clone_model_binding(db, model_source, account_id)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        # The deterministic report can run without model credentials. Its
        # onboarding assessment waits until the flow is given a model.
        logger.info("No report model is available for account %s", account_id)

    policy = resolve_policy(
        crud_delivery_report_policy.get_overrides(db, account_id=account_id)
    )
    config = dict(preset_config)
    agent_config = dict(config["agent_config"])
    agent_config["limits"] = {
        **dict(agent_config.get("limits") or {}),
        **policy.execution_limits(),
    }
    config.update(
        name=name,
        account_id=str(account_id),
        is_preset=False,
        is_enabled=policy.enabled and policy.cadence != "manual",
        schedule_config=policy.schedule_config(),
        agent_config=agent_config,
        ai_model_id=model_id,
    )
    if source is not None:
        config.update(
            source_preset_id=str(source.id),
            source_prompt_hash=compute_content_hash(source.prompt_template),
            source_tools_hash=compute_content_hash(source.allowed_mcp_tools or []),
        )
    # The tracker and flow commit together, including managed OAuth binds.
    return crud_flow.create(
        db, flow_in=FlowCreate(**config), account_id=account_id, commit=False
    )
