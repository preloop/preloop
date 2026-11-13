"""One shared read-only review flow, several projects, one project per identity.

Every test uses synthetic resources and a local database. The provider and
the lease mint are mocked; nothing here reaches a real provider.
"""

import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import ci_identities, flows
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.ci_administration import (
    SHARED_FLOW_HAS_DEDICATED_IDENTITIES,
    SHARED_FLOW_NOT_READ_ONLY,
    SHARED_FLOW_PROJECT_REFUSED,
)
from preloop.models.db.session import get_db_session
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_execution import CiReviewBinding
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.services import ci_execution as ci_service
from preloop.services import publication_credentials
from preloop.services.read_only_review import prepare_read_only_review
from preloop.services.trusted_publisher import PublicationLease
from tests.api.test_ci_principal import ci_resources as create_ci_resources

ADMIN = "/api/v1/ci-identities"
SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture(autouse=True)
def default_authority(monkeypatch: pytest.MonkeyPatch) -> Any:
    """OSS owner authority; the App tracker check needs no signing key here."""
    register_ci_administrator(None)
    monkeypatch.setattr(
        publication_credentials, "validate_publication_tracker", lambda tracker: None
    )
    yield
    register_ci_administrator(None)


class Shared(SimpleNamespace):
    """Synthetic account with one read-only review flow and three projects."""

    owner: models.User
    flow: models.Flow
    project_a: models.Project
    project_b: models.Project
    project_c: models.Project
    tracker_id: UUID


def _project(db: Session, organization_id: Any, slug: str) -> models.Project:
    return crud.crud_project.create(
        db,
        obj_in={
            "name": slug,
            "identifier": str(uuid4()),
            "slug": slug,
            "organization_id": organization_id,
            "is_active": True,
        },
    )


def _account(db: Session) -> Shared:
    """An account whose flow is a dedicated read-only review of project A."""
    owner, project_a, flow, _ = create_ci_resources.__wrapped__(db)
    tracker_id = UUID(flow.git_clone_config["repositories"][0]["tracker_id"])
    flow.agent_type = "codex"
    flow.git_clone_config = {**flow.git_clone_config, "publication_mode": "read_only"}
    db.commit()
    return Shared(
        owner=owner,
        flow=flow,
        project_a=project_a,
        project_b=_project(db, project_a.organization_id, "example/second"),
        project_c=_project(db, project_a.organization_id, "example/third"),
        tracker_id=tracker_id,
    )


@pytest.fixture
def shared(db_session: Session) -> Shared:
    """A flow whose allowlist admits projects A and B, but not C."""
    account = _account(db_session)
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=account.owner,
        flow_id=account.flow.id,
        payload=_allow(account.project_a, account.project_b),
    )
    db_session.refresh(account.flow)
    return account


def _allow(*projects: models.Project) -> Any:
    from preloop.schemas.ci_administration import CiSharedFlowUpdate

    return CiSharedFlowUpdate(project_ids=tuple(project.id for project in projects))


def _grant(account: Shared, project: models.Project) -> CiGrant:
    return CiGrant(
        version=1,
        project_id=project.id,
        flow_id=account.flow.id,
        actions=tuple(CiAction),
    )


def _identity(
    db: Session, account: Shared, project: models.Project, **kwargs: Any
) -> tuple[models.CiPrincipal, models.ApiKey, str]:
    return crud.crud_ci_principal.provision(
        db,
        actor=account.owner,
        name=f"CI {project.slug}",
        grant=_grant(account, project),
        **kwargs,
    )


def _binding(context: Any, *, head_sha: str, pr_number: int = 7) -> CiReviewBinding:
    values = asdict(context)
    values.pop("actions")
    values.pop("credential_version")
    return CiReviewBinding(
        **values,
        version=1,
        pr_number=pr_number,
        head_sha=head_sha,
        provider_pr_id=f"synthetic-pr-{pr_number}",
        base_branch="main",
    )


