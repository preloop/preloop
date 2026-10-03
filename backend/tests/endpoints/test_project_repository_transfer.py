"""Moving a GitHub repository between organisations (#1159).

The GitHub repository ID is the stable identity of a repository project.
These tests run against PostgreSQL through the real app, because the
original failure was in response serialization after the row was written.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from preloop.api.endpoints import projects as projects_endpoint
from preloop.models.crud import (
    crud_issue,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.project import Project
from preloop.models.models.tracker_scope_rule import TrackerScopeRule

REPO_ID = "987654321"


def _include_org(db, tracker, org_identifier):
    db.add(
        TrackerScopeRule(
            tracker_id=tracker.id,
            scope_type="ORGANIZATION",
            rule_type="INCLUDE",
            identifier=org_identifier,
        )
    )
    db.flush()


def _github_tracker(db, account_id, name):
    return crud_tracker.create(
        db,
        obj_in={
            "name": name,
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "test_key",
            "account_id": account_id,
            "is_active": True,
        },
    )


@pytest.fixture
def raw_client(app):
    """Client that returns 500s instead of re-raising them."""
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def moved_repo(db_session, test_user):
    """A repo registered under its old org, now visible to a new org.

    Each org has its own GitHub tracker, as with one GitHub App
    installation per organisation.
    """
    old_tracker = _github_tracker(db_session, test_user.account_id, "old install")
    new_tracker = _github_tracker(db_session, test_user.account_id, "new install")
    old_org = crud_organization.create(
        db_session,
        obj_in={
            "name": "old-owner",
            "identifier": "1001",
            "tracker_id": old_tracker.id,
        },
    )
    new_org = crud_organization.create(
        db_session,
        obj_in={
            "name": "new-owner",
            "identifier": "2002",
            "tracker_id": new_tracker.id,
        },
    )
    _include_org(db_session, old_tracker, "1001")
    _include_org(db_session, new_tracker, "2002")
    project = crud_project.create(
        db_session,
        obj_in={
            "id": str(uuid.uuid4()),
            "name": "widget",
            "identifier": REPO_ID,
            "slug": "old-owner/widget",
            "organization_id": old_org.id,
            "meta_data": {"full_name": "old-owner/widget"},
        },
    )
    issue = crud_issue.create(
        db_session,
        obj_in={
            "external_id": "55",
            "key": "old-owner/widget#1",
            "title": "kept across the move",
            "status": "open",
            "project_id": project.id,
            "tracker_id": old_tracker.id,
        },
    )
    db_session.flush()
    return {
        "old_org": old_org,
        "new_org": new_org,
        "old_tracker": old_tracker,
        "new_tracker": new_tracker,
        "project": project,
        "issue": issue,
    }


@pytest.fixture
def destination_sees(monkeypatch):
    """Stub the live destination check: the repos the new install can see."""
    visible = {}

    def fake_list(tracker, organization):
        return visible.get(str(organization.identifier), [])

    monkeypatch.setattr(projects_endpoint, "_list_destination_repositories", fake_list)
    return visible


def _repo(full_name, repo_id=REPO_ID):
    return {
        "id": repo_id,
        "identifier": repo_id,
        "name": full_name.split("/")[1],
        "meta_data": {"full_name": full_name},
    }


# --- The HTTP 500 ---------------------------------------------------------


def test_create_project_returns_201_not_500(raw_client, db_session, test_tracker):
    """Root cause: the handler returned datetimes for str response fields."""
    org = crud_organization.create(
        db_session,
        obj_in={"name": "o", "identifier": "3003", "tracker_id": test_tracker.id},
    )
    db_session.flush()
    response = raw_client.post(
        "/api/v1/projects",
        json={"name": "x", "identifier": "fresh", "organization_id": str(org.id)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_same_repo_id_in_other_org_is_409_and_writes_nothing(
    raw_client, db_session, moved_repo
):
    project = moved_repo["project"]
    response = raw_client.post(
        "/api/v1/projects",
        json={
            "name": "widget",
            "identifier": REPO_ID,
            "organization_id": str(moved_repo["new_org"].id),
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "repository_already_registered"
    assert detail["existing_project_id"] == str(project.id)
    assert f"/api/v1/projects/{project.id}/transfer" in detail["message"]
    rows = db_session.query(Project).filter(Project.identifier == REPO_ID).all()
    assert [str(p.id) for p in rows] == [str(project.id)]


def test_same_identifier_on_a_different_host_is_not_a_duplicate(
    raw_client, db_session, moved_repo, test_user
):
    """Repository IDs are only unique per GitHub host."""
    ghe = crud_tracker.create(
        db_session,
        obj_in={
            "name": "ghe",
            "tracker_type": "github",
            "url": "https://ghe.example.com",
            "api_key": "k",
            "account_id": test_user.account_id,
            "is_active": True,
        },
    )
    org = crud_organization.create(
        db_session, obj_in={"name": "ghe-org", "identifier": "9", "tracker_id": ghe.id}
    )
    db_session.flush()
    response = raw_client.post(
        "/api/v1/projects",
        json={"name": "w", "identifier": REPO_ID, "organization_id": str(org.id)},
    )
    assert response.status_code == 201, response.text


# --- Transfer -------------------------------------------------------------


def _transfer(client, project, org, dry_run):
    body = {"organization_id": str(org.id)}
    if dry_run is not None:
        body["dry_run"] = dry_run
    return client.post(f"/api/v1/projects/{project.id}/transfer", json=body)


def test_transfer_preview_changes_nothing(
    raw_client, db_session, moved_repo, destination_sees
):
    destination_sees["2002"] = [_repo("new-owner/widget")]
    project = moved_repo["project"]
    response = _transfer(raw_client, project, moved_repo["new_org"], dry_run=None)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "preview"
    assert receipt["source"]["full_name"] == "old-owner/widget"
    assert receipt["destination"]["full_name"] == "new-owner/widget"
    db_session.refresh(project)
    assert str(project.organization_id) == str(moved_repo["old_org"].id)
    assert project.slug == "old-owner/widget"


def test_transfer_rebinds_and_preserves_history(
    raw_client, db_session, moved_repo, destination_sees
):
    destination_sees["2002"] = [_repo("new-owner/widget")]
    project = moved_repo["project"]
    project_id = str(project.id)

    response = _transfer(raw_client, project, moved_repo["new_org"], dry_run=False)

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "transferred"
    assert receipt["project_id"] == project_id
    db_session.expire_all()
    project = crud_project.get(db_session, id=project_id)
    assert str(project.organization_id) == str(moved_repo["new_org"].id)
    # Review publication and clone read the slug / full_name.
    assert project.slug == "new-owner/widget"
    assert project.meta_data["full_name"] == "new-owner/widget"
    # Same row, same issues, no duplicate.
    assert [i.id for i in project.issues] == [moved_repo["issue"].id]
    assert db_session.query(Project).filter(Project.identifier == REPO_ID).count() == 1
    history = project.meta_data["repository_transfers"]
    assert history[-1]["from_full_name"] == "old-owner/widget"
    assert history[-1]["to_full_name"] == "new-owner/widget"
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "project_transferred")
        .filter(AuditLog.resource_id == project_id)
        .all()
    )
    assert len(audit) == 1
    assert audit[0].details["from_organization_id"] == str(moved_repo["old_org"].id)


def test_transfer_is_idempotent(raw_client, db_session, moved_repo, destination_sees):
    destination_sees["2002"] = [_repo("new-owner/widget")]
    project = moved_repo["project"]
    first = _transfer(raw_client, project, moved_repo["new_org"], dry_run=False)
    second = _transfer(raw_client, project, moved_repo["new_org"], dry_run=False)
    assert first.json()["status"] == "transferred"
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "unchanged"
    db_session.expire_all()
    project = crud_project.get(db_session, id=str(project.id))
    assert len(project.meta_data["repository_transfers"]) == 1
    count = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "project_transferred")
        .count()
    )
    assert count == 1


def test_transfer_reports_what_is_not_carried_over(
    raw_client, db_session, moved_repo, destination_sees
):
    """Source-integration repo grants stay behind and are named, not copied."""
    destination_sees["2002"] = [_repo("new-owner/widget")]
    db_session.add(
        TrackerScopeRule(
            tracker_id=moved_repo["old_tracker"].id,
            scope_type="PROJECT",
            rule_type="INCLUDE",
            identifier=REPO_ID,
        )
    )
    db_session.flush()
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=False
    )
    assert response.status_code == 200, response.text
    not_carried = response.json()["not_carried_over"]
    assert any("scope rule" in item for item in not_carried)
    new_rules = (
        db_session.query(TrackerScopeRule)
        .filter(TrackerScopeRule.tracker_id == moved_repo["new_tracker"].id)
        .filter(TrackerScopeRule.scope_type == "PROJECT")
        .count()
    )
    assert new_rules == 0


def test_transfer_refused_when_destination_cannot_see_repo(
    raw_client, db_session, moved_repo, destination_sees
):
    destination_sees["2002"] = [_repo("new-owner/other", repo_id="1")]
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=False
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "destination_cannot_see_repository"
    db_session.refresh(moved_repo["project"])
    assert str(moved_repo["project"].organization_id) == str(moved_repo["old_org"].id)


def test_transfer_refused_when_destination_org_out_of_scope(
    raw_client, db_session, moved_repo, destination_sees
):
    destination_sees["2002"] = [_repo("new-owner/widget")]
    db_session.query(TrackerScopeRule).filter(
        TrackerScopeRule.tracker_id == moved_repo["new_tracker"].id
    ).delete()
    db_session.flush()
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=False
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "destination_out_of_scope"


def test_transfer_refused_when_destination_already_has_the_repo(
    raw_client, db_session, moved_repo, destination_sees
):
    """A duplicate (for example from the old 500) is named, not merged."""
    destination_sees["2002"] = [_repo("new-owner/widget")]
    dup = crud_project.create(
        db_session,
        obj_in={
            "id": str(uuid.uuid4()),
            "name": "widget",
            "identifier": REPO_ID,
            "organization_id": moved_repo["new_org"].id,
        },
    )
    db_session.flush()
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=False
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "destination_has_duplicate"
    assert detail["duplicate_project_id"] == str(dup.id)
    assert detail["duplicate_issue_count"] == 0


def test_transfer_requires_destination_org_in_callers_account(
    raw_client, db_session, moved_repo, destination_sees
):
    """An org owned by another account is not found, never written to."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "other", "is_active": True}
    )
    foreign_tracker = _github_tracker(db_session, other.id, "foreign")
    foreign_org = crud_organization.create(
        db_session,
        obj_in={"name": "f", "identifier": "2002", "tracker_id": foreign_tracker.id},
    )
    _include_org(db_session, foreign_tracker, "2002")
    destination_sees["2002"] = [_repo("new-owner/widget")]
    response = _transfer(raw_client, moved_repo["project"], foreign_org, dry_run=False)
    assert response.status_code == 404, response.text
    db_session.refresh(moved_repo["project"])
    assert str(moved_repo["project"].organization_id) == str(moved_repo["old_org"].id)


