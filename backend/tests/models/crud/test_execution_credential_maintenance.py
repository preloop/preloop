"""Historical masking validates candidate credentials against owned key hashes."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.execution_credential_maintenance import discover_values


def test_only_matching_key_values_are_selected() -> None:
    """Token-shaped prose and keys owned by other executions stay unchanged."""
    token = "flow_" + "a" * 43
    unrelated = "flow_" + "b" * 43
    hashes = {hashlib.sha256(token.encode()).hexdigest()}
    found = discover_values(
        {"output": token + " " + unrelated}, key_hashes=hashes, execution_id=uuid4()
    )
    assert found == {token}
    assert token not in json.dumps({"candidates": len(found)})


def test_dry_run_apply_and_idempotence(db_session: Session) -> None:
    """Maintenance counts before writing and masks execution and session copies."""
    from datetime import datetime, UTC
    from preloop.models import models
    from preloop.models.crud.execution_credential_maintenance import (
        redact_stored_execution_credentials,
    )

    account = models.Account(organization_name="Example Org", is_active=True)
    db_session.add(account)
    db_session.flush()
    flow = models.Flow(
        account_id=account.id,
        name="Example flow",
        agent_type="codex",
        agent_config={},
        prompt_template="Example task",
        trigger_event_source="manual",
        trigger_event_types=[],
    )
    db_session.add(flow)
    db_session.flush()
    token = "flow_" + "c" * 43
    execution = models.FlowExecution(
        flow_id=flow.id, status="SUCCEEDED", model_output_summary=token
    )
    db_session.add(execution)
    db_session.flush()
    key = models.ApiKey(
        account_id=account.id,
        name="Example runtime",
        key=token,
        scopes=["mcp:read"],
        is_active=False,
        context_data={"flow_execution_id": str(execution.id)},
    )
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        started_at=datetime.now(UTC),
    )
    db_session.add_all([key, session])
    db_session.flush()
    log = models.FlowExecutionLog(
        execution_id=execution.id, message=token, metadata_={"output": token}
    )
    activity = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=session.id,
        flow_execution_id=execution.id,
        activity_type="tool_call",
        summary=token,
        metadata_={"output": token},
    )
    db_session.add_all([log, activity])
    db_session.flush()
    args = {"account_id": account.id, "execution_ids": [execution.id]}
    dry = redact_stored_execution_credentials(db_session, **args)
    assert dry["executions_changed"] == 1
    assert log.message == token and activity.summary == token
    applied = redact_stored_execution_credentials(db_session, **args, apply=True)
    assert applied == dry
    assert (
        log.message
        == activity.summary
        == execution.model_output_summary
        == "[REDACTED]"
    )
    assert (
        redact_stored_execution_credentials(db_session, **args).get(
            "executions_changed", 0
        )
        == 0
    )


def test_registry_extends_without_plaintext_and_masks_late_output(
    db_session: Session,
) -> None:
    """Fresh reads see later credentials and terminal rows retain their masks."""
    from preloop.models import models
    from preloop.models.crud.execution_credentials import register, for_execution
    from preloop.models.crud.flow_execution_log import crud_flow_execution_log

    account = models.Account(organization_name="Example Org", is_active=True)
    db_session.add(account)
    db_session.flush()
    flow = models.Flow(
        account_id=account.id,
        name="Example flow",
        agent_type="codex",
        agent_config={},
        prompt_template="Task",
        trigger_event_source="manual",
        trigger_event_types=[],
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="SUCCEEDED")
    db_session.add(execution)
    db_session.flush()
    first, second = "synthetic-first-credential", "synthetic-refreshed-credential"
    register(db_session, execution.id, [first])
    register(db_session, execution.id, [second])
    db_session.expire_all()
    masker = for_execution(db_session, execution.id)
    assert first not in json.dumps(masker.fingerprints)
    assert second not in json.dumps(masker.fingerprints)
    assert masker.mask(first + " " + second) == "[REDACTED] [REDACTED]"
    row = crud_flow_execution_log.append_log(
        db_session,
        str(execution.id),
        {"message": first, "metadata": {"late": second}},
        commit=False,
    )
    assert row.message == "[REDACTED]"
    assert row.metadata_ == {"late": "[REDACTED]"}
