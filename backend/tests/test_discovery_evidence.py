"""Synthetic compatibility, privacy and immutable observation regressions."""

import json
from pathlib import Path

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
        {"schema_version": 5},
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
        manager.return_value.get_service.return_value = None
        result = create_discovery_report(payload, SimpleNamespace(id=uuid4()), user, db)
        assert result.received == 0
        manager.return_value.get_service.assert_called_once_with(
            "discovery_report_authorizer"
        )
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
    models.DiscoveredExtension.__table__.create(engine)
    # This fixture deliberately contains only the observation table. Receipt
    # locks and history use PostgreSQL and are covered by test_expected_device.
    with (
        Session(engine) as db,
        patch("preloop.models.crud.expected_device.crud_expected_device.lock_account"),
        patch(
            "preloop.models.crud.expected_device.crud_expected_device.touch_last_reported_at"
        ),
    ):
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
    # One head, without pinning which: later migrations build on this one.
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("20261009_discovery_observation") is not None
    assert (
        script.get_revision("20261010_gateway_idp").down_revision
        == "20261010_ticket_readiness"
    )
    assert (
        script.get_revision("20261010_anthropic_import").down_revision
        == "20261010_gateway_idp"
    )
    assert (
        script.get_revision("20261010_otlp_telemetry_ingest").down_revision
        == "20261010_anthropic_import"
    )
    assert module.revision in {
        revision.revision for revision in script.walk_revisions()
    }


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


def test_profile_evidence_v2_and_legacy_compatibility() -> None:
    """Profiles extend immutable evidence while v1 reports keep their shape."""
    profiles = [
        {"profile_ref": "a" * 64, "completeness": "complete"},
        {
            "profile_ref": "b" * 64,
            "completeness": "failed",
            "error_code": "profile_unreadable",
        },
    ]
    payload = envelope(
        schema_version=2,
        profiles=profiles,
        completeness="partial",
        errors=["permission_denied"],
        applications=[
            {
                "agent_kind": "cursor",
                "config_path_hash": "c" * 64,
                "profile_ref": "a" * 64,
            }
        ],
    )
    evidence = DiscoveryEvidence.model_validate(payload)
    assert len(evidence.profiles) == 2
    assert evidence.applications[0].profile_ref == "a" * 64
    report = DiscoveryReportRequest(workstation_fingerprint="d" * 64, evidence=evidence)
    assert report.evidence.schema_version == 2
    for changes in (
        {"schema_version": 1},
        {"profiles": profiles[:1] * 2},
        {"profiles": profiles[1:]},
        {"completeness": "complete", "errors": []},
        {"profiles": [{**profiles[0], "home": "/home/jane"}]},
        {"profiles": [{**profiles[0], "profile_ref": "Jane Doe"}]},
        {"profiles": [{**profiles[0], "error_code": "secret-token"}]},
    ):
        with pytest.raises(ValidationError):
            DiscoveryEvidence.model_validate({**payload, **changes})
    assert DiscoveryEvidence.model_validate(envelope()).schema_version == 1


def test_v1_canonical_evidence_does_not_gain_profile_defaults() -> None:
    """An upgrade must not change content hashes for replayed v1 evidence."""
    evidence = DiscoveryEvidence.model_validate(
        envelope(applications=[{"agent_kind": "cursor", "config_path_hash": "a" * 64}])
    )
    data = evidence.model_dump(mode="json")
    assert "profiles" not in data
    assert "profile_ref" not in data["applications"][0]
    assert DiscoveryEvidence.model_validate(data).model_dump(mode="json") == data


