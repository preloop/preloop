"""Account scoping for models owned through ``Tracker.account_id``.

Project, Organization, Issue and IssueDuplicate have no ``account_id``
column. Lookups that pass ``account_id`` must still return only rows owned by
that account, and the API must answer 404 for another account's ids.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_comment,
    crud_issue,
    crud_issue_duplicate,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.models.ai_model import AIModel
from preloop.models.models.user import User


def _tree(db: Session, account_id, tag: str) -> dict:
    """Create tracker -> organization -> project -> issue for one account."""
    tracker = crud_tracker.create(
        db,
        obj_in={
            "name": f"{tag} tracker",
            "tracker_type": "github",
            "url": f"https://github.com/{tag}",
            "api_key": f"{tag}-secret-token",
            "account_id": str(account_id),
            "is_active": True,
        },
    )
    org = crud_organization.create(
        db,
        obj_in={
            "name": f"{tag}-org",
            "identifier": f"{tag}-org",
            "tracker_id": str(tracker.id),
            "is_active": True,
        },
    )
    project = crud_project.create(
        db,
        obj_in={
            "name": f"{tag}-project",
            "identifier": f"{tag}-project",
            "slug": f"{tag}-org/{tag}-project",
            "description": f"{tag} description",
            "organization_id": str(org.id),
            "is_active": True,
        },
    )
    issues = [
        crud_issue.create(
            db,
            obj_in={
                "title": f"{tag} issue {n}",
                "external_id": f"{tag}-{n}",
                "key": f"{tag.upper()}-{n}",
                "project_id": str(project.id),
                "tracker_id": str(tracker.id),
            },
        )
        for n in (1, 2)
    ]
    db.flush()
    return {
        "account_id": account_id,
        "tracker": tracker,
        "organization": org,
        "project": project,
        "issue": issues[0],
        "issues": issues,
    }


@pytest.fixture
def own(db_session: Session, test_user: User) -> dict:
    return _tree(db_session, test_user.account_id, "own")


@pytest.fixture
def other(db_session: Session) -> dict:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    db_session.flush()
    data = _tree(db_session, account.id, "other")
    model = AIModel(
        name=f"other-model-{uuid.uuid4()}",
        model_identifier="gpt-test",
        provider_name="openai",
        api_endpoint="https://api.openai.com/v1",
        api_key="k",
        model_parameters={},
        account_id=account.id,
    )
    db_session.add(model)
    db_session.flush()
    data["duplicate"] = crud_issue_duplicate.create(
        db_session,
        obj_in={
            "issue1_id": data["issues"][0].id,
            "issue2_id": data["issues"][1].id,
            "decision": "duplicate",
            "ai_model_id": model.id,
        },
    )
    db_session.flush()
    return data


# --- CRUD layer -------------------------------------------------------------


@pytest.mark.parametrize(
    "crud, key",
    [
        (crud_organization, "organization"),
        (crud_project, "project"),
        (crud_issue, "issue"),
    ],
)
def test_get_is_scoped_to_account(db_session, own, other, crud, key):
    own_id = own[key].id
    other_id = other[key].id
    acct = str(own["account_id"])

    assert crud.get(db_session, id=own_id, account_id=acct).id == own_id
    assert crud.get(db_session, id=other_id, account_id=acct) is None
    # Without account_id the lookup stays unscoped (internal callers).
    assert crud.get(db_session, id=other_id).id == other_id


@pytest.mark.parametrize(
    "crud, key",
    [
        (crud_organization, "organization"),
        (crud_project, "project"),
        (crud_issue, "issue"),
    ],
)
def test_get_multi_is_scoped_to_account(db_session, own, other, crud, key):
    rows = crud.get_multi(db_session, account_id=str(own["account_id"]), limit=1000)
    ids = {row.id for row in rows}
    assert own[key].id in ids
    assert other[key].id not in ids


def test_issue_duplicate_get_multi_is_scoped(db_session, own, other):
    rows = crud_issue_duplicate.get_multi(
        db_session, account_id=str(own["account_id"]), limit=1000
    )
    assert other["duplicate"].id not in {row.id for row in rows}
    rows = crud_issue_duplicate.get_multi(
        db_session, account_id=str(other["account_id"]), limit=1000
    )
    assert other["duplicate"].id in {row.id for row in rows}


def test_project_deactivate_is_scoped(db_session, own, other):
    assert (
        crud_project.deactivate(
            db_session, id=other["project"].id, account_id=str(own["account_id"])
        )
        is None
    )
    db_session.refresh(other["project"])
    assert other["project"].is_active


def test_unscopable_model_refuses_account_id(db_session):
    """A model with no account path must fail closed, not drop the filter."""
    with pytest.raises(TypeError, match="cannot scope"):
        crud_comment.get(db_session, id=uuid.uuid4(), account_id=str(uuid.uuid4()))
    with pytest.raises(TypeError, match="cannot scope"):
        crud_comment.get_multi(db_session, account_id=str(uuid.uuid4()))


# --- API: organizations and projects ---------------------------------------


@pytest.mark.parametrize("kind", ["organizations", "projects"])
def test_cross_account_read_update_delete_is_404(
    client: TestClient, db_session, own, other, kind
):
    target = other["organization" if kind == "organizations" else "project"]
    url = f"/api/v1/{kind}/{target.id}"
    original_name = target.name

    assert client.get(url).status_code == 404
    assert client.put(url, json={"name": "renamed"}).status_code == 404
    assert client.delete(url).status_code == 404

    db_session.expire_all()
    model = crud_organization if kind == "organizations" else crud_project
    row = model.get(db_session, id=target.id)
    assert row is not None
    assert row.name == original_name


@pytest.mark.parametrize("kind", ["organizations", "projects"])
def test_same_account_read_update_delete_works(
    client: TestClient, db_session, own, other, kind
):
    target = own["organization" if kind == "organizations" else "project"]
    url = f"/api/v1/{kind}/{target.id}"

    assert client.get(url).status_code == 200
    resp = client.put(url, json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"
    assert client.delete(url).status_code == 204


# --- API: issues -------------------------------------------------------------


@pytest.fixture
def analytics_client(app):
    """Mount the issue duplicate routes, which EE serves through a plugin."""
    from preloop.api.endpoints import issue_duplicates

    app.include_router(issue_duplicates.router, prefix="/api/v1/oss-analytics")
    with TestClient(app) as test_client:
        yield test_client


def test_cross_account_issue_read_and_update_is_404(client: TestClient, own, other):
    url = f"/api/v1/issues/{other['issue'].id}"
    assert client.get(url).status_code == 404
    assert client.put(url, json={"title": "x"}).status_code in (403, 404)


def test_same_account_issue_read_works(client: TestClient, own, other):
    resp = client.get(f"/api/v1/issues/{own['issue'].id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == own["issue"].title


def test_confirmed_duplicates_exclude_other_accounts(analytics_client, own, other):
    resp = analytics_client.get("/api/v1/oss-analytics/issue-duplicates/confirmed")
    assert resp.status_code == 200
    assert str(other["duplicate"].id) not in {row["id"] for row in resp.json()}


def test_duplicate_resolution_rejects_other_account_issues(
    analytics_client, own, other
):
    resp = analytics_client.post(
        "/api/v1/oss-analytics/ai-suggestion",
        json={
            "issue1_id": str(other["issues"][0].id),
            "issue2_id": str(other["issues"][1].id),
            "resolution": "merged",
        },
    )
    assert resp.status_code == 404


# --- Internal sinks that take a project id from flow input -------------------


class _NoCloseSession:
    """Share the test session with code that closes its own session."""

    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


def _executor():
    from preloop.agents.container import ContainerAgentExecutor

    return ContainerAgentExecutor(
        agent_type="codex",
        config={"test": True},
        image="test-image:latest",
        use_kubernetes=False,
    )


def test_container_token_lookup_is_account_scoped(db_session, own, other):
    executor = _executor()
    acct = str(own["account_id"])
    with patch(
        "preloop.models.db.session.get_db_session",
        side_effect=lambda: iter([_NoCloseSession(db_session)]),
    ):
        token, _, _ = executor._get_token_from_project(str(other["project"].id), acct)
        assert token is None
        token, _, _ = executor._get_token_from_project(str(other["project"].id), None)
        assert token is None
        token, _, _ = executor._get_token_from_project(str(own["project"].id), acct)
        assert token == "own-secret-token"


def test_container_repo_url_lookup_is_account_scoped(db_session, own, other):
    executor = _executor()
    acct = str(own["account_id"])
    with patch(
        "preloop.models.db.session.get_db_session",
        side_effect=lambda: iter([_NoCloseSession(db_session)]),
    ):
        assert (
            executor._get_repo_url_from_project(str(other["project"].id), acct) is None
        )
        assert executor._get_repo_url_from_project(str(own["project"].id), acct)


@pytest.mark.asyncio
async def test_project_prompt_resolver_is_account_scoped(db_session, own, other):
    from preloop.services.prompt_resolvers.base import ResolverContext
    from preloop.services.prompt_resolvers.project import ProjectResolver

    def ctx(project_id, account_id):
        return ResolverContext(
            db=db_session,
            trigger_event_data={"payload": {"project_id": str(project_id)}},
            flow_id="f",
            execution_id="e",
            account_id=account_id,
        )

    resolver = ProjectResolver()
    acct = str(own["account_id"])
    assert await resolver.resolve("name", ctx(other["project"].id, acct)) is None
    assert await resolver.resolve("name", ctx(other["project"].id, None)) is None
    assert (
        await resolver.resolve("name", ctx(own["project"].id, acct))
        == own["project"].name
    )


def test_repository_binding_ignores_other_account_project(db_session, own, other):
    from preloop.services.repository_binding import _load_bindings

    other["project"].settings = {
        "repository_bindings": [{"tracker_id": str(other["tracker"].id)}]
    }
    db_session.flush()
    bindings, source, _ = _load_bindings(
        db_session, {}, str(other["project"].id), str(own["account_id"])
    )
    assert bindings == []
    assert source == "project"


# --- Orchestrator: a flow without an account resolves no project -------------


def _orchestrator(db, account_id):
    from types import SimpleNamespace

    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db
    orchestrator.flow = SimpleNamespace(account_id=account_id)
    return orchestrator


def test_orchestrator_flow_account_id_never_stringifies_none(db_session, own):
    assert _orchestrator(db_session, None)._flow_account_id() is None
    acct = own["account_id"]
    assert _orchestrator(db_session, acct)._flow_account_id() == str(acct)


def test_orchestrator_project_tracker_is_account_scoped(db_session, own, other):
    project_id = str(other["project"].id)
    assert (
        _orchestrator(db_session, None)._resolve_project_tracker_id(project_id) is None
    )
    assert (
        _orchestrator(db_session, own["account_id"])._resolve_project_tracker_id(
            project_id
        )
        is None
    )
    assert _orchestrator(db_session, other["account_id"])._resolve_project_tracker_id(
        project_id
    ) == str(other["tracker"].id)


@pytest.mark.asyncio
async def test_follow_up_filing_without_account_is_a_domain_error(db_session, other):
    from preloop.services.follow_up_filing import FollowUpFilingError

    orchestrator = _orchestrator(db_session, None)
    orchestrator._follow_up_filing_project_id = lambda plan: str(other["project"].id)
    with pytest.raises(FollowUpFilingError, match="no account"):
        await orchestrator._follow_up_filing_target(object())
