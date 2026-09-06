"""Native profile advertisements survive PostgreSQL CRUD roundtrips."""

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services.host_exec import (
    normalize_host_exec_advertisements,
    runner_has_host_exec_profile,
)


def test_runner_capabilities_default_and_advertisement_roundtrip(
    db_session: Session,
) -> None:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Local profile test"}
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "profile-runner",
            "token_hash": "test-token",
        },
    )
    assert runner.capabilities == {}
    capabilities = normalize_host_exec_advertisements(
        [
            {
                "name": "local",
                "capabilities": ["host_exec", "cursor_cli"],
                "models": ["team-fast"],
                "executable": "/private/path",
                "env": {"KEY": "secret"},
            }
        ]
    )
    runner = crud_flow_runner.update(
        db_session, db_obj=runner, obj_in={"capabilities": capabilities}
    )
    runner_id = runner.id
    db_session.expire_all()
    loaded = crud_flow_runner.get(db_session, id=runner_id)
    assert loaded.capabilities == capabilities
    assert runner_has_host_exec_profile(loaded, "local", "team-fast")
    assert not runner_has_host_exec_profile(loaded, "local", "unknown")
    assert "secret" not in str(loaded.capabilities)
    assert "executable" not in loaded.capabilities["host_exec_profiles"][0]
