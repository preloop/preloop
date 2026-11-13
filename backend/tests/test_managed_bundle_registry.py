"""Real PostgreSQL registry lifecycle, eligibility and account isolation tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_bundle_revision,
    crud_control_expectation,
    crud_discovery_observation,
)
from preloop.schemas.managed_bundle import BundleManifest, ControlExpectationInput


def manifest(revision: str = "a" * 64, platform: str = "linux") -> BundleManifest:
    """Build public, secret-free manifest metadata."""
    return BundleManifest(
        schema="preloop.managed-hook-bundle.v1",
        app_id="claude-code",
        platform=platform,
        policy_revision_hash=revision,
        artifact="managed-settings.json",
        keys=["hooks.PreToolUse"],
        target_locations=["/etc/claude-code/managed-settings.json"],
        managed_source="system managed-settings.json",
        documented_semantics="Managed hook overlay",
        verification="unverified",
        prerequisites=[],
        omitted_capabilities=[],
        references=[],
    )


def observation(
    db: Session, account_id: UUID, platform: str = "linux"
) -> models.DiscoveryObservation:
    """Record an authenticated workstation/application observation."""
    now = datetime.now(UTC)
    return crud_discovery_observation.record(
        db,
        account_id=account_id,
        workstation_fingerprint="b" * 64,
        source_ref=uuid4(),
        evidence={
            "schema_version": 1,
            "observation_id": str(uuid4()),
            "os_family": platform,
            "started_at": now.isoformat(),
            "ended_at": now.isoformat(),
            "scan_scope": ["installed_apps"],
            "completeness": "complete",
            "applications": [
                {"agent_kind": "claude_code", "config_path_hash": "c" * 64}
            ],
        },
    )


def expectation(source_ref: UUID, revision: str = "a" * 64) -> ControlExpectationInput:
    """Map an evidence context into the drift tuple."""
    return ControlExpectationInput(
        source_ref=source_ref,
        workstation_fingerprint="b" * 64,
        agent_kind="claude_code",
        config_path_hash="c" * 64,
        axis="native_tool_hook",
        policy_revision_hash=revision,
        required_scope="managed_config",
    )


def test_registration_supersedes_only_matching_bundle_rows(
    db_session: Session, test_user: models.User
) -> None:
    """Registration supersedes only matching bundle rows."""
    account = test_user.account_id
    first = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    assert (
        crud_bundle_revision.register(
            db_session, account_id=account, manifest=manifest()
        ).id
        == first.id
    )
    assert crud_control_expectation.generated_count(db_session, account_id=account) == 0
    report = observation(db_session, account)
    data = expectation(report.source_ref)
    generated = crud_control_expectation.upsert(
        db_session,
        account_id=account,
        expectation=data,
        origin="bundle",
        bundle_revision_id=first.id,
    )
    manual = crud_control_expectation.upsert(
        db_session, account_id=account, expectation=data, origin="manual"
    )
    assert (
        crud_control_expectation.upsert(
            db_session,
            account_id=account,
            expectation=data,
            origin="bundle",
            bundle_revision_id=first.id,
        ).id
        == generated.id
    )
    other = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest(platform="darwin")
    )
    assert other.retired_at is None
    darwin_report = observation(db_session, account, platform="darwin")
    darwin_row = crud_control_expectation.upsert(
        db_session,
        account_id=account,
        expectation=expectation(darwin_report.source_ref),
        origin="bundle",
        bundle_revision_id=other.id,
    )
    assert crud_control_expectation.generated_count(db_session, account_id=account) == 2
    second = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest("d" * 64)
    )
    assert second.id != first.id
    assert first.retired_at is not None
    assert generated.retired_at is not None
    assert manual.retired_at is None
    assert other.retired_at is None
    assert darwin_row.retired_at is None
    assert crud_control_expectation.generated_count(db_session, account_id=account) == 1
    with pytest.raises(ValueError, match="retired"):
        crud_bundle_revision.register(
            db_session, account_id=account, manifest=manifest()
        )
    with pytest.raises(ValueError):
        crud_control_expectation.upsert(
            db_session,
            account_id=account,
            expectation=data,
            origin="bundle",
            bundle_revision_id=first.id,
        )


def test_bundle_requires_matching_account_application_platform_evidence(
    db_session: Session, test_user: models.User
) -> None:
    """Bundle requires matching account application platform evidence."""
    account = test_user.account_id
    bundle = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other account", "is_active": True}
    )
    foreign_report = observation(db_session, foreign.id)
    for source in [uuid4(), foreign_report.source_ref]:
        with pytest.raises(ValueError, match="evidence"):
            crud_control_expectation.upsert(
                db_session,
                account_id=account,
                expectation=expectation(source),
                origin="bundle",
                bundle_revision_id=bundle.id,
            )
    report = observation(db_session, account, platform="darwin")
    with pytest.raises(ValueError, match="evidence"):
        crud_control_expectation.upsert(
            db_session,
            account_id=account,
            expectation=expectation(report.source_ref),
            origin="bundle",
            bundle_revision_id=bundle.id,
        )
    with pytest.raises(ValueError, match="registered"):
        crud_control_expectation.upsert(
            db_session,
            account_id=foreign.id,
            expectation=expectation(foreign_report.source_ref),
            origin="bundle",
            bundle_revision_id=bundle.id,
        )
    assert crud_bundle_revision.list_active(db_session, account_id=foreign.id) == []
    assert crud_control_expectation.list_active(db_session, account_id=foreign.id) == []
    assert (
        crud_control_expectation.generated_count(db_session, account_id=foreign.id) == 0
    )


def test_retirement_is_scoped_and_preserves_manual(
    db_session: Session, test_user: models.User
) -> None:
    """Retirement is scoped and preserves manual."""
    account = test_user.account_id
    report = observation(db_session, account)
    bundle = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    kwargs = dict(account_id=account, expectation=expectation(report.source_ref))
    row = crud_control_expectation.upsert(
        db_session, **kwargs, origin="bundle", bundle_revision_id=bundle.id
    )
    manual = crud_control_expectation.upsert(db_session, **kwargs, origin="manual")
    scope = dict(
        source_ref=report.source_ref,
        workstation_fingerprint="b" * 64,
        agent_kind="claude_code",
        config_path_hash="c" * 64,
    )
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Separate account", "is_active": True}
    )
    assert (
        crud_control_expectation.retire_bundle_scope(
            db_session, account_id=foreign.id, **scope
        )
        == 0
    )
    assert row.retired_at is None
    assert (
        crud_control_expectation.retire_bundle_scope(
            db_session, account_id=account, **scope
        )
        == 1
    )
    assert manual.retired_at is None
    assert (
        crud_control_expectation.upsert(
            db_session, **kwargs, origin="bundle", bundle_revision_id=bundle.id
        ).retired_at
        is None
    )


def test_registry_api_account_isolation_and_validation(
    db_session: Session, test_user: models.User
) -> None:
    """Registry api account isolation and validation."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.jwt import get_current_active_user
    from preloop.api.endpoints.managed_bundle import router
    from preloop.models.db.session import get_db_session

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    client = TestClient(app)
    url = "/api/v1/agents/managed-config/bundles"
    data = manifest().model_dump(mode="json", by_alias=True)
    response = client.post(url, json=data)
    assert response.status_code == 200, response.text
    assert response.json()["generated_count"] == 0
    assert client.get(url).json()["generated_count"] == 0
    assert len(client.get(url).json()["bundles"]) == 1
    for extra in [
        {"account_id": str(uuid4())},
        {"credentials": "forbidden"},
        {"policy_revision_hash": "invalid"},
    ]:
        assert client.post(url, json={**data, **extra}).status_code == 422
    changed = {**data, "documented_semantics": "changed metadata"}
    assert client.post(url, json=changed).status_code == 409
    # Changing the authenticated membership cannot read the original account.
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Separate account", "is_active": True}
    )
    from types import SimpleNamespace

    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        account_id=foreign.id
    )
    assert client.get(url).json() == {"bundles": [], "generated_count": 0}
    assert (
        client.post(url, json={**data, "policy_revision_hash": "d" * 64}).status_code
        == 200
    )
    original = crud_bundle_revision.list_active(
        db_session, account_id=test_user.account_id
    )
    assert len(original) == 1 and original[0].policy_revision_hash == "a" * 64


