"""Azure Boards mapping: HTML conversion, fields, WIQL, scope, deliveries.

Payloads are in ``tests/fixtures/azure_devops/work_items.json``; each entry
cites the Microsoft documentation page its shape follows.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytest

from preloop.schemas.tracker_models import IssueFilter
from preloop.sync.event_normalizer import (
    extract_filter_fields,
    extract_trigger_subject,
    normalize_event_type,
    secondary_event_types,
)
from preloop.utils.azure_devops_html import html_to_markdown, markdown_to_html
from preloop.utils.azure_devops_work_items import (
    WorkItemRejectedError,
    apply_state_category,
    build_wiql,
    carry_derived_details,
    normalize_work_item_delivery,
    parse_work_item_id,
    parse_work_item_url,
    state_category_from_snapshot,
    states_snapshot,
    work_item_delivery_identity,
    work_item_home,
    work_item_summary,
)
from preloop.utils.issue_urls import parse_issue_url

FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "azure_devops"
        / "work_items.json"
    ).read_text()
)
BOT_ID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
BOT_NAME = "preloop-bot@example.com"


def rest(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES["rest"][key])


def event(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES["events"][key])


def german_states() -> Dict[str, Any]:
    return {t["name"].casefold(): t["states"] for t in rest("work_item_types")["value"]}


# ----------------------------------------------------------------------
# HTML <-> markdown
# ----------------------------------------------------------------------


MARKDOWN = """# Plan

