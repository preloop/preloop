"""Pull and merge request references resolve against real synced rows."""

import uuid
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.mcp import (
    PR_REFERENCE_FORMS,
    _parse_pr_identifier,
    _pull_request_client,
    _resolve_pull_request_target,
    _trigger_project_from_event,
)
from preloop.models.models.tracker_scope_rule import TrackerScopeRule

HOST = "https://gitlab.example.com"
MR = "grp/sub/proj"


def _include(db_session: Any, tracker: Any, identifier: str) -> None:
    db_session.add(
        TrackerScopeRule(
            id=uuid.uuid4(),
            tracker_id=tracker.id,
            scope_type="ORGANIZATION",
            rule_type="INCLUDE",
            identifier=identifier,
        )
    )
    db_session.flush()


@pytest.fixture
def synced(
    db_session: Any,
    create_account: Any,
    create_tracker: Any,
    create_organization: Any,
    create_project: Any,
) -> Dict[str, Any]:
    """GitHub and GitLab trackers, with a GitLab org outside scope created first.

    The out-of-scope org is the one the old first-project fallback picked.
    """
    account = create_account()
    github = create_tracker(
        account=account, tracker_type="github", url="https://github.com"
    )
    gitlab = create_tracker(account=account, tracker_type="gitlab", url=HOST)
    gh_org = create_organization(tracker=github, identifier="org")
    outside = create_organization(tracker=gitlab, identifier="outside")
    grp = create_organization(tracker=gitlab, identifier="grp")
    rows = {
        "outside": create_project(
            identifier="9", slug="outside/proj", organization=outside
        ),
        "mr": create_project(identifier="1", slug=MR, organization=grp),
        "repo": create_project(identifier="3", slug="org/repo", organization=gh_org),
    }
    _include(db_session, github, "org")
    _include(db_session, gitlab, "grp")
    return {
        "user": SimpleNamespace(account_id=account.id),
        "rows": rows,
        "gitlab_org": grp,
        "create_project": create_project,
    }


@pytest.mark.parametrize(
    "reference",
    [
        f"{HOST}/{MR}/-/merge_requests/350",
        f"{HOST}/{MR}/-/merge_requests/350/diffs",
        f"{HOST}/{MR}/-/merge_requests/350/diffs?commit_id=abc#note_9",
        f"{HOST}/{MR}/-/merge_requests/350#note_9",
        f"{HOST}/{MR}/merge_requests/350",
        f"http://gitlab.example.com/{MR}/-/merge_requests/350",
        f"https://gitlab.example.com:8443/{MR}/-/merge_requests/350",
        f"https://tunnel.example.net/{MR}/-/merge_requests/350",
        f"https://example.com/gitlab/{MR}/-/merge_requests/350",
        f"{MR}!350",
        "GRP/Sub/proj!350",
        f"{MR}#350",
        f" {MR}!350 ",
    ],
)
def test_gitlab_forms_resolve_the_project(
    db_session: Any, synced: Dict[str, Any], reference: str
) -> None:
    project, info = _resolve_pull_request_target(db_session, synced["user"], reference)
    assert project.id == synced["rows"]["mr"].id
    assert info["pr_number"] == "350"


@pytest.mark.parametrize(
    "reference",
    [
        "https://github.com/org/repo/pull/5",
        "https://github.com/org/repo/pull/5/files",
        "https://github.com/org/repo/pull/5#discussion_r1",
        "org/repo#5",
    ],
)
def test_github_forms_are_unchanged(
    db_session: Any, synced: Dict[str, Any], reference: str
) -> None:
    project, info = _resolve_pull_request_target(db_session, synced["user"], reference)
    assert project.id == synced["rows"]["repo"].id
    assert info["pr_number"] == "5"
    assert info["platform"] in ("github", None)


@pytest.mark.parametrize("reference", ["350", "!350", "#350", 350])
def test_bare_number_uses_the_trigger_project(
    db_session: Any, synced: Dict[str, Any], reference: Any
) -> None:
    project, info = _resolve_pull_request_target(
        db_session,
        synced["user"],
        reference,
        trigger_project_id=str(synced["rows"]["mr"].id),
    )
    assert project.id == synced["rows"]["mr"].id
    assert info["pr_number"] == "350"