def test_registry_api_authentication_and_permissions(
    db_session: Session, test_user: models.User
) -> None:
    """Registry api authentication and permissions."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.jwt import get_current_active_user
    from preloop.api.endpoints.managed_bundle import router
    from preloop.models.db.session import get_db_session
    from preloop.plugins.account_hooks import Decision, register_authorizer

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    url = "/api/v1/agents/managed-config/bundles"
    assert client.get(url).status_code == 401
    assert (
        client.post(url, json=manifest().model_dump(by_alias=True)).status_code == 401
    )
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    actions: list[str] = []

    def deny(ctx: object, action: str, resource: object) -> Decision:
        """Deny both registry permissions through the production authorizer."""
        actions.append(action)
        return Decision("deny")

    register_authorizer(deny)
    try:
        assert client.get(url).status_code == 403
        assert (
            client.post(url, json=manifest().model_dump(by_alias=True)).status_code
            == 403
        )
        assert actions == ["view_agents", "manage_agents"]
        assert (
            crud_bundle_revision.list_active(
                db_session, account_id=test_user.account_id
            )
            == []
        )
    finally:
        register_authorizer(None)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_ref": uuid4()},
        {"workstation_fingerprint": "e" * 64},
        {"config_path_hash": "e" * 64},
        {"agent_kind": "other_app"},
        {"policy_revision_hash": "e" * 64},
        {"axis": "model_route"},
        {"required_scope": "user_config"},
    ],
)
def test_bundle_rejects_mismatched_context(
    db_session: Session, test_user: models.User, changes: dict[str, object]
) -> None:
    """Each identity dimension must match real evidence and the registered policy."""
    account = test_user.account_id
    report = observation(db_session, account)
    bundle = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    data = ControlExpectationInput.model_validate(
        {**expectation(report.source_ref).model_dump(), **changes}
    )
    with pytest.raises(ValueError):
        crud_control_expectation.upsert(
            db_session,
            account_id=account,
            expectation=data,
            origin="bundle",
            bundle_revision_id=bundle.id,
        )
    assert crud_control_expectation.generated_count(db_session, account_id=account) == 0


def test_generated_count_is_uncapped_and_settings_do_not_replace_rows(
    db_session: Session, test_user: models.User
) -> None:
    """A fleet larger than settings' display limits still returns its real count."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.jwt import get_current_active_user
    from preloop.api.endpoints.managed_bundle import router
    from preloop.models.crud import crud_fleet_alert_settings
    from preloop.models.db.session import get_db_session

    account = test_user.account_id
    bundle = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    now = datetime.now(UTC)
    for start in range(0, 501, 100):
        applications = [
            {"agent_kind": "claude_code", "config_path_hash": f"{i:064x}"}
            for i in range(start, min(start + 100, 501))
        ]
        source = uuid4()
        crud_discovery_observation.record(
            db_session,
            account_id=account,
            workstation_fingerprint="b" * 64,
            source_ref=source,
            evidence={
                "schema_version": 1,
                "observation_id": str(uuid4()),
                "os_family": "linux",
                "started_at": now.isoformat(),
                "ended_at": now.isoformat(),
                "scan_scope": ["installed_apps"],
                "completeness": "complete",
                "applications": applications,
            },
        )
        for app_data in applications:
            data = ControlExpectationInput.model_validate(
                {
                    **expectation(source).model_dump(),
                    "config_path_hash": app_data["config_path_hash"],
                }
            )
            crud_control_expectation.upsert(
                db_session,
                account_id=account,
                expectation=data,
                origin="bundle",
                bundle_revision_id=bundle.id,
            )
    crud_fleet_alert_settings.replace(
        db_session,
        account_id=account,
        settings={"rules": {"control_drift": {"expectations": []}}},
        user_id=test_user.id,
    )
    assert (
        len(
            crud_control_expectation.list_active(
                db_session, account_id=account, origin="bundle"
            )
        )
        == 501
    )
    assert (
        crud_control_expectation.generated_count(db_session, account_id=account) == 501
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    response = TestClient(app).get("/agents/managed-config/bundles")
    assert response.status_code == 200
    assert response.json()["generated_count"] == 501
    assert response.json()["bundles"][0]["generated_count"] == 501


def test_migrated_bundle_tables_match_models(db_session: Session) -> None:
    """Compare actual PostgreSQL tables with the two new ORM declarations."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from preloop.models.models.base import Base

    tables = {"registered_bundle_revision", "control_expectation"}

    def include_object(
        obj: object, name: str | None, kind: str, reflected: bool, compare_to: object
    ) -> bool:
        """Limit model/schema comparison to this migration's tables."""
        if kind == "table":
            return name in tables
        return getattr(getattr(obj, "table", None), "name", None) in tables

    context = MigrationContext.configure(
        db_session.connection(), opts={"include_object": include_object}
    )
    assert compare_metadata(context, Base.metadata) == []


def test_concurrent_stale_generator_cannot_write_superseded_revision(
    db_engine: Engine,
) -> None:
    """A generator waiting on registration must recheck current eligibility."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    with Session(db_engine) as setup:
        account = crud_account.create(
            setup,
            obj_in={"organization_name": "Concurrent registry test", "is_active": True},
        )
        account_id = account.id
        report = observation(setup, account_id)
        data = expectation(report.source_ref)
        bundle = crud_bundle_revision.register(
            setup, account_id=account_id, manifest=manifest()
        )
        bundle_id = bundle.id
        setup.commit()
    started = Event()

    def stale_generator() -> None:
        """Attempt generation while another transaction supersedes its bundle."""
        with Session(db_engine) as worker:
            started.set()
            crud_control_expectation.upsert(
                worker,
                account_id=account_id,
                expectation=data,
                origin="bundle",
                bundle_revision_id=bundle_id,
            )
            worker.commit()

    try:
        with Session(db_engine) as registration:
            crud_bundle_revision.register(
                registration, account_id=account_id, manifest=manifest("d" * 64)
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(stale_generator)
                try:
                    assert started.wait(timeout=5)
                    with pytest.raises(TimeoutError):
                        pending.result(timeout=0.1)
                finally:
                    registration.commit()
                with pytest.raises(ValueError, match="registered"):
                    pending.result(timeout=5)
        with Session(db_engine) as check:
            assert (
                crud_control_expectation.generated_count(check, account_id=account_id)
                == 0
            )
    finally:
        with Session(db_engine) as cleanup:
            crud_account.delete_where_in(cleanup, column="id", values=[account_id])
            cleanup.commit()


@pytest.mark.parametrize("platform", ["darwin", "linux", "windows"])
def test_cli_manifest_contract(platform: str) -> None:
    """Accept actual CLI golden manifests and verify their artifact revisions."""
    import hashlib
    from pathlib import Path

    directory = (
        Path(__file__).resolve().parents[2]
        / "cli/internal/cmd/testdata/managed-hook-bundle"
        / platform
    )
    metadata = BundleManifest.model_validate_json(
        (directory / "manifest.json").read_bytes()
    )
    assert metadata.platform == platform
    assert (
        metadata.policy_revision_hash
        == hashlib.sha256((directory / metadata.artifact).read_bytes()).hexdigest()
    )


def test_same_tuple_can_follow_a_matching_platform_registration(
    db_session: Session, test_user: models.User
) -> None:
    """Rebinding a tuple must not leave it owned by the previous platform."""
    account = test_user.account_id
    report = observation(db_session, account)
    linux = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest()
    )
    data = expectation(report.source_ref)
    original = crud_control_expectation.upsert(
        db_session,
        account_id=account,
        expectation=data,
        origin="bundle",
        bundle_revision_id=linux.id,
    )
    darwin_evidence = {
        **report.evidence,
        "observation_id": str(uuid4()),
        "os_family": "darwin",
    }
    crud_discovery_observation.record(
        db_session,
        account_id=account,
        workstation_fingerprint=report.workstation_fingerprint,
        source_ref=report.source_ref,
        evidence=darwin_evidence,
    )
    darwin = crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest(platform="darwin")
    )
    rebound = crud_control_expectation.upsert(
        db_session,
        account_id=account,
        expectation=data,
        origin="bundle",
        bundle_revision_id=darwin.id,
    )
    assert rebound.id == original.id
    assert rebound.bundle_revision_id == darwin.id
    crud_bundle_revision.register(
        db_session, account_id=account, manifest=manifest("d" * 64)
    )
    assert rebound.retired_at is None
    assert (
        crud_control_expectation.generated_count(
            db_session, account_id=account, bundle_revision_id=darwin.id
        )
        == 1
    )
