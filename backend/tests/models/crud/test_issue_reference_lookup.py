"""Issue references resolve against real synced rows (PostgreSQL)."""

import pytest

from preloop.api.endpoints.mcp import (
    IssueAmbiguousError,
    IssueNotFoundError,
    _find_issue_by_identifier,
)
from preloop.models.crud import crud_issue

HOST = "https://gitlab.example.com"


@pytest.fixture
def synced(create_account, create_tracker, create_issue):
    """One account with GitLab and GitHub issues as sync stores them."""
    account = create_account()
    gitlab = create_tracker(account=account, tracker_type="gitlab")
    github = create_tracker(account=account, tracker_type="github")
    rows = {
        "gl12": create_issue(
            tracker=gitlab,
            key="grp/sub/proj#12",
            external_url="",  # rows synced before web_url was stored
        ),
        "gl112": create_issue(
            tracker=gitlab,
            key="grp/sub/proj#112",
            external_url=f"{HOST}/grp/sub/proj/-/work_items/112",
        ),
        "gl7": create_issue(tracker=gitlab, key="grp/sub/proj#7"),
        "other7": create_issue(tracker=gitlab, key="grp/other#7"),
        "gh5": create_issue(
            tracker=github,
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
