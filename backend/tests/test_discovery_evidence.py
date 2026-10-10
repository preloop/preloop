"""Synthetic compatibility, privacy and immutable observation regressions."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from preloop.api.endpoints.agent_discovery import create_discovery_report
from preloop.models import models
from preloop.models.crud import crud_discovery_observation
from preloop.models.crud.discovery_observation import ObservationConflictError
from preloop.schemas.agent_discovery import DiscoveryReportRequest
from preloop.schemas.discovery_evidence import DiscoveryEvidence
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def envelope(**changes: object) -> dict:
    """A safe synthetic empty scan."""
    return {
        "observation_id": str(uuid4()),
        "started_at": NOW.isoformat(),
        "ended_at": NOW.isoformat(),
        "scan_scope": ["installed_apps"],
        "completeness": "complete",
        **changes,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"hostname": "device.example.com"},
        {"account_id": str(uuid4())},
        {"source_ref": str(uuid4())},
        {"last_used_at": NOW.isoformat()},
        {"errors": ["https://example.com"]},
        {"schema_version": 2},
    ],
)
def test_private_or_unknown_fields_rejected(extra: dict) -> None:
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(envelope(**extra))


@pytest.mark.parametrize(
    "changes",
    [
        {"ended_at": (NOW - timedelta(seconds=1)).isoformat()},
        {"ended_at": (NOW + timedelta(days=2)).isoformat()},
        {"started_at": "2026-01-01T00:00:00"},
        {"errors": ["timeout"]},
    ],
)
def test_invalid_collection_window_or_completeness(changes: dict) -> None:
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(envelope(**changes))


def test_partial_empty_scan_is_valid_and_never_verifies() -> None:
    evidence = DiscoveryEvidence.model_validate(
        envelope(completeness="partial", errors=["permission_denied"])
    )
    assert evidence.applications == []
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(
            envelope(
                applications=[
                    {
                        "agent_kind": "cursor",
                        "config_path_hash": "a" * 64,
                        "controls": [
                            {
                                "axis": "model_route",
                                "supported": "true",
                                "effective": "true",
                                "verification": "passed",
                            }
                        ],
                    }
                ]
            )
        )


def test_legacy_report_and_disabled_extension() -> None:
    payload = DiscoveryReportRequest(workstation_fingerprint="a" * 64)
    user = SimpleNamespace(id=uuid4())
    db = MagicMock()
    with (
        patch("preloop.api.endpoints.agent_discovery._require_report_access"),
        patch(
            "preloop.api.endpoints.agent_discovery.crud_discovered_agent_candidate.record_report",
            return_value=[],
        ),
        patch("preloop.api.endpoints.agent_discovery.get_plugin_manager") as manager,
    ):
        result = create_discovery_report(payload, SimpleNamespace(id=uuid4()), user, db)
        assert result.received == 0
        manager.assert_not_called()
        payload.evidence = DiscoveryEvidence.model_validate(envelope())
        manager.return_value.get_service.return_value = None
        with pytest.raises(HTTPException) as exc:
            create_discovery_report(payload, SimpleNamespace(id=uuid4()), user, db)
        assert exc.value.status_code == 503


@compiles(JSONB, "sqlite")
def sqlite_jsonb(type_: JSONB, compiler: object, **kw: object) -> str:
    """Only the synthetic test DB substitutes JSON storage for PostgreSQL JSONB."""
    return "JSON"


@pytest.fixture
def observation_db() -> Generator[Session, None, None]:
    """Fresh in-memory storage never connects to a configured database."""
    engine = create_engine("sqlite://")
    models.DiscoveryObservation.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def record(
    db: Session,
    account: object,
    source: object,
    evidence: dict,
    received: datetime = NOW,
) -> models.DiscoveryObservation:
    return crud_discovery_observation.record(
        db,
        account_id=account,
        workstation_fingerprint="a" * 64,
        source_ref=source,
        evidence=DiscoveryEvidence.model_validate(evidence).model_dump(mode="json"),
        now=received,
    )


def test_replay_conflict_sources_tenants_and_order(observation_db: Session) -> None:
    db = observation_db
    account, other, source, source2 = uuid4(), uuid4(), uuid4(), uuid4()
    first = envelope()
    row = record(db, account, source, first)
    assert record(db, account, source, first).id == row.id
    with pytest.raises(ObservationConflictError):
        record(db, account, source, {**first, "completeness": "failed"})
    record(db, account, source2, {**first, "completeness": "partial"})
    record(db, other, source, first)
    old = envelope(
        started_at=(NOW - timedelta(days=1)).isoformat(),
        ended_at=(NOW - timedelta(days=1)).isoformat(),
    )
    record(db, account, source, old)
    rows, total = crud_discovery_observation.list_for_account(
        db, account_id=account, now=NOW, limit=1
    )
    assert total == 3 and len(rows) == 1 and rows[0].ended_at.date() == NOW.date()
    rows, total = crud_discovery_observation.list_for_account(
        db, account_id=account, source_ref=source2, now=NOW
    )
    assert total == 1 and rows[0].evidence["completeness"] == "partial"
    assert (
        crud_discovery_observation.list_for_account(db, account_id=uuid4(), now=NOW)[1]
        == 0
    )


def test_retention_cannot_delete_another_tenant(observation_db: Session) -> None:
    db = observation_db
    account, other, source = uuid4(), uuid4(), uuid4()
    old = NOW - timedelta(days=91)
    record(db, account, source, envelope(), old)
    record(db, other, source, envelope(), old)
    assert (
        crud_discovery_observation.list_for_account(db, account_id=account, now=NOW)[1]
        == 0
    )
    assert (
        crud_discovery_observation.purge_expired(db, account_id=account, now=NOW) == 1
    )
    assert len(list(db.scalars(select(models.DiscoveryObservation)))) == 1


def test_app_filter_is_bounded_postgres_json_contains() -> None:
    db = MagicMock()
    db.scalar.return_value = 0
    db.scalars.return_value = []
    account = uuid4()
    crud_discovery_observation.list_for_account(
        db, account_id=account, agent_kind="cursor", now=NOW
    )
    query = db.scalars.call_args.args[0].compile(dialect=dialect())
    assert (
        "account_id =" in str(query) and " @> " in str(query) and "LIMIT" in str(query)
    )
    assert [{"agent_kind": "cursor"}] in query.params.values()


def test_optional_plugin_disabled_adds_no_fleet_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.api.app import create_app

    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "true")
    app = create_app()
    assert "/api/v1/fleet/evidence" not in app.openapi()["paths"]


def test_observation_migration_renders_postgres_and_has_one_head() -> None:
    import importlib.util
    from io import StringIO
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    migration = (
        root
        / "backend/preloop/models/alembic/versions/20261009_discovery_observation.py"
    )
    spec = importlib.util.spec_from_file_location("observation_migration", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        module.upgrade()
        module.downgrade()
    sql = buffer.getvalue()
    assert "CREATE TABLE discovery_observation" in sql
    assert "uq_discovery_observation_source" in sql and "JSONB" in sql
    assert "DROP TABLE discovery_observation" in sql
    config = Config()
    config.set_main_option(
        "script_location", str(root / "backend/preloop/models/alembic")
    )
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20261010_anthropic_import"
    ]


def test_changed_replay_plugin_error_is_a_409() -> None:
    payload = DiscoveryReportRequest(
        workstation_fingerprint="a" * 64,
        evidence=DiscoveryEvidence.model_validate(envelope()),
    )
    with (
        patch("preloop.api.endpoints.agent_discovery._require_report_access"),
        patch("preloop.api.endpoints.agent_discovery.get_plugin_manager") as manager,
        patch(
            "preloop.api.endpoints.agent_discovery.crud_discovered_agent_candidate.record_report"
        ) as candidates,
    ):
        manager.return_value.get_service.return_value.record.side_effect = (
            ObservationConflictError("changed")
        )
        with pytest.raises(HTTPException) as exc:
            create_discovery_report(
                payload=payload,
                account=SimpleNamespace(id=uuid4()),
                current_user=SimpleNamespace(id=uuid4()),
                db=MagicMock(),
            )
        assert exc.value.status_code == 409
        candidates.assert_not_called()


def test_python_and_json_schema_dumps_are_the_same_replay(
    observation_db: Session,
) -> None:
    account, source = uuid4(), uuid4()
    evidence = DiscoveryEvidence.model_validate(envelope())
    row = crud_discovery_observation.record(
        observation_db,
        account_id=account,
        workstation_fingerprint="a" * 64,
        source_ref=source,
        evidence=evidence.model_dump(),
        now=NOW,
    )
    replay = crud_discovery_observation.record(
        observation_db,
        account_id=account,
        workstation_fingerprint="a" * 64,
        source_ref=source,
        evidence=evidence.model_dump(mode="json"),
        now=NOW,
    )
    assert replay.id == row.id


def test_background_retention_purges_inactive_sources(observation_db: Session) -> None:
    db = observation_db
    source = uuid4()
    for _ in range(2):
        record(db, uuid4(), source, envelope(), NOW - timedelta(days=91))
    record(db, uuid4(), source, envelope(), NOW)
    assert crud_discovery_observation.purge_all_expired(db, now=NOW) == 2
    assert len(list(db.scalars(select(models.DiscoveryObservation)))) == 1