def _run(db: Session, token: str, *, head_sha: str) -> models.FlowExecution:
    context = crud.crud_ci_principal.authenticate(db, token=token)
    assert context is not None
    return crud.crud_ci_execution.create(
        db, context=context, binding=_binding(context, head_sha=head_sha), event={}
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def actor() -> dict[str, Any]:
    """The human the administration client is signed in as."""
    return {}


@pytest.fixture
def api(
    db_session: Session, actor: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Administration and execution routes behind the real CI middleware."""

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(ci_identities.router, prefix="/api/v1")
    app.include_router(flows.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_active_user] = lambda: actor["user"]
    monkeypatch.setattr(ci_auth, "get_auth_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


# --- 1. explicit allowlist, one project per identity -----------------------


def test_admin_shares_a_flow_and_each_identity_keeps_one_project(
    db_session: Session, api: TestClient, actor: dict[str, Any]
) -> None:
    account = _account(db_session)
    actor["user"] = account.owner
    path = f"{ADMIN}/shared-flows/{account.flow.id}"
    before = api.get(path)
    assert before.status_code == 200, before.text
    assert before.json()["binding_mode"] == "dedicated"
    assert before.json()["read_only"] is True

    response = api.put(
        path,
        json={"project_ids": [str(account.project_a.id), str(account.project_b.id)]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["binding_mode"] == "shared"
    assert [row["project_id"] for row in body["projects"]] == [
        str(account.project_a.id),
        str(account.project_b.id),
    ]
    assert [row["identities"] for row in body["projects"]] == [0, 0]
    db_session.refresh(account.flow)
    assert account.flow.trigger_project_ids == [
        str(account.project_a.id),
        str(account.project_b.id),
    ]
    assert account.flow.git_clone_config["ci_project_selection"] is True
    # Preloop derives the repository and tracker from the project.
    assert account.flow.git_clone_config["repositories"] == [
        {"project_id": str(project.id), "tracker_id": str(account.tracker_id)}
        for project in (account.project_a, account.project_b)
    ]

    tokens = {}
    for project in (account.project_a, account.project_b):
        created = api.post(
            ADMIN,
            json={
                "name": f"CI {project.slug}",
                "grant": _grant(account, project).model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["identity"]["binding_mode"] == "shared"
        assert created.json()["identity"]["grant"]["project_id"] == str(project.id)
        tokens[project.id] = created.json()["token"]
    for project in (account.project_a, account.project_b):
        context = crud.crud_ci_principal.authenticate(
            db_session, token=tokens[project.id]
        )
        assert context is not None
        assert context.project_id == project.id
        assert context.flow_id == account.flow.id
        assert context.repository_slug == project.slug
    counts = api.get(path).json()["projects"]
    assert [row["identities"] for row in counts] == [1, 1]
    capabilities = api.get(f"{ADMIN}/capabilities").json()
    assert capabilities["binding_modes"] == ["dedicated", "shared"]
    assert "shared read-only review flow" in capabilities["binding"]


def test_project_outside_the_allowlist_cannot_get_an_identity(
    db_session: Session, shared: Shared, api: TestClient, actor: dict[str, Any]
) -> None:
    actor["user"] = shared.owner
    grant = _grant(shared, shared.project_c).model_dump(mode="json")
    assert api.post(f"{ADMIN}/preview", json={"grant": grant}).status_code == 400
    created = api.post(ADMIN, json={"name": "Not admitted", "grant": grant})
    assert created.status_code == 400, created.text
    with pytest.raises(ValueError):
        _identity(db_session, shared, shared.project_c)


@pytest.mark.parametrize(
    "payload",
    [
        {"project_ids": []},
        {"project_ids": "all"},
        {},
        {"project_ids": ["11111111-1111-4111-8111-111111111111"] * 2},
        {
            "project_ids": ["11111111-1111-4111-8111-111111111111"],
            "repositories": [{"repository_url": "https://example.invalid/x.git"}],
        },
        {
            "project_ids": ["11111111-1111-4111-8111-111111111111"],
            "tracker_id": "22222222-2222-4222-8222-222222222222",
        },
    ],
)
def test_allowlist_request_shape_fails_closed(
    db_session: Session,
    shared: Shared,
    api: TestClient,
    actor: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """No wildcard, no duplicate, no caller-chosen repository or tracker."""
    actor["user"] = shared.owner
    before = copy.deepcopy(shared.flow.git_clone_config)
    response = api.put(f"{ADMIN}/shared-flows/{shared.flow.id}", json=payload)
    assert response.status_code == 422, response.text
    db_session.refresh(shared.flow)
    assert shared.flow.git_clone_config == before


def test_only_a_read_only_review_flow_can_be_shared(
    db_session: Session, api: TestClient, actor: dict[str, Any]
) -> None:
    account = _account(db_session)
    actor["user"] = account.owner
    account.flow.git_clone_config = {
        **account.flow.git_clone_config,
        "publication_mode": "legacy",
    }
    db_session.commit()
    before = copy.deepcopy(account.flow.git_clone_config)
    response = api.put(
        f"{ADMIN}/shared-flows/{account.flow.id}",
        json={"project_ids": [str(account.project_a.id), str(account.project_b.id)]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == SHARED_FLOW_NOT_READ_ONLY
    db_session.refresh(account.flow)
    assert account.flow.git_clone_config == before
    assert account.flow.trigger_project_ids == [str(account.project_a.id)]


def test_existing_dedicated_identity_is_unchanged_and_cannot_be_flipped(
    db_session: Session, api: TestClient, actor: dict[str, Any]
) -> None:
    """A one-project identity keeps working; sharing its flow is refused."""
    account = _account(db_session)
    actor["user"] = account.owner
    principal, _, token = _identity(db_session, account, account.project_a)
    assert "ci_project_selection" not in principal.repository_binding
    listed = api.get(ADMIN).json()
    assert [row["binding_mode"] for row in listed] == ["dedicated"]
    before = copy.deepcopy(account.flow.git_clone_config)
    response = api.put(
        f"{ADMIN}/shared-flows/{account.flow.id}",
        json={"project_ids": [str(account.project_a.id), str(account.project_b.id)]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == SHARED_FLOW_HAS_DEDICATED_IDENTITIES
    db_session.refresh(account.flow)
    assert account.flow.git_clone_config == before
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None and context.project_id == account.project_a.id
    # Once the identity is retired the administrator can share the flow.
    crud.crud_ci_principal.retire(
        db_session, actor=account.owner, principal_id=principal.id
    )
    response = api.put(
        f"{ADMIN}/shared-flows/{account.flow.id}",
        json={"project_ids": [str(account.project_a.id), str(account.project_b.id)]},
    )
    assert response.status_code == 200, response.text
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None


def test_hand_edited_mode_flip_denies_a_dedicated_identity(
    db_session: Session,
) -> None:
    """The binding mode is part of the snapshot, so a silent flip fails closed."""
    account = _account(db_session)
    _, _, token = _identity(db_session, account, account.project_a)
    account.flow.trigger_project_ids = [
        str(account.project_a.id),
        str(account.project_b.id),
    ]
    account.flow.git_clone_config = {
        **account.flow.git_clone_config,
        "ci_project_selection": True,
        "repositories": [
            *account.flow.git_clone_config["repositories"],
            {
                "project_id": str(account.project_b.id),
                "tracker_id": str(account.tracker_id),
            },
        ],
    }
    db_session.commit()
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None


def test_allowlist_administration_is_human_owner_only(
    db_session: Session, shared: Shared, api: TestClient, actor: dict[str, Any]
) -> None:
    """A CI credential and a non-owner cannot read or change the allowlist."""
    actor["user"] = shared.owner
    _, _, token = _identity(db_session, shared, shared.project_a)
    path = f"{ADMIN}/shared-flows/{shared.flow.id}"
    body = {"project_ids": [str(shared.project_a.id), str(shared.project_c.id)]}
    before = copy.deepcopy(shared.flow.git_clone_config)
    assert api.get(path, headers=_bearer(token)).status_code == 403
    assert api.put(path, json=body, headers=_bearer(token)).status_code == 403
    register_ci_administrator(lambda actor, operation, grant: False)
    assert api.get(path).status_code == 403
    assert api.put(path, json=body).status_code == 403
    register_ci_administrator(None)
    db_session.refresh(shared.flow)
    assert shared.flow.git_clone_config == before
    denied = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == shared.owner.account_id,
            models.AuditLog.action == "ci_identity_administration_denied",
        )
        .all()
    )
    assert any(row.details["operation"] == "share_flow" for row in denied)


def test_allowlist_change_is_audited(db_session: Session, shared: Shared) -> None:
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=shared.owner,
        flow_id=shared.flow.id,
        payload=_allow(shared.project_a, shared.project_c),
    )
    row = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == shared.owner.account_id,
            models.AuditLog.action == "ci_shared_flow_update",
        )
        .order_by(models.AuditLog.timestamp.desc())
        .first()
    )
    assert row is not None and row.user_id == shared.owner.id
    assert row.details["added"] == [str(shared.project_c.id)]
    assert row.details["removed"] == [str(shared.project_b.id)]


# --- 2. nothing sideways ----------------------------------------------------


@pytest.fixture
def pair(db_session: Session, shared: Shared) -> SimpleNamespace:
    """Identities for A and B on the shared flow, each with one execution."""
    principal_a, key_a, token_a = _identity(db_session, shared, shared.project_a)
    principal_b, key_b, token_b = _identity(db_session, shared, shared.project_b)
    return SimpleNamespace(
        principal_a=principal_a,
        key_a=key_a,
        token_a=token_a,
        run_a=_run(db_session, token_a, head_sha=SHA_A),
        principal_b=principal_b,
        key_b=key_b,
        token_b=token_b,
        run_b=_run(db_session, token_b, head_sha=SHA_B),
    )


def test_each_identity_lists_and_counts_only_its_own_executions(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    for token, own, sibling in (
        (pair.token_a, pair.run_a, shared.project_b),
        (pair.token_b, pair.run_b, shared.project_a),
    ):
        response = api.get("/api/v1/flows/executions", headers=_bearer(token))
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == [str(own.id)]
        assert response.headers["x-total-count"] == "1"
        # The same flow id is accepted as a filter and still shows one row.
        filtered = api.get(
            f"/api/v1/flows/executions?flow_id={shared.flow.id}",
            headers=_bearer(token),
        )
        assert [row["id"] for row in filtered.json()] == [str(own.id)]
        for text in (response.text, filtered.text):
            assert str(sibling.id) not in text
            assert sibling.slug not in text
            assert sibling.identifier not in text


@pytest.mark.parametrize(
    "suffix,method",
    [
        ("", "get"),
        ("/result", "get"),
        ("/tool-evidence", "get"),
        ("/command", "post"),
    ],
)
def test_sibling_execution_is_not_found_for_the_other_identity(
    db_session: Session,
    shared: Shared,
    pair: SimpleNamespace,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    method: str,
) -> None:
    """Status, result, startup receipt, evidence and stop are all per identity."""
    from preloop.sync.services import event_bus

    for run in (pair.run_a, pair.run_b):
        CRUDBase(models.FlowExecution).update(
            db_session,
            db_obj=run,
            obj_in={"status": "SUCCEEDED", "result": {"review": "synthetic"}},
        )
    bus = AsyncMock()
    monkeypatch.setattr(event_bus, "get_nats_client", bus)
    extra = {"json": {"command": "stop"}} if method == "post" else {}
    for token, sibling_run, own_run in (
        (pair.token_a, pair.run_b, pair.run_a),
        (pair.token_b, pair.run_a, pair.run_b),
    ):
        denied = api.request(
            method,
            f"/api/v1/flows/executions/{sibling_run.id}{suffix}",
            headers=_bearer(token),
            **extra,
        )
        missing = api.request(
            method,
            f"/api/v1/flows/executions/{uuid4()}{suffix}",
            headers=_bearer(token),
            **extra,
        )
        # A sibling's execution is indistinguishable from one that never existed.
        assert denied.status_code == 404, denied.text
        assert denied.json() == missing.json()
        own = api.request(
            method,
            f"/api/v1/flows/executions/{own_run.id}{suffix}",
            headers=_bearer(token),
            **extra,
        )
        assert own.status_code == 200, own.text
    bus.assert_not_awaited()
    db_session.refresh(pair.run_a)
    db_session.refresh(pair.run_b)
    assert pair.run_a.status == pair.run_b.status == "SUCCEEDED"


def test_identity_cannot_read_the_allowlist_or_another_flow(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    """No machine route describes the flow, its projects or other identities."""
    for path in (
        f"{ADMIN}/shared-flows/{shared.flow.id}",
        ADMIN,
        f"{ADMIN}/{pair.principal_b.id}",
        f"/api/v1/flows/{shared.flow.id}",
        "/api/v1/flows",
    ):
        response = api.get(path, headers=_bearer(pair.token_a))
        assert response.status_code == 403, (path, response.text)
        assert str(shared.project_b.id) not in response.text
        assert shared.project_b.slug not in response.text
    other_flow = api.post(
        f"/api/v1/flows/{uuid4()}/trigger",
        headers=_bearer(pair.token_a),
        json={"pr_number": 7, "head_sha": SHA_A},
    )
    assert other_flow.status_code == 403, other_flow.text


def _provider(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> tuple[AsyncMock, Mock]:
    client = Mock()
    client._request = AsyncMock(return_value=payload)
    monkeypatch.setattr(
        ci_service, "_create_review_client", AsyncMock(return_value=client)
    )
    dispatch = AsyncMock()
    from preloop.services.flow_trigger_service import FlowTriggerService

    monkeypatch.setattr(
        "preloop.sync.services.event_bus.get_nats_client",
        AsyncMock(return_value=Mock()),
    )
    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", dispatch)
    monkeypatch.setattr(ci_service, "feedback_tracker_options", lambda db, tracker: {})
    monkeypatch.setattr(ci_service, "is_triage_flow", lambda db, flow: False)
    monkeypatch.setattr(
        ci_service, "prepare_execution_routing", lambda db, flow, event: dict(event)
    )
    return dispatch, client


def _pull(project: models.Project, head_sha: str) -> dict[str, Any]:
    repository = {"id": project.identifier, "full_name": project.slug}
    return {
        "id": 7007,
        "number": 7,
        "state": "open",
        "head": {"repo": dict(repository), "sha": head_sha},
        "base": {"repo": dict(repository), "ref": "main", "sha": "c" * 40},
    }


def test_two_projects_one_flow_end_to_end(
    db_session: Session,
    shared: Shared,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two identities trigger the same flow; each run belongs to its project."""
    _, _, token_a = _identity(db_session, shared, shared.project_a)
    _, _, token_b = _identity(db_session, shared, shared.project_b)
    flow_before = copy.deepcopy(shared.flow.git_clone_config)
    prompt_before = shared.flow.prompt_template
    started = {}
    for token, project, head in (
        (token_a, shared.project_a, SHA_A),
        (token_b, shared.project_b, SHA_B),
    ):
        dispatch, client = _provider(monkeypatch, _pull(project, head))
        response = api.post(
            f"/api/v1/flows/{shared.flow.id}/trigger",
            headers=_bearer(token),
            json={"pr_number": 7, "head_sha": head},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # Preloop addressed the provider by the identity's own repository.
        client._request.assert_awaited_once_with(
            "GET", f"/repositories/{project.identifier}/pulls/7"
        )
        assert body["project_id"] == str(project.id)
        assert body["repository_identifier"] == project.identifier
        assert body["head_sha"] == head
        assert body["base_sha"] == "c" * 40
        event = dispatch.await_args.kwargs["event_data"]
        assert event["project_id"] == str(project.id)
        assert event["payload"]["repository"]["full_name"] == project.slug
        assert dispatch.await_args.kwargs["precreated_execution"].id == UUID(body["id"])
        started[project.id] = body["id"]
    assert len(set(started.values())) == 2
    for token, project, sibling in (
        (token_a, shared.project_a, shared.project_b),
        (token_b, shared.project_b, shared.project_a),
    ):
        listing = api.get("/api/v1/flows/executions", headers=_bearer(token))
        assert [row["id"] for row in listing.json()] == [started[project.id]]
        assert (
            api.get(
                f"/api/v1/flows/executions/{started[sibling.id]}",
                headers=_bearer(token),
            ).status_code
            == 404
        )
    # Triggering wrote nothing per pull request to the shared flow.
    db_session.refresh(shared.flow)
    assert shared.flow.git_clone_config == flow_before
    assert shared.flow.prompt_template == prompt_before


@pytest.mark.parametrize("target", ["sibling", "unapproved", "other_account"])
def test_identity_cannot_trigger_for_another_repository(
    db_session: Session,
    shared: Shared,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """A provider answer naming any other repository is refused.

    Covers an admitted sibling, a project of the same account that is not on
    the allowlist, and a project of a different account.
    """
    other = {
        "sibling": lambda: shared.project_b,
        "unapproved": lambda: shared.project_c,
        "other_account": lambda: _account(db_session).project_a,
    }[target]()
    _, _, token_a = _identity(db_session, shared, shared.project_a)
    dispatch, client = _provider(monkeypatch, _pull(other, SHA_B))
    response = api.post(
        f"/api/v1/flows/{shared.flow.id}/trigger",
        headers=_bearer(token_a),
        json={"pr_number": 7, "head_sha": SHA_B},
    )
    assert response.status_code == 403, response.text
    assert other.slug not in response.text
    assert other.identifier not in response.text
    # The provider was only ever asked about the identity's own repository.
    client._request.assert_awaited_once_with(
        "GET", f"/repositories/{shared.project_a.identifier}/pulls/7"
    )
    dispatch.assert_not_awaited()
    context = crud.crud_ci_principal.authenticate(db_session, token=token_a)
    assert crud.crud_ci_execution.list(db_session, context=context) == []


def test_stale_head_is_refused_on_a_shared_flow(
    db_session: Session,
    shared: Shared,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requested head must be the pull request's current head."""
    _, _, token_a = _identity(db_session, shared, shared.project_a)
    dispatch, _ = _provider(monkeypatch, _pull(shared.project_a, SHA_B))
    response = api.post(
        f"/api/v1/flows/{shared.flow.id}/trigger",
        headers=_bearer(token_a),
        json={"pr_number": 7, "head_sha": SHA_A},
    )
    assert response.status_code == 403, response.text
    dispatch.assert_not_awaited()


# --- 3. the caller selects nothing -------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        "project_id",
        "repository",
        "repository_url",
        "flow_id",
        "base_sha",
        "ref",
        "git_clone_config",
        "setup_commands",
        "ai_model_id",
        "prompt_template",
        "sandbox",
        "result_schema",
        "context_repositories",
    ],
)
def test_shared_trigger_accepts_only_pr_number_and_head(
    db_session: Session,
    shared: Shared,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    extra: str,
) -> None:
    _, _, token = _identity(db_session, shared, shared.project_a)
    trigger = AsyncMock()
    monkeypatch.setattr(flows, "trigger_ci_review", trigger)
    response = api.post(
        f"/api/v1/flows/{shared.flow.id}/trigger",
        headers=_bearer(token),
        json={
            "pr_number": 7,
            "head_sha": SHA_A,
            extra: str(shared.project_b.id),
        },
    )
    assert response.status_code == 422, response.text
    trigger.assert_not_awaited()


def test_trigger_project_resolves_from_the_binding_not_the_first_listed(
    db_session: Session, shared: Shared, pair: SimpleNamespace
) -> None:
    """The second admitted project never falls back to the first one."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    for run, project in (
        (pair.run_a, shared.project_a),
        (pair.run_b, shared.project_b),
    ):
        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator.db = db_session
        orchestrator.flow = shared.flow
        orchestrator.execution_log = run
        orchestrator.trigger_event_data = dict(run.trigger_event_details)
        assert orchestrator._resolve_trigger_project_id() == str(project.id)
        assert orchestrator._resolve_trigger_project_id(
            allow_first_project_fallback=False
        ) == str(project.id)


# --- 5. concurrent runs are isolated -----------------------------------------


@pytest.mark.asyncio
async def test_concurrent_runs_get_separate_repository_revision_and_lease(
    db_session: Session, shared: Shared, pair: SimpleNamespace
) -> None:
    flow_before = copy.deepcopy(shared.flow.git_clone_config)

    async def mint(tracker: Any, url: str, *, write: bool, client: Any) -> Any:
        assert write is False
        await asyncio.sleep(0)
        return PublicationLease(
            f"lease-for-{url}", url, datetime.now(timezone.utc) + timedelta(hours=1)
        )

    contexts: list[dict[str, Any]] = [{}, {}]
    with patch("preloop.services.read_only_review.mint_repository_lease", mint):
        leases = await asyncio.gather(
            prepare_read_only_review(db_session, shared.flow, pair.run_a, contexts[0]),
            prepare_read_only_review(db_session, shared.flow, pair.run_b, contexts[1]),
        )
    for context, lease, project, head, sibling in (
        (contexts[0], leases[0], shared.project_a, SHA_A, shared.project_b),
        (contexts[1], leases[1], shared.project_b, SHA_B, shared.project_a),
    ):
        url = f"https://github.com/{project.slug}.git"
        repositories = context["git_clone_config"]["repositories"]
        assert len(repositories) == 1
        assert repositories[0]["project_id"] == str(project.id)
        assert repositories[0]["repository_url"] == url
        assert repositories[0]["commit"] == head
        assert context["git_clone_config"]["source_branch"] == head
        assert context["git_clone_config"]["ci_project_selection"] is False
        assert list(context["git_credentials_map"]) == [url]
        assert context["git_credentials_map"][url]["token"] == f"lease-for-{url}"
        assert context["git_credentials_map"][url]["permission"] == "read"
        assert lease.repository_url == url
        assert str(sibling.id) not in repr(context)
        assert sibling.slug not in repr(context)
    assert contexts[0]["git_clone_config"] is not contexts[1]["git_clone_config"]
    # Per-run values live in the execution context, never on the shared flow.
    db_session.refresh(shared.flow)
    assert shared.flow.git_clone_config == flow_before
    assert all(
        "commit" not in repo and "repository_url" not in repo
        for repo in shared.flow.git_clone_config["repositories"]
    )


# --- 6. the v1 contract still holds per identity -----------------------------


def test_revocation_expiry_and_retire_affect_one_identity_only(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    own_b = f"/api/v1/flows/executions/{pair.run_b.id}"
    own_a = f"/api/v1/flows/executions/{pair.run_a.id}"
    crud.crud_ci_principal.revoke_key(
        db_session,
        actor=shared.owner,
        principal_id=pair.principal_a.id,
        key_id=pair.key_a.id,
    )
    assert api.get(own_a, headers=_bearer(pair.token_a)).status_code == 401
    assert api.get(own_b, headers=_bearer(pair.token_b)).status_code == 200
    # A replacement key keeps the identity's own executions and nothing else.
    issued = crud.crud_ci_administration.issue(
        db_session,
        actor=shared.owner,
        principal_id=pair.principal_a.id,
        payload=SimpleNamespace(actions=None, expires_at=None),
    )
    assert api.get(own_a, headers=_bearer(issued.token)).status_code == 200
    assert api.get(own_b, headers=_bearer(issued.token)).status_code == 404
    # An expired key stops authenticating.
    key = crud.crud_api_key.get(db_session, id=issued.key_id)
    key.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=1
    )
    db_session.commit()
    assert api.get(own_a, headers=_bearer(issued.token)).status_code == 401
    crud.crud_ci_principal.retire(
        db_session, actor=shared.owner, principal_id=pair.principal_a.id
    )
    assert crud.crud_ci_principal.authenticate(db_session, token=pair.token_a) is None
    assert api.get(own_b, headers=_bearer(pair.token_b)).status_code == 200
    counts = crud.crud_ci_administration.shared_flow(
        db_session, actor=shared.owner, flow_id=shared.flow.id
    ).projects
    assert [row.identities for row in counts] == [0, 1]


def test_removing_a_project_denies_its_identity_and_pending_run_only(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    """An allowlist change reaches in-flight work at the next authorization."""
    crud.crud_ci_execution.authorize_dispatch(db_session, execution=pair.run_a)
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=shared.owner,
        flow_id=shared.flow.id,
        payload=_allow(shared.project_b),
    )
    own_a = f"/api/v1/flows/executions/{pair.run_a.id}"
    assert api.get(own_a, headers=_bearer(pair.token_a)).status_code == 401
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.authorize_dispatch(db_session, execution=pair.run_a)
    # The sibling that stayed on the allowlist is untouched.
    crud.crud_ci_execution.authorize_dispatch(db_session, execution=pair.run_b)
    assert (
        api.get(
            f"/api/v1/flows/executions/{pair.run_b.id}",
            headers=_bearer(pair.token_b),
        ).status_code
        == 200
    )
    # Re-admitting the project restores the same identity; nothing was rebound.
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=shared.owner,
        flow_id=shared.flow.id,
        payload=_allow(shared.project_b, shared.project_a),
    )
    assert api.get(own_a, headers=_bearer(pair.token_a)).status_code == 200


@pytest.mark.asyncio
async def test_removed_project_gets_no_lease_for_an_accepted_run(
    db_session: Session, shared: Shared, pair: SimpleNamespace
) -> None:
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=shared.owner,
        flow_id=shared.flow.id,
        payload=_allow(shared.project_b),
    )
    db_session.refresh(shared.flow)
    mint = AsyncMock()
    with patch("preloop.services.read_only_review.mint_repository_lease", mint):
        with pytest.raises(PermissionError):
            await prepare_read_only_review(db_session, shared.flow, pair.run_a, {})
    mint.assert_not_awaited()


def test_own_result_and_receipt_are_readable_on_a_shared_flow(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=pair.run_b,
        obj_in={"status": "SUCCEEDED", "result": {"review": "for project B"}},
    )
    crud.crud_flow_execution.record_startup_receipt(
        db_session,
        execution_id=pair.run_b.id,
        receipt={
            "commit": SHA_B,
            "checkout_verified": True,
            "setup_completed": True,
            "agent_started_at": "2030-01-01T00:00:00+00:00",
            "lease_revoked": None,
        },
    )
    result = api.get(
        f"/api/v1/flows/executions/{pair.run_b.id}/result",
        headers=_bearer(pair.token_b),
    )
    assert result.status_code == 200, result.text
    assert result.json()["result"] == {"review": "for project B"}
    assert result.json()["project_id"] == str(shared.project_b.id)
    assert result.json()["head_sha"] == SHA_B
    assert result.json()["startup_receipt"]["commit"] == SHA_B
    assert str(shared.project_a.id) not in result.text


def test_completion_callback_is_delivered_to_the_owning_identity_only(
    db_session: Session, shared: Shared, pair: SimpleNamespace
) -> None:
    """An identity's subscription never carries a sibling's completion."""
    from preloop.schemas.ci_subscription import CiSubscriptionCreate

    endpoints = {}
    for label, token in (("a", pair.token_a), ("b", pair.token_b)):
        context = crud.crud_ci_principal.authenticate(db_session, token=token)
        endpoint, _ = crud.crud_ci_subscription.create(
            db_session,
            context=context,
            payload=CiSubscriptionCreate(url=f"https://example.com/{label}"),
        )
        endpoints[label] = endpoint
        owned = crud.crud_ci_subscription.list_owned(db_session, context=context)
        assert [row.id for row in owned] == [endpoint.id]
    for run in (pair.run_a, pair.run_b):
        CRUDBase(models.FlowExecution).update(
            db_session,
            db_obj=run,
            obj_in={"status": "SUCCEEDED", "result": {"review": "synthetic"}},
        )

    def payload(endpoint: Any, run: Any) -> Any:
        return crud.crud_ci_subscription.callback_payload(
            db_session,
            endpoint=endpoint,
            account_id=shared.owner.account_id,
            event_type="flow.execution.finished",
            subject_id=run.id,
        )

    own = payload(endpoints["a"], pair.run_a)
    assert own is not None
    assert own["project_id"] == str(shared.project_a.id)
    assert own["head_sha"] == SHA_A and own["result_ready"] is True
    assert payload(endpoints["a"], pair.run_b) is None
    assert payload(endpoints["b"], pair.run_a) is None
    assert payload(endpoints["b"], pair.run_b)["project_id"] == str(shared.project_b.id)
    # Removing project A from the allowlist stops its callbacks too.
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=shared.owner,
        flow_id=shared.flow.id,
        payload=_allow(shared.project_b),
    )
    assert payload(endpoints["a"], pair.run_a) is None
    assert payload(endpoints["b"], pair.run_b) is not None


# --- 7. the account is the boundary ------------------------------------------


def test_a_flow_cannot_be_shared_across_accounts(
    db_session: Session, api: TestClient, actor: dict[str, Any]
) -> None:
    home = _account(db_session)
    foreign = _account(db_session)
    assert home.owner.account_id != foreign.owner.account_id
    path = f"{ADMIN}/shared-flows/{home.flow.id}"

    # The other account cannot see or change the flow.
    actor["user"] = foreign.owner
    assert api.get(path).status_code == 404
    assert (
        api.put(path, json={"project_ids": [str(foreign.project_a.id)]}).status_code
        == 404
    )

    # The flow's own administrator cannot admit a project of another account,
    # and the refusal does not confirm that the project exists.
    actor["user"] = home.owner
    before = copy.deepcopy(home.flow.git_clone_config)
    refused = api.put(
        path,
        json={"project_ids": [str(home.project_a.id), str(foreign.project_a.id)]},
    )
    unknown = api.put(
        path, json={"project_ids": [str(home.project_a.id), str(uuid4())]}
    )
    assert refused.status_code == unknown.status_code == 409
    assert refused.json() == unknown.json()
    assert refused.json()["detail"] == SHARED_FLOW_PROJECT_REFUSED
    db_session.refresh(home.flow)
    assert home.flow.git_clone_config == before


def test_hand_edited_cross_account_entry_grants_nothing(
    db_session: Session,
) -> None:
    """Listing a foreign project in the flow JSON cannot bind either side."""
    home = _account(db_session)
    foreign = _account(db_session)
    home.flow.trigger_project_ids = [
        str(home.project_a.id),
        str(foreign.project_a.id),
    ]
    home.flow.git_clone_config = {
        **home.flow.git_clone_config,
        "ci_project_selection": True,
        "repositories": [
            *home.flow.git_clone_config["repositories"],
            {
                "project_id": str(foreign.project_a.id),
                "tracker_id": str(foreign.tracker_id),
            },
        ],
    }
    db_session.commit()
    grant = CiGrant(
        version=1,
        project_id=foreign.project_a.id,
        flow_id=home.flow.id,
        actions=tuple(CiAction),
    )
    for owner in (home.owner, foreign.owner):
        with pytest.raises((ValueError, PermissionError)):
            crud.crud_ci_principal.provision(
                db_session, actor=owner, name="Cross account", grant=grant
            )
    # The administrator view shows the foreign entry without its name or slug.
    view = crud.crud_ci_administration.shared_flow(
        db_session, actor=home.owner, flow_id=home.flow.id
    )
    foreign_row = [
        row for row in view.projects if row.project_id == foreign.project_a.id
    ]
    assert len(foreign_row) == 1
    assert foreign_row[0].name is None and foreign_row[0].repository_slug is None
    # The home account's own identity still works and stays on its project.
    _, _, token = _identity(db_session, home, home.project_a)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None and context.project_id == home.project_a.id


# --- the two-repository check an operator can run ----------------------------


def _verify_script() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[3] / "scripts" / "verify_shared_ci_flow.py"
    spec = importlib.util.spec_from_file_location("verify_shared_ci_flow", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_check_passes_against_two_identities_on_one_flow(
    db_session: Session, shared: Shared, pair: SimpleNamespace, api: TestClient
) -> None:
    script = _verify_script()
    calls: list[tuple[str, str]] = []

    def request(
        token: str, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        calls.append((method, path))
        response = api.request(
            method, path, headers=_bearer(token), **({"json": body} if body else {})
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    results = script.run_checks(
        request,
        flow=str(shared.flow.id),
        tokens={"A": pair.token_a, "B": pair.token_b},
        projects={"A": str(shared.project_a.id), "B": str(shared.project_b.id)},
    )
    # No receipt exists in this fixture, so that check is skipped, not passed.
    skipped = [text for passed, text in results if passed is None]
    assert len(skipped) == 2 and all("no startup receipt" in t for t in skipped)
    assert all(passed is not False for passed, _ in results), results
    assert sum(1 for passed, _ in results if passed is True) >= 20
    assert any("rejected for its extra field" in text for _, text in results)
    text = repr(results)
    assert pair.token_a not in text and pair.token_b not in text
    # Both directions of the cross-read were actually probed.
    for run in (pair.run_a, pair.run_b):
        assert ("GET", f"/api/v1/flows/executions/{run.id}/result") in calls
    # Nothing was started by the malformed triggers.
    for token in (pair.token_a, pair.token_b):
        context = crud.crud_ci_principal.authenticate(db_session, token=token)
        assert len(crud.crud_ci_execution.list(db_session, context=context)) == 1


def test_operator_check_reports_a_sideways_read() -> None:
    """A server that leaked a sibling's execution fails the check."""
    script = _verify_script()
    flow, project_a, project_b = str(uuid4()), str(uuid4()), str(uuid4())
    rows = {
        "token-a": [{"id": str(uuid4()), "flow_id": flow, "project_id": project_a}],
        "token-b": [{"id": str(uuid4()), "flow_id": flow, "project_id": project_b}],
    }

    def leaky(
        token: str, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        if path.endswith("/executions?limit=100"):
            return 200, rows[token]
        if method == "POST":
            return 422, None
        if "/executions/" in path:
            return 200, {"id": "leaked"}
        return 403, None

    results = script.run_checks(
        leaky,
        flow=flow,
        tokens={"A": "token-a", "B": "token-b"},
        projects={"A": project_a, "B": project_b},
    )
    failed = [description for passed, description in results if passed is False]
    assert len(failed) == 6, failed
    assert all("cannot read" in description for description in failed)


@pytest.mark.parametrize(
    "argv,environment",
    [
        (["--url", "http://example.com"], {"A": "one", "B": "two"}),
        (["--url", "https://user:pw@example.com"], {"A": "one", "B": "two"}),
        (["--url", "https://example.com"], {"A": "same", "B": "same"}),
        (["--url", "https://example.com"], {"A": "one", "B": ""}),
    ],
)
def test_operator_check_refuses_unsafe_invocations(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], environment: dict[str, str]
) -> None:
    script = _verify_script()
    monkeypatch.setenv("PRELOOP_CI_TOKEN_A", environment["A"])
    monkeypatch.setenv("PRELOOP_CI_TOKEN_B", environment["B"])
    identifiers = [
        "--flow",
        str(uuid4()),
        "--project-a",
        str(uuid4()),
        "--project-b",
        str(uuid4()),
    ]
    with pytest.raises(SystemExit) as refused:
        script.main([*argv, *identifiers])
    assert refused.value.code not in (0, None)


# --- moving a working dedicated identity to a shared flow --------------------


def test_project_moves_to_a_shared_flow_while_its_dedicated_review_keeps_working(
    db_session: Session, api: TestClient
) -> None:
    """The documented cut-over: new shared flow, new identity, then retire."""
    account = _account(db_session)
    old_principal, _, old_token = _identity(db_session, account, account.project_a)
    old_run = _run(db_session, old_token, head_sha=SHA_A)
    dedicated_flow = account.flow

    # 1. A second read-only review flow, shared by A and B. The dedicated flow
    #    and its identity are not touched.
    shared_flow = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": "Shared review",
            "prompt_template": dedicated_flow.prompt_template,
            "agent_type": "codex",
            "agent_config": {},
            "account_id": account.owner.account_id,
            "is_enabled": True,
            "runner_pool": "server",
            "git_clone_config": {
                "enabled": True,
                "publication_mode": "read_only",
                "repositories": [
                    {
                        "project_id": str(account.project_a.id),
                        "tracker_id": str(account.tracker_id),
                    }
                ],
            },
        },
    )
    crud.crud_ci_administration.share_flow(
        db_session,
        actor=account.owner,
        flow_id=shared_flow.id,
        payload=_allow(account.project_a, account.project_b),
    )
    # 2. A new identity for the same project on the shared flow.
    account.flow = shared_flow
    new_principal, _, new_token = _identity(db_session, account, account.project_a)
    new_run = _run(db_session, new_token, head_sha=SHA_B)
    assert new_principal.repository_binding["ci_project_selection"] is True

    # 3. Both work side by side during the cut-over, each on its own flow.
    for token, own, other in (
        (old_token, old_run, new_run),
        (new_token, new_run, old_run),
    ):
        listing = api.get("/api/v1/flows/executions", headers=_bearer(token))
        assert [row["id"] for row in listing.json()] == [str(own.id)]
        assert (
            api.get(
                f"/api/v1/flows/executions/{other.id}", headers=_bearer(token)
            ).status_code
            == 404
        )
    old_context = crud.crud_ci_principal.authenticate(db_session, token=old_token)
    assert old_context is not None and old_context.flow_id == dedicated_flow.id

    # 4. Retire the old identity; the shared one is unaffected and the old
    #    execution history stays attributed to the retired identity.
    crud.crud_ci_principal.retire(
        db_session, actor=account.owner, principal_id=old_principal.id
    )
    assert crud.crud_ci_principal.authenticate(db_session, token=old_token) is None
    assert (
        api.get(
            f"/api/v1/flows/executions/{new_run.id}", headers=_bearer(new_token)
        ).status_code
        == 200
    )
    db_session.refresh(old_run)
    assert old_run.ci_principal_id == old_principal.id


def test_release_gate_script_runs_the_two_repository_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify_restricted_ci.py shared-flow`` reaches the isolation check."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parents[3] / "scripts" / "verify_restricted_ci.py"
    spec = importlib.util.spec_from_file_location("verify_restricted_ci", path)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert "api/test_ci_shared_flow.py" in gate.TESTS
    monkeypatch.setenv("PRELOOP_CI_TOKEN_A", "one")
    monkeypatch.setenv("PRELOOP_CI_TOKEN_B", "one")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_restricted_ci.py",
            "shared-flow",
            "--url",
            "https://example.com",
            "--flow",
            str(uuid4()),
            "--project-a",
            str(uuid4()),
            "--project-b",
            str(uuid4()),
        ],
    )
    with pytest.raises(SystemExit) as refused:
        gate.main()
    # Refused by the shared-flow check itself (identical tokens), not pytest.
    assert "two different CI tokens" in str(refused.value.code)