def test_bare_number_with_several_projects_lists_candidates(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    with pytest.raises(HTTPException) as exc:
        _resolve_pull_request_target(db_session, synced["user"], "350")
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "'350'" in detail
    assert "org/repo#350" in detail
    assert f"{MR}!350" in detail
    # Out-of-scope projects are never candidates.
    assert "outside/proj" not in detail


def test_gitlab_sigil_narrows_a_bare_number_to_gitlab(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    project, info = _resolve_pull_request_target(db_session, synced["user"], "!350")
    assert project.id == synced["rows"]["mr"].id
    assert info["platform"] == "gitlab"


def test_bare_number_with_one_project_in_scope_resolves_it(
    db_session: Any,
    create_account: Any,
    create_tracker: Any,
    create_organization: Any,
    create_project: Any,
) -> None:
    account = create_account()
    gitlab = create_tracker(account=account, tracker_type="gitlab", url=HOST)
    outside = create_organization(tracker=gitlab, identifier="outside")
    grp = create_organization(tracker=gitlab, identifier="grp")
    create_project(identifier="9", slug="outside/proj", organization=outside)
    only = create_project(identifier="1", slug=MR, organization=grp)
    _include(db_session, gitlab, "grp")
    user = SimpleNamespace(account_id=account.id)
    project, info = _resolve_pull_request_target(db_session, user, "350")
    assert project.id == only.id
    assert info["pr_number"] == "350"


def test_project_argument_names_the_project_for_a_bare_number(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    project, info = _resolve_pull_request_target(
        db_session, synced["user"], 350, project=MR
    )
    assert project.id == synced["rows"]["mr"].id
    assert info["pr_number"] == "350"


@pytest.mark.asyncio
async def test_out_of_scope_project_says_so(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    project, _ = _resolve_pull_request_target(
        db_session, synced["user"], "outside/proj!1"
    )
    assert project.id == synced["rows"]["outside"].id
    user = SimpleNamespace(account_id=synced["user"].account_id, username="u")
    with pytest.raises(HTTPException) as exc:
        await _pull_request_client(db_session, user, project, "outside/proj!1")
    assert exc.value.status_code == 403
    assert "outside/proj" in exc.value.detail
    assert "Organization is not included in tracker scope" in exc.value.detail


@pytest.mark.parametrize(
    "reference",
    [
        "grp/missing!350",
        f"{HOST}/grp/missing/-/merge_requests/350",
    ],
)
def test_unknown_project_never_falls_back(
    db_session: Any, synced: Dict[str, Any], reference: str
) -> None:
    with pytest.raises(HTTPException) as exc:
        _resolve_pull_request_target(db_session, synced["user"], reference)
    assert exc.value.status_code == 404
    assert "grp/missing" in exc.value.detail
    assert "Organization is not included" not in exc.value.detail
    assert PR_REFERENCE_FORMS in exc.value.detail


@pytest.mark.parametrize("reference", ["", "abc", "grp/proj!x", "!", "12a"])
def test_malformed_reference_lists_accepted_forms(
    db_session: Any, synced: Dict[str, Any], reference: str
) -> None:
    with pytest.raises(HTTPException) as exc:
        _resolve_pull_request_target(db_session, synced["user"], reference)
    assert exc.value.status_code == 400
    assert PR_REFERENCE_FORMS in exc.value.detail


def test_parse_gitlab_reference_with_nested_groups() -> None:
    info = _parse_pr_identifier("a/b/c/d!12")
    assert info["platform"] == "gitlab"
    assert info["project_path"] == "a/b/c/d"
    assert info["pr_number"] == "12"


def test_trigger_project_from_gitlab_event(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    event = {
        "source": "gitlab",
        "payload": {"project": {"id": 1, "path_with_namespace": MR}},
    }
    found = _trigger_project_from_event(db_session, synced["user"].account_id, event)
    assert found == str(synced["rows"]["mr"].id)


def test_trigger_project_from_explicit_project_id(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    project_id = str(synced["rows"]["repo"].id)
    found = _trigger_project_from_event(
        db_session, synced["user"].account_id, {"project_id": project_id}
    )
    assert found == project_id


def test_trigger_project_absent_for_manual_run(
    db_session: Any, synced: Dict[str, Any]
) -> None:
    assert (
        _trigger_project_from_event(db_session, synced["user"].account_id, {}) is None
    )


def test_issue_url_without_stored_page_is_rebuilt_for_gitlab(
    db_session: Any, synced: Dict[str, Any], create_issue: Any
) -> None:
    from preloop.api.endpoints.mcp import _issue_web_url

    mr = synced["rows"]["mr"]
    issue = create_issue(
        tracker=mr.organization.tracker,
        project=mr,
        key=f"{MR}#272",
        external_url="",
        meta_data={},
    )
    assert _issue_web_url(issue, "https://preloop.example.com") == (
        f"{HOST}/{MR}/-/issues/272"
    )


@pytest.mark.parametrize(
    "preloop_url", ["https://preloop.example.com", "https://preloop.example.com/"]
)
def test_issue_url_fallback_has_one_scheme(
    db_session: Any, synced: Dict[str, Any], create_issue: Any, preloop_url: str
) -> None:
    from preloop.api.endpoints.mcp import _issue_web_url

    mr = synced["rows"]["mr"]
    issue = create_issue(
        tracker=mr.organization.tracker,
        project=mr,
        key="not-a-path-key",
        external_url="",
        meta_data={},
    )
    assert _issue_web_url(issue, preloop_url) == (
        f"https://preloop.example.com/issues/{issue.id}"
    )
