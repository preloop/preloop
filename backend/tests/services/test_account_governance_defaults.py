"""Account-wide governance defaults with per-agent inherit/override.

The resolution chain for native tool approvals is: explicit per-agent value
-> account default -> enforce. Overrides are bidirectional: a per-agent
"enforce" shields an agent from an account default of "off", and a per-agent
"off" bypasses approvals even when the account default is "enforce"/unset.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.services.agent_permission_service import (
    _account_defaults_governance_field,
    _native_tool_approvals_disabled,
)
from preloop.services.subject_governance import (
    get_account_governance_defaults,
    normalize_subject_governance_store,
    sanitize_account_governance_defaults,
    set_account_governance_defaults,
    set_subject_governance,
    SUBJECT_TYPE_MANAGED_AGENTS,
)


class _AsyncDBShim:
    """Adapt the sync test session to the service's async execute call."""

    def __init__(self, db_session):
        self._db = db_session

    async def execute(self, statement):
        return self._db.execute(statement)


def _write_meta(db_session, test_user, meta):
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(db_session, db_obj=account, obj_in={"meta_data": meta})
    return account


@pytest.mark.asyncio
async def test_resolution_chain_on_real_database(db_session, test_user):
    """All chain combinations execute the real JSON-path SQL on Postgres."""
    agent_off = uuid.uuid4()
    agent_enforce = uuid.uuid4()
    agent_inherit = uuid.uuid4()

    meta = {
        "subject_governance": {
            "managed_agents": {
                str(agent_off): {"native_tool_approvals": "off"},
                str(agent_enforce): {"native_tool_approvals": "enforce"},
                # agent_inherit deliberately absent.
            },
            "account_defaults": {"native_tool_approvals": "off"},
        }
    }
    _write_meta(db_session, test_user, meta)
    db = _AsyncDBShim(db_session)
    account_id = str(test_user.account_id)

    assert await _native_tool_approvals_disabled(db, account_id, agent_off) is True
    # Explicit per-agent "enforce" shields against the account "off" default.
    assert await _native_tool_approvals_disabled(db, account_id, agent_enforce) is False
    # Absent per-agent setting inherits the account default.
    assert await _native_tool_approvals_disabled(db, account_id, agent_inherit) is True


@pytest.mark.asyncio
async def test_enforce_default_when_nothing_set(db_session, test_user):
    """No per-agent value and no account default -> enforce (fail safe)."""
    _write_meta(db_session, test_user, {"subject_governance": {}})
    db = _AsyncDBShim(db_session)
    assert (
        await _native_tool_approvals_disabled(
            db, str(test_user.account_id), uuid.uuid4()
        )
        is False
    )


def test_account_defaults_field_sql_shape(db_session, test_user):
    """The account-defaults JSON extraction executes on real Postgres."""
    _write_meta(
        db_session,
        test_user,
        {"subject_governance": {"account_defaults": {"native_tool_approvals": "off"}}},
    )
    value = db_session.execute(
        select(_account_defaults_governance_field("native_tool_approvals"))
        .select_from(models.Account)
        .where(models.Account.id == test_user.account_id)
        .limit(1)
    ).scalar()
    assert value == "off"


def test_defaults_survive_per_agent_governance_writes():
    """A per-agent governance save must not erase account defaults.

    set_subject_governance round-trips the whole store through
    normalize_subject_governance_store; if that drops account_defaults, any
    unrelated per-agent save silently resets the account default.
    """
    meta = set_account_governance_defaults(
        {}, defaults={"native_tool_approvals": "off"}
    )
    meta = set_subject_governance(
        meta,
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id=str(uuid.uuid4()),
        config={"native_tool_approvals": "enforce"},
    )
    assert get_account_governance_defaults(meta).get("native_tool_approvals") == "off"


def test_sanitize_account_defaults_rejects_unknown_values():
    sanitized = sanitize_account_governance_defaults(
        {
            "native_tool_approvals": "BANANAS",
            "approval_workflow_id": "  wf-1  ",
            "tool_rules": {"Bash": []},  # not an account-default field
        }
    )
    assert sanitized == {
        "native_tool_approvals": None,
        "approval_workflow_id": "wf-1",
    }
    assert (
        sanitize_account_governance_defaults({"native_tool_approvals": " OFF "})[
            "native_tool_approvals"
        ]
        == "off"
    )


def test_normalize_store_preserves_defaults_bucket():
    store = normalize_subject_governance_store(
        {
            "subject_governance": {
                "managed_agents": {"a": {}},
                "account_defaults": {"native_tool_approvals": "off"},
            }
        }
    )
    assert store["account_defaults"] == {"native_tool_approvals": "off"}
    # Absent/malformed bucket normalizes to empty dict, never crashes.
    assert normalize_subject_governance_store({})["account_defaults"] == {}
    assert (
        normalize_subject_governance_store(
            {"subject_governance": {"account_defaults": "garbage"}}
        )["account_defaults"]
        == {}
    )