def test_operator_check_does_not_pass_what_it_could_not_probe() -> None:
    """No executions, no receipts and a credential refusal are all skipped."""
    script = _verify_script()
    flow, project_a, project_b = str(uuid4()), str(uuid4()), str(uuid4())

    def empty(
        token: str, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        if path.endswith("/executions?limit=100"):
            return 200, []
        return 403, None

    results = script.run_checks(
        empty,
        flow=flow,
        tokens={"A": "token-a", "B": "token-b"},
        projects={"A": project_a, "B": project_b},
    )
    skipped = [text for passed, text in results if passed is None]
    assert len(skipped) == 6
    assert sum("cannot test the project override" in text for text in skipped) == 2
    assert sum("no startup receipt" in text for text in skipped) == 2
    assert sum("no execution of" in text for text in skipped) == 2
    assert not [text for passed, text in results if passed is False]

    def ignores_extra_field(
        token: str, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        if method == "POST":
            return 200, {"id": str(uuid4())}
        return empty(token, method, path, body)

    results = script.run_checks(
        ignores_extra_field,
        flow=flow,
        tokens={"A": "token-a", "B": "token-b"},
        projects={"A": project_a, "B": project_b},
    )
    failed = [text for passed, text in results if passed is False]
    assert len(failed) == 2 and all("was not rejected" in text for text in failed)


@pytest.mark.parametrize("case", ["tracker", "project", "organization", "slug"])
def test_project_that_cannot_be_scoped_is_refused_from_the_allowlist(
    db_session: Session,
    api: TestClient,
    actor: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A non-App tracker, an inactive project or organisation, or no slug."""
    account = _account(db_session)
    actor["user"] = account.owner
    if case == "tracker":
        checked: list[Any] = []

        def not_an_app(tracker: Any) -> None:
            checked.append(tracker.id)
            raise ValueError("synthetic: not a GitHub App installation")

        monkeypatch.setattr(
            publication_credentials, "validate_publication_tracker", not_an_app
        )
    elif case == "project":
        account.project_b.is_active = False
    elif case == "organization":
        other = crud.crud_organization.create(
            db_session,
            obj_in={
                "name": "Inactive",
                "identifier": str(uuid4()),
                "tracker_id": account.tracker_id,
                "is_active": False,
            },
        )
        account.project_b.organization_id = other.id
    else:
        account.project_b.slug = None
    db_session.commit()
    before = copy.deepcopy(account.flow.git_clone_config)
    response = api.put(
        f"{ADMIN}/shared-flows/{account.flow.id}",
        json={"project_ids": [str(account.project_a.id), str(account.project_b.id)]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == SHARED_FLOW_PROJECT_REFUSED
    if case == "tracker":
        assert checked == [account.tracker_id]
    db_session.refresh(account.flow)
    assert account.flow.git_clone_config == before
    assert account.flow.trigger_project_ids == [str(account.project_a.id)]


def test_inactive_tracker_refuses_every_project_on_it(
    db_session: Session, api: TestClient, actor: dict[str, Any]
) -> None:
    account = _account(db_session)
    actor["user"] = account.owner
    tracker = crud.crud_tracker.get(
        db_session, id=account.tracker_id, account_id=str(account.owner.account_id)
    )
    tracker.is_active = False
    db_session.commit()
    response = api.put(
        f"{ADMIN}/shared-flows/{account.flow.id}",
        json={"project_ids": [str(account.project_a.id)]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == SHARED_FLOW_PROJECT_REFUSED