Fix the **login** flow and _log_ `errors` (see [docs](https://example.com/a)).
Second line of the paragraph: https://example.com/b

- first
- second
   - nested
1. one
2. two

> quoted

```python
if a < b:
    print("x")
```"""


def test_markdown_round_trips_through_html() -> None:
    html = markdown_to_html(MARKDOWN)
    assert "<h1>Plan</h1>" in html
    assert "<strong>login</strong>" in html
    assert '<a href="https://example.com/a">docs</a>' in html
    assert '<pre><code class="language-python">if a &lt; b:' in html
    assert "<ul><li>first</li><li>second<ul><li>nested</li></ul></li></ul>" in html
    assert html_to_markdown(html) == MARKDOWN


def test_markdown_escapes_html_it_contains() -> None:
    html = markdown_to_html("<script>alert(1)</script> & <b>x</b>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in html


def test_html_from_azure_editor_becomes_markdown() -> None:
    html = (
        '<div style="color:red">Hello&nbsp;<b>team</b><br>next line</div>'
        "<table><tr><td>a</td><td>b</td></tr></table>"
        '<p><a href="#" data-vss-mention="version:2.0,x">@Jane Doe</a> please look'
        '</p><script>ignored()</script><img src="https://example.com/i.png" alt="shot">'
    )
    assert html_to_markdown(html) == (
        "Hello **team**\nnext line\n\na | b\n\n"
        "[@Jane Doe](#) please look\n\n![shot](https://example.com/i.png)"
    )


def test_empty_fields() -> None:
    assert html_to_markdown(None) == ""
    assert markdown_to_html("  ") == ""


# ----------------------------------------------------------------------
# Identifiers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("297", 297),
        (297, 297),
        ("#297", 297),
        ("AB#297", 297),
        ("fabrikam/Fabrikam-Fiber-Git#297", 297),
        ("https://dev.azure.com/fabrikam/Fabrikam%20Fiber/_workitems/edit/297", 297),
        ("0", None),
        ("abc", None),
        (True, None),
    ],
)
def test_parse_work_item_id(value: Any, expected: Any) -> None:
    assert parse_work_item_id(value) == expected


def test_work_item_url_and_issue_url_parsing() -> None:
    url = "https://dev.azure.com/fabrikam/Fabrikam%20Fiber/_workitems/edit/297/"
    assert parse_work_item_url(url) == ("fabrikam", "Fabrikam Fiber", 297)
    assert parse_work_item_url("https://example.com/a/b/_workitems/edit/1") is None
    parsed = parse_issue_url(url + "?view=x")
    assert parsed is not None
    assert parsed.key == "fabrikam/Fabrikam Fiber#297"
    assert parsed.project == "fabrikam/Fabrikam Fiber"
    # Same path on another host is not an Azure Boards URL.
    assert parse_issue_url("https://example.com/o/p/_workitems/edit/3") is None


# ----------------------------------------------------------------------
# Field mapping
# ----------------------------------------------------------------------


def test_work_item_summary_maps_fields() -> None:
    summary = work_item_summary(
        rest("work_item"), "fabrikam", state_category="Proposed"
    )
    assert summary["key"] == "fabrikam/Fabrikam-Fiber-Git#297"
    assert summary["title"] == "Customer can sign in using their Microsoft Account"
    assert summary["body"] == (
        "Our authorization logic needs to allow for users with "
        "**Microsoft accounts**:\n\n- sign in\n- sign out"
    )
    assert summary["state"] == "open"
    assert summary["state_name"] == "New"
    assert summary["work_item_type"] == "Product Backlog Item"
    assert summary["labels"] == ["auth", "customer"]
    assert summary["assignee"] == {
        "id": "d291b0c4-a05c-4ea6-8df1-4b41d5f39eff",
        "name": "Jane Doe",
        "email": "jane.doe@example.com",
    }
    assert summary["priority"] == {"id": "2", "name": "High", "level": 2}
    assert summary["parent_id"] == 296
    assert [r["relation_type"] for r in summary["relations"]] == [
        "parent",
        "relates_to",
        "pull_request",
    ]
    assert summary["html_url"] == (
        "https://dev.azure.com/fabrikam/Fabrikam-Fiber-Git/_workitems/edit/297"
    )


def test_non_english_closed_state_is_closed_by_category() -> None:
    item = rest("work_item")
    item["fields"]["System.State"] = "Erledigt"
    snapshot = states_snapshot("Fabrikam-Fiber-Git", german_states())
    category = state_category_from_snapshot(
        snapshot, "fabrikam-fiber-git", "Product Backlog Item", "ERLEDIGT"
    )
    assert category == "Completed"
    summary = work_item_summary(item, "fabrikam", state_category=category)
    assert (summary["state"], summary["state_name"]) == ("closed", "Erledigt")
    # "Entfernt" (Removed) is closed too; "Genehmigt" (Proposed) is open.
    for name, expected in (("Entfernt", "closed"), ("Genehmigt", "open")):
        cat = state_category_from_snapshot(
            snapshot, "Fabrikam-Fiber-Git", "Product Backlog Item", name
        )
        assert work_item_summary(item, "fabrikam", state_category=cat)["state"] == (
            expected
        )


def test_unknown_category_falls_back_to_closed_date_not_state_name() -> None:
    item = rest("work_item")
    item["fields"]["System.State"] = "Closed"
    assert work_item_summary(item, "fabrikam")["state"] == "open"
    item["fields"]["Microsoft.VSTS.Common.ClosedDate"] = "2015-01-01T00:00:00Z"
    assert work_item_summary(item, "fabrikam")["state"] == "closed"


def test_markdown_format_field_is_passed_through() -> None:
    item = rest("work_item")
    item["fields"]["System.Description"] = "**already** markdown <x>"
    item["multilineFieldsFormat"] = {"System.Description": "markdown"}
    assert work_item_summary(item, "fabrikam")["body"] == "**already** markdown <x>"


# ----------------------------------------------------------------------
# WIQL
# ----------------------------------------------------------------------


def test_wiql_expresses_open_and_closed_by_category_per_type() -> None:
    query = build_wiql(
        "Fabrikam-Fiber-Git",
        IssueFilter(
            status=["closed"],
            labels=["agent-ready"],
            query="it's broken",
            updated_after=datetime(2026, 1, 2, 3, 4, 5),
            assigned_to="me",
        ),
        states=german_states(),
    )
    assert query.startswith(
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] = 'Fabrikam-Fiber-Git'"
    )
    assert (
        "([System.WorkItemType] = 'product backlog item' AND [System.State] IN "
        "('Erledigt', 'Entfernt'))" in query
    )
    assert "([System.WorkItemType] = 'bug' AND [System.State] IN ('Erledigt'))" in query
    assert "[System.Tags] CONTAINS 'agent-ready'" in query
    assert "[System.Title] CONTAINS 'it''s broken'" in query
    assert "[System.ChangedDate] >= '2026-01-02T03:04:05Z'" in query
    assert "[System.AssignedTo] = @Me" in query
    assert query.endswith("ORDER BY [System.ChangedDate] DESC")

    open_query = build_wiql(
        "P", IssueFilter(status=["open", "In Arbeit"]), states=german_states()
    )
    assert "[System.State] NOT IN ('Erledigt', 'Entfernt')" in open_query
    assert "[System.State] IN ('In Arbeit')" in open_query


# ----------------------------------------------------------------------
# Scope
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repos", "kwargs", "expected"),
    [
        (["api"], {}, "api"),
        (["api", "Web"], {}, None),
        (["api", "Fabrikam"], {}, "Fabrikam"),
        (["api", "web"], {"bound_repository": "WEB"}, "web"),
        (["api", "web"], {"overrides": {"fabrikam": "api"}}, "api"),
        (["api", "web"], {"overrides": {"fabrikam": "gone"}}, None),
        ([], {}, None),
    ],
)
def test_work_item_home(repos: list, kwargs: dict, expected: Any) -> None:
    assert work_item_home("Fabrikam", repos, **kwargs) == expected


# ----------------------------------------------------------------------
# Service hook deliveries
# ----------------------------------------------------------------------


def test_created_delivery_maps_to_issue_opened() -> None:
    delivery = normalize_work_item_delivery(
        event("workitem.created"), organization="fabrikam"
    )
    assert delivery.accepted and delivery.event_type == "issue_opened"
    assert (delivery.work_item_id, delivery.revision) == (5, 1)
    issue = delivery.payload["issue"]
    assert issue["key"] == "fabrikam/FabrikamCloud#5"
    assert issue["title"] == "Some great new idea!"
    assert issue["body"] == "Steps: `login`"
    assert issue["labels"] == ["triage"]
    assert issue["author"]["email"] == "jane.doe@example.com"
    attrs = delivery.payload["object_attributes"]
    assert attrs["url"].endswith("/fabrikam/FabrikamCloud/_workitems/edit/5")
    assert attrs["labels"] == ["triage"]
    assert "self_generated" not in delivery.payload["azure_devops"]


def test_updated_delivery_reads_the_revision_and_changes() -> None:
    delivery = normalize_work_item_delivery(
        event("workitem.updated"), organization="fabrikam"
    )
    # resource.id is the update id (2); the work item is revision.id.
    assert (delivery.work_item_id, delivery.revision) == (5, 2)
    assert delivery.payload["issue"]["state_name"] == "Approved"
    assert delivery.payload["changes"]["System.Tags"] == {
        "old": "triage",
        "new": "triage; agent-ready",
    }
    key = work_item_delivery_identity("t1", delivery)
    assert key == "azure_devops:t1:wi:5:rev:2:workitem.updated"


def test_commented_delivery_carries_the_comment() -> None:
    payload = event("workitem.commented")
    delivery = normalize_work_item_delivery(payload, organization="fabrikam")
    assert delivery.event_type == "comment_created"
    assert delivery.payload["comment"]["id"] == "77"
    assert delivery.payload["comment"]["body"] == "This is a **great** new idea"
    del payload["resource"]["commentVersionRef"]
    older = normalize_work_item_delivery(payload, organization="fabrikam")
    assert older.payload["comment"]["id"] == "rev:4"


def test_self_update_is_marked_by_guid_or_unique_name() -> None:
    updated = event("workitem.updated")
    updated["resource"]["revisedBy"]["id"] = BOT_ID
    by_id = normalize_work_item_delivery(
        updated, organization="fabrikam", own_identity_ids=[BOT_ID, BOT_NAME]
    )
    assert by_id.payload["azure_devops"]["self_generated"] is True

    commented = event("workitem.commented")
    commented["resource"]["fields"]["System.ChangedBy"] = (
        f"Preloop Bot <{BOT_NAME.upper()}>"
    )
    by_name = normalize_work_item_delivery(
        commented, organization="fabrikam", own_identity_ids=[BOT_ID, BOT_NAME]
    )
    assert by_name.payload["azure_devops"]["self_generated"] is True

    human = normalize_work_item_delivery(
        event("workitem.commented"),
        organization="fabrikam",
        own_identity_ids=[BOT_ID, BOT_NAME],
    )
    assert "self_generated" not in human.payload["azure_devops"]


def test_foreign_or_malformed_deliveries_are_rejected() -> None:
    payload = event("workitem.created")
    payload["resource"]["url"] = "https://dev.azure.com/contoso/_apis/wit/workItems/5"
    with pytest.raises(WorkItemRejectedError) as exc:
        normalize_work_item_delivery(payload, organization="fabrikam")
    assert (exc.value.status_code, exc.value.reason) == (403, "organization_mismatch")
    payload = event("workitem.created")
    del payload["resource"]["fields"]["System.TeamProject"]
    with pytest.raises(WorkItemRejectedError, match="missing_project"):
        normalize_work_item_delivery(payload, organization="fabrikam")
    payload = event("workitem.created")
    payload["resource"]["id"] = "5"
    with pytest.raises(WorkItemRejectedError, match="missing_work_item"):
        normalize_work_item_delivery(payload, organization="fabrikam")


def test_state_category_snapshot_overrides_open_closed() -> None:
    delivery = normalize_work_item_delivery(
        event("workitem.updated"), organization="fabrikam"
    )
    apply_state_category(delivery.payload, "Removed")
    assert delivery.payload["issue"]["state"] == "closed"
    assert delivery.payload["object_attributes"]["state"] == "closed"


# ----------------------------------------------------------------------
# Event normalizer
# ----------------------------------------------------------------------


def test_updated_event_is_refined_and_filter_fields_filled() -> None:
    payload = normalize_work_item_delivery(
        event("workitem.updated"), organization="fabrikam"
    ).payload
    assert normalize_event_type("azure_devops", "workitem.updated", payload) == (
        "issue_labeled"
    )
    fields = extract_filter_fields("azure_devops", "workitem.updated", payload)
    assert fields["added_labels"] == ["agent-ready"]
    assert fields["labels"] == ["triage", "agent-ready"]
    assert (fields["status_from"], fields["status_to"]) == ("New", "Approved")
    assert fields["state"] == "Approved"
    assert fields["issue_type"] == "Bug"
    assert fields["priority"] == "Medium"
    assert fields["assignee"] == "Jane Doe"
    extras = secondary_event_types(
        "azure_devops", "issue_labeled", {**payload, **fields}
    )
    assert extras == ("issue_status_changed", "issue_updated")

    # A state move without tag changes is issue_status_changed.
    raw = event("workitem.updated")
    del raw["resource"]["fields"]["System.Tags"]
    moved = normalize_work_item_delivery(raw, organization="fabrikam").payload
    assert normalize_event_type("azure_devops", "workitem.updated", moved) == (
        "issue_status_changed"
    )
    assert normalize_event_type("azure_devops", "workitem.created", moved) == (
        "issue_opened"
    )


def test_trigger_subject_names_the_work_item() -> None:
    payload = normalize_work_item_delivery(
        event("workitem.created"), organization="fabrikam"
    ).payload
    subject = extract_trigger_subject(
        {"source": "azure_devops", "type": "issue_opened", "payload": payload}
    )
    assert subject is not None
    assert subject["reference"] == "fabrikam/FabrikamCloud#5"
    assert subject["title"] == "Some great new idea!"


def test_tracker_edit_keeps_derived_keys_and_drops_identity_on_new_token() -> None:
    stored = {
        "organization": "fabrikam",
        "project": "Old",
        "token_identity_id": BOT_ID,
        "token_identity_unique_name": BOT_NAME,
        "work_item_states": {"p": {}},
    }
    requested = {"organization": "fabrikam", "project": "New"}
    kept = carry_derived_details(stored, requested, token_changed=False)
    assert kept == {**stored, "project": "New"}
    rotated = carry_derived_details(stored, requested, token_changed=True)
    assert rotated == {
        "organization": "fabrikam",
        "project": "New",
        "work_item_states": {"p": {}},
    }
