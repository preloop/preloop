"""Issue references resolve against real synced rows (PostgreSQL)."""

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from preloop.api.endpoints.mcp import (
    IssueAmbiguousError,
    IssueNotFoundError,
    _find_issue_by_identifier,
)
from preloop.models.crud import crud_issue

HOST = "https://gitlab.example.com"


@pytest.fixture
def synced(
    create_account, create_tracker, create_organization, create_project, create_issue
):
    """One account with GitLab and GitHub issues as sync stores them."""
    account = create_account()
    gitlab = create_tracker(account=account, tracker_type="gitlab")
    github = create_tracker(account=account, tracker_type="github")
    gl_org = create_organization(tracker=gitlab)
    gh_org = create_organization(tracker=github)
    proj = create_project(identifier="1", slug="grp/sub/proj", organization=gl_org)
    other = create_project(identifier="2", slug="grp/other", organization=gl_org)
    repo = create_project(identifier="3", slug="org/repo", organization=gh_org)
    rows = {
        "gl12": create_issue(
            tracker=gitlab,
            project=proj,
            key="grp/sub/proj#12",
            external_url="",  # rows synced before web_url was stored
        ),
        "gl112": create_issue(
            tracker=gitlab,
            project=proj,
            key="grp/sub/proj#112",
            external_url=f"{HOST}/grp/sub/proj/-/work_items/112",
        ),
        "gl7": create_issue(tracker=gitlab, project=proj, key="grp/sub/proj#7"),
        "other7": create_issue(tracker=gitlab, project=other, key="grp/other#7"),
        "gh5": create_issue(
            tracker=github,
            project=repo,
            key="org/repo#5",
            external_url="https://github.com/org/repo/issues/5",
        ),
    }
    return str(account.id), rows


@pytest.mark.parametrize(
    "ref",
    [
        f"{HOST}/grp/sub/proj/-/issues/12",
        f"{HOST}/grp/sub/proj/-/work_items/12",
        f"{HOST}/grp/sub/proj/-/work_items/12?show=1#note_4",
        "grp/sub/proj#12",
        "12",
        "#12",
    ],
)
def test_each_form_finds_the_same_issue(db_session, synced, ref) -> None:
    account_id, rows = synced
    found = _find_issue_by_identifier(db_session, ref, account_id)
    assert found.id == rows["gl12"].id


@pytest.mark.parametrize("word", ["issues", "work_items"])
def test_stored_url_in_either_form_is_found_by_the_other(
    db_session, synced, word
) -> None:
    account_id, rows = synced
    found = _find_issue_by_identifier(
        db_session, f"{HOST}/grp/sub/proj/-/{word}/112", account_id
    )
    assert found.id == rows["gl112"].id


def test_bare_number_matches_exactly_not_as_suffix(db_session, synced) -> None:
    account_id, rows = synced
    assert _find_issue_by_identifier(db_session, "112", account_id).id == (
        rows["gl112"].id
    )
    matches = crud_issue.list_by_issue_number(
        db_session, number="12", account_id=account_id
    )
    assert [m.key for m in matches] == ["grp/sub/proj#12"]


def test_bare_number_in_two_projects_is_ambiguous(db_session, synced) -> None:
    account_id, _ = synced
    with pytest.raises(IssueAmbiguousError) as err:
        _find_issue_by_identifier(db_session, "7", account_id)
    assert err.value.candidates == ["grp/other#7", "grp/sub/proj#7"]
    assert "grp/other#7, grp/sub/proj#7" in str(err.value)


def test_unknown_bare_number_suggests_key_form(db_session, synced) -> None:
    account_id, _ = synced
    with pytest.raises(IssueNotFoundError, match="group/project#<iid>"):
        _find_issue_by_identifier(db_session, "999", account_id)


def test_unsynced_work_item_says_what_was_looked_up(db_session, synced) -> None:
    account_id, _ = synced
    url = f"{HOST}/grp/sub/proj/-/work_items/40"
    with pytest.raises(IssueNotFoundError) as err:
        _find_issue_by_identifier(db_session, url, account_id)
    message = str(err.value)
    assert url in message
    assert "grp/sub/proj#40" in message
    assert "group/project#<iid>" in message


def test_github_url_and_key_unchanged(db_session, synced) -> None:
    account_id, rows = synced
    for ref in ("https://github.com/org/repo/issues/5", "org/repo#5"):
        assert _find_issue_by_identifier(db_session, ref, account_id).id == (
            rows["gh5"].id
        )


def test_other_account_cannot_resolve(db_session, synced, create_account) -> None:
    other = create_account()
    with pytest.raises(IssueNotFoundError):
        _find_issue_by_identifier(db_session, "grp/sub/proj#12", str(other.id))


@pytest.mark.parametrize(
    ("stored", "url"),
    [
        ("acme/team/api#12", f"{HOST}/team/api/-/issues/12"),
        ("acme/team/api#12", f"{HOST}/team/api/-/work_items/12"),
        ("acme/team/api#12", "https://github.com/team/api/issues/12"),
        ("XPROJ-12", "https://jira.example.com/browse/PROJ-12"),
    ],
)
def test_url_never_resolves_by_key_suffix(
    db_session, create_account, create_tracker, create_issue, stored, url
) -> None:
    account = create_account()
    tracker = create_tracker(account=account, tracker_type="gitlab")
    create_issue(tracker=tracker, key=stored)
    with pytest.raises(IssueNotFoundError, match="looked up synced issue"):
        _find_issue_by_identifier(db_session, url, str(account.id))


