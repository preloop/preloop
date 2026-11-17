"""The one flow delete path, shared by the API and operator maintenance.

Running executions and restricting references block the delete before
anything changes; executions and their logs cascade with the flow; the
deletion is written to the audit trail.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas import flow as flow_schemas
from preloop.utils.audit import log_config_change

logger = logging.getLogger(__name__)


class FlowDeleteRefusedError(Exception):
    """The delete was refused and nothing changed.

    Attributes:
        status_code: The HTTP status the API answers with (403 or 409).
        detail: What blocks the delete and how to clear it.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """Record the API status and the operator-readable reason."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def delete_flow_checked(
    db: Session, *, flow: Any, account_id: Any, actor: Any
) -> flow_schemas.FlowResponse:
    """Delete one account flow with the API's blockers, cascade and audit.

    Args:
        db: Database session; committed by the delete.
        flow: The flow, already resolved within ``account_id``.
        account_id: The account that owns the flow.
        actor: The user (or system actor with ``id`` and ``account_id``)
            recorded in the audit trail.

    Returns:
        The flow as it was before the delete.

    Raises:
        FlowDeleteRefusedError: Built-in preset (403), running executions or
            restricting references (409).
    """
    from preloop.api.endpoints.flows import _describe_delete_blockers

    if flow.is_preset and flow.account_id is None:
        raise FlowDeleteRefusedError(403, "Cannot delete built-in flow presets")

    # A running agent would keep streaming logs for rows the cascade removes.
    active_executions = crud_flow_execution.get_running_by_flow(
        db, flow_id=flow.id, account_id=account_id
    )
    if active_executions:
        raise FlowDeleteRefusedError(
            409,
            f"Flow has {len(active_executions)} active execution(s). "
            "Stop them first (POST /api/v1/flows/executions/{id}/command "
            'with {"command": "stop"}) and retry the delete.',
        )

    blockers = crud_flow.get_delete_blockers(db, flow_id=flow.id, account_id=account_id)
    if blockers:
        raise FlowDeleteRefusedError(409, _describe_delete_blockers(blockers))

    # Validate before mutating: a row that fails FlowResponse stays for
    # inspection rather than being deleted and then failing the response.
    response = flow_schemas.FlowResponse.model_validate(flow)
    flow_id, flow_name = flow.id, flow.name
    try:
        crud_flow.remove(db=db, id=flow_id, account_id=account_id)
    except IntegrityError:
        db.rollback()
        logger.warning("Flow %s delete hit a foreign key restriction", flow_id)
        raise FlowDeleteRefusedError(
            409,
            "Flow cannot be deleted because other records still reference "
            "it. Nothing was changed. Disable the flow instead, or remove "
            "the references and retry.",
        ) from None

    log_config_change(
        db,
        user=actor,
        config_type="flow",
        action="deleted",
        old_value={"id": str(flow_id), "name": flow_name},
    )
    return response