def test_transfer_requires_manage_trackers(
    app, db_session, moved_repo, destination_sees, test_user
):
    """A same-account user without manage_trackers gets 403 and changes nothing."""
    from preloop.api.auth import get_current_active_user
    from preloop.models.crud import crud_role, crud_user, crud_user_role

    viewer = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "viewer-1159@example.com",
            "username": "viewer1159",
            "full_name": "Viewer",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    viewer_role = crud_role.get_by_name(db_session, name="viewer")
    assert viewer_role is not None
    crud_user_role.create(
        db_session, obj_in={"user_id": viewer.id, "role_id": viewer_role.id}
    )
    db_session.flush()
    viewer_id = viewer.id
    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        db_session, id=viewer_id
    )
    destination_sees["2002"] = [_repo("new-owner/widget")]
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _transfer(
            client, moved_repo["project"], moved_repo["new_org"], dry_run=False
        )
    assert response.status_code == 403, response.text
    db_session.refresh(moved_repo["project"])
    assert str(moved_repo["project"].organization_id) == str(moved_repo["old_org"].id)


def test_create_project_in_another_accounts_org_is_404(
    raw_client, db_session, test_user
):
    """The destination organization must belong to the caller's account."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "other", "is_active": True}
    )
    foreign_tracker = _github_tracker(db_session, other.id, "foreign")
    foreign_org = crud_organization.create(
        db_session,
        obj_in={"name": "f", "identifier": "77", "tracker_id": foreign_tracker.id},
    )
    db_session.flush()
    response = raw_client.post(
        "/api/v1/projects",
        json={"name": "x", "identifier": "1", "organization_id": str(foreign_org.id)},
    )
    assert response.status_code == 404, response.text
    assert (
        db_session.query(Project)
        .filter(Project.organization_id == foreign_org.id)
        .count()
        == 0
    )


def test_review_publication_targets_new_owner_after_transfer(
    raw_client, db_session, moved_repo, destination_sees
):
    """PR triggers resolve to the same project; publication uses the new repo.

    Review publication builds ``https://github.com/{project.slug}.git`` with
    the credentials of ``project.organization.tracker``
    (services/isolated_publication.py), and a PR webhook is matched to a
    project by repository path, then repository ID.
    """
    from preloop.services.flow_orchestrator import _project_identity_rank

    destination_sees["2002"] = [_repo("new-owner/widget")]
    project = moved_repo["project"]
    pr_from_new_owner = {
        "source": "github",
        "path": "new-owner/widget",
        "external_id": REPO_ID,
    }
    # Before: only the ID matches, and the old owner's integration would publish.
    assert _project_identity_rank(project, "github", pr_from_new_owner) == 2

    response = _transfer(raw_client, project, moved_repo["new_org"], dry_run=False)
    assert response.status_code == 200, response.text

    db_session.expire_all()
    project = crud_project.get(db_session, id=str(project.id))
    assert _project_identity_rank(project, "github", pr_from_new_owner) == 3
    assert str(project.organization.tracker_id) == str(moved_repo["new_tracker"].id)
    assert f"https://github.com/{project.slug}.git" == (
        "https://github.com/new-owner/widget.git"
    )


def test_rename_in_same_org_updates_publication_target(
    raw_client, db_session, moved_repo, destination_sees
):
    """A rename keeps the org; the same endpoint refreshes owner/name."""
    destination_sees["1001"] = [_repo("old-owner/widget-renamed")]
    project = moved_repo["project"]
    response = _transfer(raw_client, project, moved_repo["old_org"], dry_run=False)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "updated"
    db_session.expire_all()
    project = crud_project.get(db_session, id=str(project.id))
    assert project.slug == "old-owner/widget-renamed"
    assert str(project.organization_id) == str(moved_repo["old_org"].id)
    assert "repository_transfers" not in project.meta_data


# --- Webhook routing ------------------------------------------------------


def test_webhook_project_lookup_stays_in_the_trackers_account(
    db_session, moved_repo, test_user
):
    """Another account tracking the same repository never receives its events."""
    from preloop.api.endpoints.webhooks import _resolve_webhook_project
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "other", "is_active": True}
    )
    foreign_tracker = _github_tracker(db_session, other.id, "foreign")
    foreign_org = crud_organization.create(
        db_session,
        obj_in={"name": "f", "identifier": "2002", "tracker_id": foreign_tracker.id},
    )
    foreign = crud_project.create(
        db_session,
        obj_in={
            "id": str(uuid.uuid4()),
            "name": "widget",
            "identifier": REPO_ID,
            "organization_id": foreign_org.id,
        },
    )
    db_session.flush()

    ours = _resolve_webhook_project(
        db_session,
        identifier=REPO_ID,
        organization_id=moved_repo["new_org"].id,
        account_id=test_user.account_id,
    )
    theirs = _resolve_webhook_project(
        db_session,
        identifier=REPO_ID,
        organization_id=foreign_org.id,
        account_id=other.id,
    )
    assert ours.id == moved_repo["project"].id
    assert theirs.id == foreign.id


def test_webhook_project_lookup_prefers_the_delivering_org(
    db_session, moved_repo, test_user
):
    """With a leftover duplicate, the webhook's own organization wins."""
    from preloop.api.endpoints.webhooks import _resolve_webhook_project

    dup = crud_project.create(
        db_session,
        obj_in={
            "id": str(uuid.uuid4()),
            "name": "widget",
            "identifier": REPO_ID,
            "organization_id": moved_repo["new_org"].id,
        },
    )
    db_session.flush()
    for org, expected in (
        (moved_repo["new_org"], dup.id),
        (moved_repo["old_org"], moved_repo["project"].id),
    ):
        found = _resolve_webhook_project(
            db_session,
            identifier=REPO_ID,
            organization_id=org.id,
            account_id=test_user.account_id,
        )
        assert found.id == expected


