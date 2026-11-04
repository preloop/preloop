"""Review publication gate decisions (EE #202)."""

from types import SimpleNamespace

import pytest

from preloop.services.review_publication import (
    REASON_NO_RECEIPT,
    REASON_OTHER_HEAD,
    REASON_OTHER_TARGET,
    STATUS_ALREADY_REVIEWED,
    STATUS_NOT_PUBLISHED,
    STATUS_PUBLISHED,
    evaluate_review_publication,
    parse_pull_request_url,
    review_publication_required,
    trigger_pull_request,
)

REPO = "example-org/example-repo"
HEAD = "c" * 40


def _receipt(**overrides):
    values = dict(
        id="r1",
        repository=REPO,
        pull_request="7",
        head_sha=HEAD,
        review_action="approve",
        provider="github",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _evaluate(receipts, artifact=None, prior=None, **target):
    kwargs = dict(repository=REPO, pull_request="7", head_sha=HEAD)
    kwargs.update(target)
    return evaluate_review_publication(
        receipts,
        artifact=artifact or {},
        find_prior_receipt=lambda *_a: prior,
        **kwargs,
    )


def test_matching_receipt_is_published() -> None:
    assert _evaluate([_receipt()])["status"] == STATUS_PUBLISHED


def test_receipt_without_provider_head_is_accepted() -> None:
    # GitLab approvals do not report the commit they approved.
    assert _evaluate([_receipt(head_sha=None)])["status"] == STATUS_PUBLISHED


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"pull_request": "8"}, REASON_OTHER_TARGET),
        ({"repository": "example-org/other"}, REASON_OTHER_TARGET),
        ({"head_sha": "d" * 40}, REASON_OTHER_HEAD),
    ],
)
def test_mismatched_receipt_is_not_published(overrides, reason) -> None:
    outcome = _evaluate([_receipt(**overrides)])
    assert outcome == {"status": STATUS_NOT_PUBLISHED, "reason": reason}


@pytest.mark.parametrize(
    "artifact",
    [
        {"status": "success", "review_action": "approve", "review_posted": False},
        {
            "status": "success",
            "review_action": "approve",
            "review_posted_to_github": False,
            "blocker": "GitHub App 401",
        },
        {"status": "success", "review_posted": True},
        {"status": "success", "review_posted": True, "review_posted_to_github": False},
        {"status": "success", "review_action": "approve", "receipt": {"id": "fake"}},
        "not a mapping",
        {},
    ],
)
def test_no_receipt_fails_whatever_the_agent_reports(artifact) -> None:
    outcome = _evaluate([], artifact=artifact, prior=_receipt(id="old"))
    assert outcome == {"status": STATUS_NOT_PUBLISHED, "reason": REASON_NO_RECEIPT}


def test_deliberate_skip_passes_on_an_already_reviewed_head() -> None:
    skip = {"status": "success", "review_posted": False, "review_action": None}
    outcome = _evaluate([], artifact=skip, prior=_receipt(id="earlier"))
    assert outcome["status"] == STATUS_ALREADY_REVIEWED
    assert outcome["receipt_id"] == "earlier"


def test_deliberate_skip_without_prior_receipt_fails() -> None:
    skip = {"status": "success", "review_posted": False, "review_action": None}
    assert _evaluate([], artifact=skip)["status"] == STATUS_NOT_PUBLISHED


def test_auth_blocked_skip_is_not_a_permitted_skip() -> None:
    blocked = {
        "status": "success",
        "review_posted": False,
        "review_action": None,
        "blocker": "401 from the code host",
    }
    outcome = _evaluate([], artifact=blocked, prior=_receipt(id="earlier"))
    assert outcome["status"] == STATUS_NOT_PUBLISHED


def test_skip_needs_a_known_head() -> None:
    skip = {"status": "success", "review_posted": False, "review_action": None}
    outcome = _evaluate([], artifact=skip, prior=_receipt(), head_sha=None)
    assert outcome["status"] == STATUS_NOT_PUBLISHED


@pytest.mark.parametrize("receipt_head", [HEAD, None])
def test_named_pull_request_without_trigger_head_fails_closed(receipt_head) -> None:
    outcome = _evaluate([_receipt(head_sha=receipt_head)], head_sha=None)
    assert outcome == {
        "status": STATUS_NOT_PUBLISHED,
        "reason": "trigger_head_missing",
    }


def test_unknown_target_accepts_any_receipt_of_the_run() -> None:
    outcome = _evaluate([_receipt()], repository=None, pull_request=None)
    assert outcome["status"] == STATUS_PUBLISHED


@pytest.mark.parametrize(
    ("own", "preset", "required"),
    [
        ({"review_publication": {"required": True}}, None, True),
        (
            {"review_publication": {"required": False}},
            {"review_publication": {"required": True}},
            False,
        ),
        ({"enabled": True}, {"review_publication": {"required": True}}, True),
        (None, {"review_publication": {"required": True}}, True),
        ({"enabled": True}, {"enabled": True}, False),
        (None, None, False),
        ("garbage", None, False),
    ],
)
def test_requirement_comes_from_flow_then_preset(own, preset, required) -> None:
    assert review_publication_required(own, preset) is required


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Example-Org/Example-Repo/pull/7", (REPO, "7")),
        ("https://github.com/example-org/example-repo/pull/7/files", (REPO, "7")),
        (
            "https://gitlab.example.com/group/sub/project/-/merge_requests/12",
            ("group/sub/project", "12"),
        ),
        (
            "https://bitbucket.org/example-ws/example-repo/pull-requests/3",
            ("example-ws/example-repo", "3"),
        ),
        ("https://example.com/not/a/pr", (None, None)),
        (None, (None, None)),
    ],
)
def test_pull_request_url_parsing(url, expected) -> None:
    assert parse_pull_request_url(url) == expected


def test_trigger_reads_object_attributes_then_pull_request() -> None:
    gitlab = {
        "payload": {
            "object_attributes": {
                "url": "https://gitlab.example.com/g/p/-/merge_requests/4"
            }
        }
    }
    github = {
        "payload": {
            "pull_request": {
                "html_url": "https://github.com/example-org/example-repo/pull/7"
            }
        }
    }
    assert trigger_pull_request(gitlab) == ("g/p", "4")
    assert trigger_pull_request(github) == (REPO, "7")
    assert trigger_pull_request({"payload": "x"}) == (None, None)


def test_trigger_reads_raw_bitbucket_events() -> None:
    linked = {
        "payload": {
            "repository": {"full_name": "Example-WS/Example-Repo"},
            "pullrequest": {
                "id": 7,
                "links": {
                    "html": {
                        "href": "https://bitbucket.org/example-ws/example-repo/pull-requests/7"
                    }
                },
            },
        }
    }
    bare = {
        "payload": {
            "repository": {"full_name": "ws/repo"},
            "pullrequest": {"id": 7, "source": {"commit": {"hash": "head"}}},
        }
    }
    assert trigger_pull_request(linked) == ("example-ws/example-repo", "7")
    assert trigger_pull_request(bare) == ("ws/repo", "7")


def test_bitbucket_trigger_rejects_a_receipt_for_another_pull_request() -> None:
    target = trigger_pull_request(
        {"payload": {"repository": {"full_name": REPO}, "pullrequest": {"id": 7}}}
    )
    outcome = _evaluate(
        [_receipt(pull_request="8", head_sha=None)],
        repository=target[0],
        pull_request=target[1],
    )
    assert outcome["status"] == STATUS_NOT_PUBLISHED