def test_go_profile_report_wire_contract() -> None:
    """The exact Go serializer fixture must pass the server's privacy schema."""
    path = (
        Path(__file__).resolve().parents[2]
        / "cli/internal/cmd/testdata/profile-inventory/report-v2.json"
    )
    report = DiscoveryReportRequest.model_validate(json.loads(path.read_text()))
    assert report.evidence is not None
    assert report.evidence.schema_version == 2
    assert report.evidence.profiles[2].completeness == "failed"
    assert len(report.evidence.applications) == len(report.candidates) == 2
    assert len({candidate.config_path_hash for candidate in report.candidates}) == 2
    assert all(not candidate.enrolled for candidate in report.candidates)


def test_managed_configuration_facts_are_scoped_and_unverified() -> None:
    """Machine configuration does not masquerade as a collected user profile."""
    app = {
        "agent_kind": "claude_code",
        "config_path_hash": "a" * 64,
        "config_scope": "managed_config",
        "controls": [
            {
                "axis": "native_tool_hook",
                "configured": "false",
                "configuration_evidence": "managed_config",
                "method": "managed_config",
            }
        ],
    }
    evidence = DiscoveryEvidence.model_validate(
        envelope(
            schema_version=2,
            scan_scope=["managed_config"],
            applications=[app],
        )
    )
    control = evidence.applications[0].controls[0]
    assert control.configured == "false"
    assert control.applied == "unknown"
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(
            envelope(
                schema_version=2,
                applications=[{**app, "config_scope": "user_config"}],
            )
        )
    for field, value in [
        ("command", "private"),
        ("configuration_evidence", "private"),
        ("verification", "passed"),
    ]:
        with pytest.raises(ValidationError):
            DiscoveryEvidence.model_validate(
                envelope(
                    applications=[
                        {
                            **app,
                            "controls": [{**app["controls"][0], field: value}],
                        }
                    ]
                )
            )


def test_configuration_additions_preserve_legacy_canonical_control_bytes() -> None:
    """Adding optional facts cannot change immutable historical replay hashes."""
    evidence = DiscoveryEvidence.model_validate(
        envelope(
            applications=[
                {
                    "agent_kind": "claude_code",
                    "config_path_hash": "a" * 64,
                    "controls": [
                        {"axis": "native_tool_hook", "method": "local_config"}
                    ],
                }
            ]
        )
    )
    app = evidence.model_dump(mode="json")["applications"][0]
    assert "config_scope" not in app
    assert "scope_completeness" not in evidence.model_dump(mode="json")
    assert app["controls"] == [
        {
            "axis": "native_tool_hook",
            "supported": "unknown",
            "requested": "unknown",
            "applied": "unknown",
            "method": "local_config",
        }
    ]


def test_v3_managed_configuration_is_not_an_extension_candidate() -> None:
    """A machine settings observation cannot invent or reconcile an install."""
    app = {
        "agent_kind": "claude_code",
        "config_path_hash": "a" * 64,
        "config_scope": "managed_config",
        "controls": [
            {
                "axis": "native_tool_hook",
                "configured": "false",
                "configuration_evidence": "managed_config",
            }
        ],
    }
    payload = DiscoveryReportRequest(
        workstation_fingerprint="b" * 64,
        evidence=envelope(
            schema_version=3, scan_scope=["managed_config"], applications=[app]
        ),
        candidates=[],
    )
    assert payload.evidence.applications[0].extensions is None
    with pytest.raises(ValidationError):
        DiscoveryReportRequest(
            workstation_fingerprint="b" * 64,
            evidence=envelope(
                schema_version=3,
                scan_scope=["managed_config"],
                applications=[{**app, "extensions": [], "completeness": "complete"}],
            ),
            candidates=[],
        )


@pytest.mark.parametrize(
    "scopes",
    [
        {"managed_config": "complete"},
        {"user_config": "private-value"},
        {"private-path": "complete"},
        {"user_config": "partial"},
    ],
)
def test_scope_completeness_is_bounded_and_consistent(scopes: dict[str, str]) -> None:
    """Undeclared scope, arbitrary values and false completeness are rejected."""
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(
            envelope(scan_scope=["user_config"], scope_completeness=scopes)
        )
