"""Persist native prompt delivery independently of socket acknowledgement."""

from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_agent_control_command, crud_runtime_session


def record_prompt_delivery(
    db: Session, *, account_id: Any, agent_id: Any, command_id: str, state: str
) -> None:
    """Update the prompt receipt and its existing transcript row, if bound."""
    if state not in {"queued", "sent"}:
        return
    command = crud_agent_control_command.get_by_command_id(
        db, account_id=account_id, managed_agent_id=agent_id, command_id=command_id
    )
    if (
        command is None
        or command.kind != "command"
        or command.status in {"failed", "expired", "cancelled"}
    ):
        return
    if crud_agent_control_command.command_result_payload(command) is not None:
        return
    command.envelope = {**command.envelope, "_prompt_delivery": state}
    rows = (
        db.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.account_id
            == (command.consuming_account_id or account_id),
            models.RuntimeSessionActivity.metadata_["command_id"].astext == command_id,
            models.RuntimeSessionActivity.activity_type == "agent_control_message",
        )
        .all()
    )
    for row in rows:
        row.metadata_ = {**(row.metadata_ or {}), "prompt_delivery": state}
    if state == "sent" and command.runtime_session_id:
        crud_runtime_session.record_control_turn(
            db,
            account_id=command.consuming_account_id or account_id,
            runtime_session_id=command.runtime_session_id,
        )
    db.flush()