def test_bare_number_ignores_projects_of_other_accounts(
    db_session,
    synced,
    create_account,
    create_tracker,
    create_organization,
    create_project,
    create_issue,
) -> None:
    account_id, rows = synced
    stranger = create_tracker(account=create_account(), tracker_type="gitlab")
    project = create_project(
        identifier="9",
        slug="grp/hidden",
        organization=create_organization(tracker=stranger),
    )
    create_issue(tracker=stranger, project=project, key="grp/hidden#5")
    matches = crud_issue.list_by_issue_number(
        db_session, number="5", account_id=account_id
    )
    assert [m.id for m in matches] == [rows["gh5"].id]


def test_jira_project_key_uses_a_dash(
    db_session,
    create_account,
    create_tracker,
    create_organization,
    create_project,
    create_issue,
) -> None:
    account = create_account()
    jira = create_tracker(account=account, tracker_type="jira")
    project = create_project(
        identifier="10000", slug="PROJ", organization=create_organization(tracker=jira)
    )
    issue = create_issue(tracker=jira, project=project, key="PROJ-12")
    create_issue(tracker=jira, project=project, key="PROJ-112")
    found = _find_issue_by_identifier(db_session, "12", str(account.id))
    assert found.id == issue.id


def test_bare_number_scans_projects_in_batches(
    db_session,
    create_account,
    create_tracker,
    create_organization,
    create_project,
    create_issue,
    monkeypatch,
) -> None:
    account = create_account()
    tracker = create_tracker(account=account, tracker_type="gitlab")
    org = create_organization(tracker=tracker)
    projects = [
        create_project(identifier=str(i), slug=f"grp/p{i:02d}", organization=org)
        for i in range(7)
    ]
    first = create_issue(tracker=tracker, project=projects[0], key="grp/p00#3")
    last = create_issue(tracker=tracker, project=projects[6], key="grp/p06#3")
    monkeypatch.setattr(type(crud_issue), "ISSUE_NUMBER_BATCH", 2)
    with pytest.raises(IssueAmbiguousError) as err:
        _find_issue_by_identifier(db_session, "3", str(account.id))
    assert err.value.candidates == [first.key, last.key]
    assert crud_issue.list_by_issue_number(
        db_session, number="3", account_id=str(account.id), limit=1
    ) == [first]


def test_project_without_slug_still_matches(
    db_session,
    create_account,
    create_tracker,
    create_organization,
    create_project,
    create_issue,
) -> None:
    account = create_account()
    tracker = create_tracker(account=account, tracker_type="gitlab")
    project = create_project(
        identifier="1", slug=None, organization=create_organization(tracker=tracker)
    )
    issue = create_issue(tracker=tracker, project=project, key="grp/legacy#12")
    create_issue(tracker=tracker, project=project, key="grp/legacy#112")
    assert crud_issue.list_by_issue_number(
        db_session, number="12", account_id=str(account.id)
    ) == [issue]


def test_bare_number_query_uses_the_key_index(db_session, synced) -> None:
    """The candidate-key read is an equality on ``issue.key``, which the key
    index serves (a leading-wildcard LIKE could not)."""
    account_id, _ = synced
    query = crud_issue.issue_number_key_query(
        db_session, keys=["grp/sub/proj#12", "grp/other#12"], account_id=account_id
    )
    sql = str(
        query.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "LIKE" not in sql.upper()
    # On a near-empty table PostgreSQL costs every issue index about the same,
    # so leave ix_issue_key as the only non-constraint issue index (as
    # test_session_keys_index does). The DROPs roll back with the test.
    competing = db_session.execute(
        text(
            "SELECT i.indexname FROM pg_indexes i "
            "WHERE i.tablename = 'issue' AND i.indexname <> 'ix_issue_key' "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c "
            "WHERE c.conindid = to_regclass(quote_ident(i.schemaname) "
            "|| '.' || quote_ident(i.indexname)))"
        )
    ).scalars()
    for index_name in list(competing):
        db_session.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(row[0] for row in db_session.execute(text(f"EXPLAIN {sql}")))
    assert "ix_issue_key" in plan, plan


def test_transferred_project_keeps_its_issue_keys(
    db_session, create_account, create_tracker, create_organization,
    create_project, create_issue,
) -> None:
    """A transfer changes the slug; synced keys keep the former name."""
    account = create_account()
    tracker = create_tracker(account=account, tracker_type="github")
    org = create_organization(tracker=tracker)
    project = create_project(
        identifier="1",
        slug="new-owner/widget",
        organization=org,
        meta_data={
            "repository_transfers": [
                {"from_full_name": "old-owner/widget", "to_full_name": "new-owner/widget"}
            ]
        },
    )
    issue = create_issue(tracker=tracker, project=project, key="old-owner/widget#1")
    create_issue(tracker=tracker, project=project, key="new-owner/widget#2")
    found = _find_issue_by_identifier(db_session, "1", str(account.id))
    assert found.id == issue.id


def test_renamed_project_without_history_still_matches(
    db_session, create_account, create_tracker, create_organization,
    create_project, create_issue,
) -> None:
    """An upstream rename that sync has not rewritten into the keys yet."""
    account = create_account()
    tracker = create_tracker(account=account, tracker_type="gitlab")
    project = create_project(
        identifier="1",
        slug="grp/renamed",
        organization=create_organization(tracker=tracker),
    )
    issue = create_issue(tracker=tracker, project=project, key="grp/before#1")
    create_issue(tracker=tracker, project=project, key="grp/before#11")
    assert crud_issue.list_by_issue_number(
        db_session, number="1", account_id=str(account.id)
    ) == [issue]
