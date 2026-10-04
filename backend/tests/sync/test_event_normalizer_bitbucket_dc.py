"""Bitbucket Data Center payload normalization into the trigger contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from preloop.sync.event_normalizer import (
    BITBUCKET_EVENT_MAP,
    EVENT_TYPE_LABELS,
    extract_filter_fields,
    extract_trigger_subject,
    normalize_event_type,
)
from preloop.utils.bitbucket_dc import parse_instance_url
from preloop.utils.bitbucket_dc_webhooks import (
    BITBUCKET_DC_EVENT_MAP,
    BITBUCKET_DC_WEBHOOK_EVENTS,
    BitbucketDCWebhookRejectedError,
    delivery_identity,
    normalize_delivery,
)

INSTANCE = parse_instance_url(
    "https://bitbucket.example.com/bitbucket", allow_path=True
)
FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "bitbucket_dc"
        / "webhooks_10.2.json"
    ).read_text()
)["events"]


def fx(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES[key])


def normalize(key: str, payload: Any = None, **kwargs: Any):
    kwargs.setdefault("bound_repository_id", 42)
    kwargs.setdefault("bound_project_key", "PRJ")
    return normalize_delivery(
        key, fx(key) if payload is None else payload, instance=INSTANCE, **kwargs
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("repo:refs_changed", "push"),
        ("pr:opened", "pull_request_opened"),
        ("pr:from_ref_updated", "pull_request_updated"),
        ("pr:modified", "pull_request_updated"),
        ("pr:reviewer:approved", "pull_request_approved"),
        ("pr:reviewer:unapproved", "pull_request_unapproved"),
        ("pr:reviewer:needs_work", "pull_request_changes_requested"),
        ("pr:comment:added", "comment_created"),
        ("pr:comment:edited", "comment_updated"),
        ("pr:comment:deleted", "comment_deleted"),
        ("pr:merged", "pull_request_merged"),
        ("pr:declined", "pull_request_closed"),
        ("pr:deleted", "pull_request_closed"),
        ("repo:forked", "repo:forked"),
    ],
)
def test_normalize_event_type(raw: str, expected: str) -> None:
    assert normalize_event_type("bitbucket_dc", raw, {}) == expected


def test_cloud_event_names_are_not_data_center_contracts() -> None:
    assert not set(BITBUCKET_EVENT_MAP) & set(BITBUCKET_DC_EVENT_MAP)
    assert normalize_event_type("bitbucket_dc", "pullrequest:created", {}) == (
        "pullrequest:created"
    )
    # Cloud mapping is unchanged by the Data Center map.
    assert normalize_event_type("bitbucket", "pullrequest:created", {}) == (
        "pull_request_opened"
    )


def test_every_subscribed_event_has_a_label() -> None:
    for raw in BITBUCKET_DC_WEBHOOK_EVENTS:
        assert BITBUCKET_DC_EVENT_MAP[raw] in EVENT_TYPE_LABELS


def test_pull_request_identity_is_mapped_explicitly() -> None:
    result = normalize("pr:from_ref_updated")
    assert result.accepted
    pr = result.payload["pull_request"]
    dc = result.payload["bitbucket_dc"]
    assert pr["number"] == 101
    assert pr["head"]["sha"] == "04c7c5c931b9418ca7b66f51fe934d0bd9b2ba4b"
    assert pr["head"]["full_ref"] == "refs/heads/feature/retry"
    assert pr["base"]["full_ref"] == "refs/heads/main"
    assert pr["html_url"] == (
        "https://bitbucket.example.com/bitbucket/projects/PRJ/repos/my-repo"
        "/pull-requests/101"
    )
    assert dc["instance"] == "https://bitbucket.example.com/bitbucket"
    assert dc["project_key"] == "PRJ"
    assert dc["repository_id"] == 42
    assert dc["actor"] == "jdoe"
    assert dc["head_changed"] is True
    assert dc["previous_head_sha"] == "9c0e5e2d4b8f4a1c3e6d7f8a9b0c1d2e3f4a5b6c"
    assert "emailAddress" not in json.dumps(result.payload)


def test_modified_event_is_an_update_without_a_new_head() -> None:
    result = normalize("pr:modified")
    assert result.accepted
    assert result.payload["bitbucket_dc"]["head_changed"] is False


def test_comment_ids_and_thread_are_mapped() -> None:
    result = normalize("pr:comment:added")
    comment = result.payload["comment"]
    assert comment["id"] == 502
    assert comment["in_reply_to_id"] == 501
    assert comment["body"] == "Please add a test for the retry path."
    assert result.payload["bitbucket_dc"]["comment_id"] == 502
    assert result.payload["bitbucket_dc"]["parent_comment_id"] == 501


def test_reviewer_verdict_is_mapped() -> None:
    result = normalize("pr:reviewer:needs_work")
    assert result.payload["bitbucket_dc"]["participant"] == "rev"
    assert result.payload["bitbucket_dc"]["participant_status"] == "NEEDS_WORK"
    fields = extract_filter_fields(
        "bitbucket_dc", "pr:reviewer:needs_work", result.payload
    )
    assert fields["review_state"] == "NEEDS_WORK"
    assert fields["sender"] == "rev"


def test_push_changes_are_mapped() -> None:
    result = normalize("repo:refs_changed")
    assert result.payload["after"] == "6df3858eeb9a53a911cd17e66a9174d44ffb02cd"
    assert result.payload["push"]["changes"][0]["branch"] == "main"
    fields = extract_filter_fields("bitbucket_dc", "repo:refs_changed", result.payload)
    assert fields["ref"] == "main"


def test_self_generated_actor_is_flagged() -> None:
    payload = fx("pr:comment:added")
    payload["actor"]["slug"] = "Preloop-Bot"
    result = normalize("pr:comment:added", payload, self_user_slug="preloop-bot")
    assert result.payload["bitbucket_dc"]["self_generated"] is True
    assert (
        normalize("pr:comment:added", self_user_slug="preloop-bot").payload[
            "bitbucket_dc"
        ]["self_generated"]
        is False
    )


def test_filter_fields_and_subject() -> None:
    result = normalize("pr:opened")
    fields = extract_filter_fields("bitbucket_dc", "pr:opened", result.payload)
    assert fields["author"] == "jdoe"
    assert fields["reviewer"] == ["rev"]
    assert fields["source_branch"] == "feature/retry"
    assert fields["target_branch"] == "main"
    assert fields["state"] == "open"
    assert fields["draft"] is False
    subject = extract_trigger_subject(
        {
            "source": "bitbucket_dc",
            "type": "pull_request_opened",
            "payload": result.payload,
        }
    )
    assert subject["text"] == "PRJ/my-repo #101 · Pull Request Opened · 04c7c5c9"


def test_unbound_tracker_requires_a_known_repository() -> None:
    with pytest.raises(BitbucketDCWebhookRejectedError):
        normalize("pr:opened", bound_repository_id=None, bound_project_key=None)
    ok = normalize(
        "pr:opened",
        bound_repository_id=None,
        bound_project_key=None,
        repository_known=lambda repo_id: repo_id == 42,
    )
    assert ok.accepted


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("pullRequest"),
        lambda p: p["pullRequest"].update(id="101"),
        lambda p: p["pullRequest"]["toRef"]["repository"].update(id=None),
    ],
)
def test_malformed_payloads_are_rejected(mutate) -> None:
    payload = fx("pr:opened")
    mutate(payload)
    with pytest.raises(BitbucketDCWebhookRejectedError) as exc:
        normalize("pr:opened", payload)
    assert exc.value.status_code == 400


def test_delivery_identity_is_tracker_scoped() -> None:
    result = normalize("pr:opened")
    a = delivery_identity("t-1", "req", "pr:opened", result.payload, b"{}")
    b = delivery_identity("t-2", "req", "pr:opened", result.payload, b"{}")
    assert a != b
    assert a == "bitbucket_dc:t-1:req"


def test_over_long_request_id_falls_back_to_a_bounded_identity() -> None:
    result = normalize("pr:opened")
    tracker = "11111111-2222-3333-4444-555555555555"
    kept = delivery_identity(tracker, "r" * 140, "pr:opened", result.payload, b"{}")
    assert kept.endswith("r" * 140)
    assert len("delivery:" + kept) <= 200
    long = delivery_identity(tracker, "r" * 141, "pr:opened", result.payload, b"{}")
    assert ":derived:" in long
    assert len("delivery:" + long) <= 200
