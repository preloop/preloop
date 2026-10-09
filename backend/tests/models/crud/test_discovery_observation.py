"""PostgreSQL integration for safe immutable observation persistence."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from preloop.models import models
from preloop.models.crud import crud_account, crud_discovery_observation
from preloop.models.crud.discovery_observation import ObservationConflictError
from preloop.schemas.discovery_evidence import DiscoveryEvidence, ObservedApplication
from sqlalchemy.orm import Session

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_postgres_replay_json_filters_and_tenant_isolation(
    db_session: Session, test_user: models.User
) -> None:
    other = crud_account.create(
        db_session,
        obj_in={"organization_name": "Synthetic other account", "is_active": True},
    )
    evidence = DiscoveryEvidence(
        observation_id=uuid4(),
        started_at=NOW,
        ended_at=NOW,
        scan_scope=["user_config"],
        completeness="complete",
        applications=[
            ObservedApplication(agent_kind="cursor", config_path_hash="a" * 64)
        ],
    )
    source = uuid4()
    arguments: dict[str, Any] = {
        "account_id": test_user.account_id,
        "workstation_fingerprint": "b" * 64,
        "source_ref": source,
        "now": NOW,
    }
    row = crud_discovery_observation.record(
        db_session, **arguments, evidence=evidence.model_dump()
    )
    replay = crud_discovery_observation.record(
        db_session, **arguments, evidence=evidence.model_dump(mode="json")
    )
    assert replay.id == row.id
    with pytest.raises(ObservationConflictError):
        crud_discovery_observation.record(
            db_session,
            **arguments,
            evidence={**evidence.model_dump(), "completeness": "partial"},
        )
    rows, total = crud_discovery_observation.list_for_account(
        db_session, account_id=test_user.account_id, agent_kind="cursor", now=NOW
    )
    assert total == 1 and rows[0].id == row.id
    assert (
        crud_discovery_observation.list_for_account(
            db_session, account_id=test_user.account_id, agent_kind="codex", now=NOW
        )[1]
        == 0
    )
    assert (
        crud_discovery_observation.list_for_account(
            db_session, account_id=other.id, now=NOW
        )[1]
        == 0
    )
    assert (
        crud_discovery_observation.purge_all_expired(
            db_session, now=NOW + timedelta(days=91)
        )
        == 1
    )
