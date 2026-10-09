"""Consent searches remain tenant-scoped and include standalone denied calls."""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log


@pytest.mark.parametrize("action", ["tool_call", "policy_deny"])
def test_consent_search_is_exact_account_scoped_and_paginated(
    db_session: Session, test_user: models.User, action: str
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic other account"}
    )
    wanted = []
    for account_id, reference in [
        (test_user.account_id, "consent-example"),
        (test_user.account_id, "consent-example"),
        (test_user.account_id, "consent-example-other"),
        (other.id, "consent-example"),
        (test_user.account_id, None),
    ]:
        details: dict[str, Any] = {"grant": {"consent_ref": reference}}
        row = crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            action=action,
            status="denied",
            resource_type="tool",
            resource_id="read_record",
            details=details,
        )
        if account_id == test_user.account_id and reference == "consent-example":
            wanted.append(row.id)
    args = {"account_id": test_user.account_id, "consent_ref": "consent-example"}
    rows = crud_audit_log.get_by_account(db_session, **args, limit=1)
    assert len(rows) == 1 and rows[0].id in wanted
    assert crud_audit_log.count_by_account(db_session, **args) == 2
    groups, total = crud_audit_log.get_grouped_by_correlation(
        db_session, **args, limit=1
    )
    assert total == 2 and len(groups) == 1
    assert groups[0]["primary_event"].id in wanted
    next_rows = crud_audit_log.get_by_account(db_session, **args, skip=1, limit=1)
    assert next_rows[0].id != rows[0].id
