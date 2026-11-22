"""Pure Azure DevOps helpers: config, URLs, webhook auth and normalization.

Fixtures are the Microsoft service hook samples
(https://learn.microsoft.com/azure/devops/service-hooks/events), see
``tests/fixtures/azure_devops/service_hooks.json``.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from preloop.services.review_publication import (
    parse_pull_request_url,
    trigger_pull_request,
)
from preloop.sync.event_normalizer import (
    extract_filter_fields,
    extract_trigger_subject,
    normalize_event_type,
)
from preloop.utils import azure_devops as ado
from preloop.utils.git_credentials import credential_username
from preloop.utils.repo_urls import tracker_host_kind

FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "azure_devops"
        / "service_hooks.json"
    ).read_text()
)["events"]
REPO_ID = "c2c2c2c2-dddd-eeee-ffff-a3a3a3a3a3a3"


def fx(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES[key])


def test_config_validation() -> None:
    details = ado.validate_azure_devops_config(
        api_key="pat",
        auth_type="api_token",
        connection_details={"organization": " fabrikam ", "project": "Fabrikam"},
        url="https://dev.azure.com",
    )
    assert details == {"organization": "fabrikam", "project": "Fabrikam"}
    with pytest.raises(ado.AzureDevOpsConfigError, match="organization"):
        ado.validate_azure_devops_config(
            api_key="pat", auth_type=None, connection_details={}
        )
    with pytest.raises(ado.AzureDevOpsConfigError, match="project"):
        ado.validate_azure_devops_config(
            api_key="pat",
            auth_type=None,
            connection_details={"organization": "o", "repository": "r"},
        )
    with pytest.raises(ado.AzureDevOpsConfigError, match="visualstudio"):
        ado.validate_azure_devops_config(
            api_key="pat",
            auth_type=None,
            connection_details={"organization": "o"},
            url="https://fabrikam.visualstudio.com",
        )
    with pytest.raises(ado.AzureDevOpsConfigError, match="on-premises"):
        ado.validate_azure_devops_config(
            api_key="pat",
            auth_type=None,
            connection_details={"organization": "o"},
            url="https://tfs.example.com/tfs",
        )
    with pytest.raises(ado.AzureDevOpsConfigError, match="personal access"):
        ado.validate_azure_devops_config(
            api_key="", auth_type=None, connection_details={"organization": "o"}
        )
    with pytest.raises(ado.AzureDevOpsConfigError):
        ado.validate_azure_devops_config(
            api_key="pat",
            auth_type=None,
            connection_details={"organization": "o", "project": "a/b"},
        )


def test_parse_urls() -> None:
    assert ado.parse_azure_url(
        "https://fabrikam@dev.azure.com/fabrikam/My%20Project/_git/repo/pullrequest/7"
    ) == ("fabrikam", "My Project", "repo", 7)
    assert ado.parse_azure_url("https://dev.azure.com/o/p/_git/r.git") == (
        "o",
        "p",
        "r",
        None,
    )
    assert ado.parse_azure_url("https://github.com/o/r/pull/1") is None
    assert ado.parse_azure_url("https://dev.azure.com/o/p/_git/r/pullrequest/x") is None
    with pytest.raises(ado.AzureDevOpsConfigError):
        ado.parse_azure_url("https://fabrikam.visualstudio.com/p/_git/r")
    assert ado.clone_url("o", "My Project", "r") == (
        "https://dev.azure.com/o/My%20Project/_git/r"
    )
    assert tracker_host_kind("https://dev.azure.com/o/p/_git/r") == "azure_devops"
    assert credential_username("azure_devops", None) == "azure-devops"


def test_review_publication_parses_azure_pr_url() -> None:
    assert parse_pull_request_url(
        "https://dev.azure.com/o/My%20Project/_git/Repo/pullrequest/7"
    ) == ("o/my project/repo", "7")


def test_verify_webhook_secret() -> None:
    assert ado.verify_webhook_secret("s3cret", {ado.WEBHOOK_SECRET_HEADER: "s3cret"})
    basic = "Basic " + base64.b64encode(b"preloop:s3cret").decode()
    assert not ado.verify_webhook_secret("s3cret", {"authorization": basic})
    assert not ado.verify_webhook_secret("s3cret", {ado.WEBHOOK_SECRET_HEADER: "x"})
    assert not ado.verify_webhook_secret("s3cret", {})
    assert not ado.verify_webhook_secret(None, {ado.WEBHOOK_SECRET_HEADER: ""})


def test_normalize_created() -> None:
    delivery = ado.normalize_delivery(
        fx("git.pullrequest.created"),
        organization="Fabrikam",
        repository_known=lambda repo_id: repo_id == REPO_ID,
    )
    assert delivery.accepted
    assert delivery.event_type == "pull_request_opened"
    payload = delivery.payload
    attrs = payload["object_attributes"]
    assert attrs["url"] == (
        "https://dev.azure.com/Fabrikam/Fabrikam/_git/Fabrikam/pullrequest/1"
    )
    assert attrs["source_branch"] == "mytopic"
    assert attrs["target_branch"] == "main"
    assert attrs["author"] == "Jamal Hartnett"
    assert attrs["last_commit"]["id"].startswith("4444eeee")
    assert payload["pull_request"]["head"]["sha"].startswith("4444eeee")
    assert payload["repository"]["clone_url"] == (
        "https://dev.azure.com/Fabrikam/Fabrikam/_git/Fabrikam"
    )
    assert payload["repository"]["full_name"] == "Fabrikam/Fabrikam/Fabrikam"
    # No e-mail address leaves the intake.
    assert "hotmail" not in json.dumps(payload)
    assert trigger_pull_request({"payload": payload}) == (
        "fabrikam/fabrikam/fabrikam",
        "1",
    )


def test_same_head_is_one_identity_and_new_head_another() -> None:
    created = ado.normalize_delivery(
        fx("git.pullrequest.created"), organization="fabrikam"
    )
    vote = fx("git.pullrequest.created")
    vote["eventType"] = "git.pullrequest.updated"
    vote_delivery = ado.normalize_delivery(vote, organization="fabrikam")
    pushed = ado.normalize_delivery(
        fx("git.pullrequest.updated"), organization="fabrikam"
    )
    assert ado.delivery_identity("t", created) == ado.delivery_identity(
        "t", vote_delivery
    )
    assert ado.delivery_identity("t", pushed) != ado.delivery_identity("t", created)
    assert len("delivery:" + ado.delivery_identity("x" * 36, pushed)) < 200


def test_completed_and_unsupported_events_are_not_dispatched() -> None:
    done = ado.normalize_delivery(
        fx("git.pullrequest.updated.completed"), organization="fabrikam"
    )
    assert not done.accepted and done.reason == ado.REASON_NOT_ACTIVE
    other = fx("git.pullrequest.created")
    other["eventType"] = "git.push"
    assert ado.normalize_delivery(other, organization="fabrikam").reason == (
        ado.REASON_UNSUPPORTED_EVENT
    )
    no_head = fx("git.pullrequest.updated")
    del no_head["resource"]["lastMergeSourceCommit"]
    assert (
        ado.normalize_delivery(no_head, organization="fabrikam").reason
        == ado.REASON_NO_SOURCE_COMMIT
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"organization": "contoso"},
        {"organization": "fabrikam", "bound_repository": "Other"},
        {"organization": "fabrikam", "bound_project": "Other"},
        {"organization": "fabrikam", "repository_known": lambda _r: False},
    ],
)
def test_foreign_deliveries_are_rejected(kwargs: Dict[str, Any]) -> None:
    with pytest.raises(ado.AzureDevOpsWebhookRejectedError) as exc:
        ado.normalize_delivery(fx("git.pullrequest.created"), **kwargs)
    assert exc.value.status_code == 403


def test_malformed_payloads_are_rejected() -> None:
    for payload in ([], {}, {"eventType": "git.pullrequest.created"}):
        with pytest.raises(ado.AzureDevOpsWebhookRejectedError) as exc:
            ado.normalize_delivery(payload, organization="fabrikam")
        assert exc.value.status_code == 400


def test_event_normalizer() -> None:
    delivery = ado.normalize_delivery(
        fx("git.pullrequest.updated"), organization="fabrikam"
    )
    assert (
        normalize_event_type("azure_devops", "git.pullrequest.updated", {})
        == "pull_request_updated"
    )
    fields = extract_filter_fields(
        "azure_devops", "git.pullrequest.updated", delivery.payload
    )
    assert fields["source_branch"] == "mytopic"
    assert fields["author"] == "Jamal Hartnett"
    subject = extract_trigger_subject(
        {
            "source": "azure_devops",
            "type": "pull_request_updated",
            "payload": delivery.payload,
        }
    )
    assert subject and subject.get("reference") == "!1"
