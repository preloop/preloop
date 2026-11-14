"""The console reads an execution's posted-review record from its detail.

Read-only projection added for the console (#2298 follow-up): the account
user's ``GET /flows/executions/{id}`` carries ``platform_review`` with links
built from the execution's own binding. Nothing is fetched from GitHub.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from preloop.api.endpoints.flows import _github_web_base
from tests.services.test_platform_review import (  # noqa: F401  (fixtures)
    HEAD,
    client,
    finished,
    github,
    human_client,
    identity,
    world,
)


@pytest.mark.parametrize(
    ("tracker_url", "expected"),
    [
        (None, "https://github.com"),
        ("", "https://github.com"),
        ("https://api.github.com", "https://github.com"),
        ("https://api.github.com/", "https://github.com"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        ("https://user:pw@ghe.example.com/api/v3", None),
        ("javascript:alert(1)", None),
        ("https://ghe.example.com/api/v3?x=1", None),
    ],
)
def test_github_web_base(tracker_url: Any, expected: Any) -> None:
    assert _github_web_base(tracker_url) == expected


def test_execution_detail_carries_the_record_and_links(
    world: SimpleNamespace,  # noqa: F811  (imported fixture)
    human_client: TestClient,  # noqa: F811
) -> None:
    api = human_client
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    path = f"/api/v1/flows/executions/{execution.id}"

    pending = api.get(path)
    assert pending.status_code == 200, pending.text
    record = pending.json()["platform_review"]
    assert record["status"] == "pending"
    assert record["pull_request_url"] == f"https://github.com/{project.slug}/pull/7"
    assert record["review_url"] is None and record["commit_url"] is None

    posted = api.post(path + "/platform-review")
    assert posted.status_code == 200, posted.text

    read = api.get(path).json()["platform_review"]
    assert read["status"] == "posted"
    assert read["review_url"] == (
        f"https://github.com/{project.slug}/pull/7"
        f"#pullrequestreview-{read['review_id']}"
    )
    assert read["commit_url"] == f"https://github.com/{project.slug}/commit/{HEAD}"
    # The record itself is the one the publish endpoint returns.
    for key, value in posted.json()["platform_review"].items():
        assert read[key] == value


def test_execution_detail_without_the_opt_in_has_no_record(
    world: SimpleNamespace,  # noqa: F811  (imported fixture)
    human_client: TestClient,  # noqa: F811
) -> None:
    api = human_client
    execution = finished(world, identity(world, world.projects[0]))
    config = dict(world.flow.git_clone_config)
    config.pop("platform_review", None)
    world.flow.git_clone_config = config
    world.db.commit()
    read = api.get(f"/api/v1/flows/executions/{execution.id}")
    assert read.status_code == 200, read.text
    assert read.json()["platform_review"] is None
