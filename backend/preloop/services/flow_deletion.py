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

from preloop.models.crud import crud_audit_log, crud_flow, crud_flow_execution
from preloop.models.schemas import flow as flow_schemas
from preloop.utils.audit import log_config_change

logger = logging.getLogger(__name__)


#: Reason codes of a refused delete, sent in ``X-Preloop-Delete-Blocker``
#: and stored in the refusal audit row's ``details.reason_code``.
REASON_BUILT_IN_PRESET = "built_in_preset"
REASON_ACTIVE_EXECUTIONS = "active_executions"
REASON_REFERENCES = "references"
REASON_REFERENCE_RACE = "reference_race"


class FlowDeleteRefusedError(Exception):
    """The delete was refused and nothing changed.

    Attributes:
        status_code: The HTTP status the API answers with (403 or 409).
        detail: What blocks the delete and how to clear it.
        reason_code: One of the ``REASON_*`` codes.
        facts: Ids and counts behind the refusal, without names.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        reason_code: str = REASON_REFERENCES,
        facts: dict[str, Any] | None = None,
    ) -> None:
        """Record the API status, the readable reason and its code."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.reason_code = reason_code
        self.facts = facts or {}


def _blocker_facts(blockers: Any) -> dict[str, Any]:
    """Ids and counts of restricting references; names are left out."""
    return {
        "ci_identity_ids": [str(ref.id) for ref in blockers.ci_identities],
        "security_maintenance_release_ids": [
            str(ref.id) for ref in blockers.security_maintenance_releases
        ],
        "security_maintenance_baselines": blockers.security_maintenance_baselines,
        "other_account_references": blockers.other_account_references,
    }


def _audit_refusal(
    db: Session, *, flow_id: Any, account_id: Any, actor: Any, refused: Any
) -> None:
    """Write the refusal and its reason code to the audit trail.

    Best effort: a failed audit write must not turn the 409 into a 500.
    """
    try:
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=getattr(actor, "id", None),
            action="flow_delete_refused",
            resource_type="flow",
            resource_id=str(flow_id),
            status="denied",
            details={"reason_code": refused.reason_code, **refused.facts},
        )
    except Exception:
        db.rollback()
        logger.warning("Could not audit refused delete of flow %s", flow_id)


def delete_flow_checked(
    db: Session, *, flow: Any, account_id: Any, actor: Any
) -> flow_schemas.FlowResponse:
    """Delete one account flow, auditing a refusal with its reason code.

    Args:
        db: Database session; committed by the delete or the refusal audit.
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
    flow_id = flow.id
    try:
        return _delete_flow_checked(db, flow=flow, account_id=account_id, actor=actor)
    except FlowDeleteRefusedError as refused:
        _audit_refusal(
            db, flow_id=flow_id, account_id=account_id, actor=actor, refused=refused
        )
        raise


def _delete_flow_checked(
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
        raise FlowDeleteRefusedError(
            403, "Cannot delete built-in flow presets", REASON_BUILT_IN_PRESET
        )

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
            REASON_ACTIVE_EXECUTIONS,
            {
                "active_execution_count": len(active_executions),
                "active_execution_ids": [
                    str(execution.id) for execution in active_executions[:20]
                ],
            },
        )

    blockers = crud_flow.get_delete_blockers(db, flow_id=flow.id, account_id=account_id)
    if blockers:
        raise FlowDeleteRefusedError(
            409,
            _describe_delete_blockers(blockers),
            REASON_REFERENCES,
            _blocker_facts(blockers),
        )

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
            REASON_REFERENCE_RACE,
        ) from None

    log_config_change(
        db,
        user=actor,
        config_type="flow",
        action="deleted",
        old_value={"id": str(flow_id), "name": flow_name},
    )
    return response