def test_destination_check_uses_destination_credentials(
    raw_client, db_session, moved_repo, monkeypatch
):
    """The live check runs through the destination tracker's client.

    Exercises the real sync-handler-to-event-loop bridge; only the GitHub
    HTTP client is faked.
    """
    import preloop.sync.scanner.core as scanner_core

    seen = []

    class FakeGitHub:
        def __init__(self, tracker):
            self.tracker = tracker

        async def get_projects(self, organization_identifier):
            seen.append((str(self.tracker.id), organization_identifier))
            return [_repo("new-owner/widget")]

    class FakeTrackerClient:
        def __init__(self, tracker, **_):
            self.client = FakeGitHub(tracker)

    monkeypatch.setattr(scanner_core, "TrackerClient", FakeTrackerClient)
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=True
    )
    assert response.status_code == 200, response.text
    assert response.json()["destination"]["full_name"] == "new-owner/widget"
    assert seen == [(str(moved_repo["new_tracker"].id), "2002")]


def test_destination_check_failure_is_502_not_500(
    raw_client, db_session, moved_repo, monkeypatch
):
    def boom(tracker, organization):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(projects_endpoint, "_list_destination_repositories", boom)
    response = _transfer(
        raw_client, moved_repo["project"], moved_repo["new_org"], dry_run=False
    )
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "destination_check_failed"
