"""Preloop posts the validated read-only review and a commit status.

Every test runs against a fake GitHub served through ``httpx.MockTransport``;
no request leaves the process. The fake records every call, so the tests
assert exactly which requests the publish lease was used for.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.schemas.flow import GitCloneConfig
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_execution import CiReviewBinding
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.services import platform_review, publication_credentials
from preloop.services.platform_review import (
    plain_text,
    publish_platform_review,
    render_review,
)
from preloop.services.trusted_publisher import PublicationError

HEAD = "a" * 40
OTHER_HEAD = "c" * 40
SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object"}},
        "checked": {"type": "array", "items": {"type": "string"}},
        "pr_number": {"type": "integer"},
        "repository": {"type": "string"},
        "head_sha": {"type": "string"},
    },
}
OPT_IN = {
    "enabled": True,
    "status": {
        "context": "preloop/review",
        "verdict_states": {"pass": "success", "fail": "failure"},
        "incomplete_state": "error",
    },
}
RESULT = {
    "head_sha": HEAD,
    "verdict": "fail",
    "summary": "One problem in the parser.",
    "findings": [
        {
            "severity": "high",
            "title": "Unchecked length",
            "body": "The length is read before it is validated.",
            "path": "src/parser.py",
            "line": 12,
        },
        {
            "severity": "low",
            "title": "Naming",
            "body": "Outside the diff.",
            "path": "src/other.py",
            "line": 400,
        },
    ],
    "checked": ["correctness", "security"],
}


@pytest.fixture(autouse=True)
def reset_ci_authority() -> Any:
    register_ci_administrator(None)
    yield
    register_ci_administrator(None)


class FakeGitHub:
    """An in-memory GitHub that enforces token scope and records calls."""

    def __init__(self) -> None:
        self.app_permissions = {"pull_requests": "write", "statuses": "write"}
        self.extra_returned: dict[str, str] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.revoked: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.mints: list[dict[str, Any]] = []
        self.pulls: dict[tuple[str, int], dict[str, Any]] = {}
        self.files: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.statuses: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.fail: dict[tuple[str, str], list[int]] = {}
        self.lose_response_after_review = False
        self.tamper_review_body = False
        #: Single-review GETs that answer 404 before the review is returned.
        self.review_misses = 0
        #: Single-review GETs that answer 200 without a body, then the review.
        self.review_bodyless_misses = 0
        #: When set, every single-review GET answers 404.
        self.review_hidden = False
        #: Status-list reads that omit the newest status id.
        self.status_misses = 0
        #: When set, the status list never contains the newest status id.
        self.status_hidden = False
        self.sequence = 5000

    def add_pull(
        self, slug: str, repository_id: str, number: int = 7, head: str = HEAD
    ) -> None:
        repo = {"id": repository_id, "full_name": slug}
        self.pulls[(slug, number)] = {
            "id": 9001,
            "number": number,
            "state": "open",
            "head": {"sha": head, "repo": repo},
            "base": {"ref": "develop", "sha": "b" * 40, "repo": repo},
        }
        self.files[(slug, number)] = [
            {
                "filename": "src/parser.py",
                "patch": "@@ -10,2 +10,4 @@\n context\n+added\n+added again\n context",
            }
        ]

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [
            call
            for call in self.calls
            if call[0] != "GET" and "/access_tokens" not in call[1]
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _token(self, request: httpx.Request, permission: str) -> dict[str, Any] | None:
        value = request.headers.get("Authorization", "").removeprefix("Bearer ")
        token = self.tokens.get(value)
        if token is None or value in self.revoked:
            return None
        return token if permission in token["permissions"] else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        assert request.url.host == "api.github.com"
        planned = self.fail.get((method, path))
        if planned:
            return httpx.Response(planned.pop(0), json={"message": "synthetic"})
        if method == "POST" and path.endswith("/access_tokens"):
            body = json.loads(request.content)
            self.mints.append(body)
            wanted = body["permissions"]
            if any(self.app_permissions.get(k) != v for k, v in wanted.items()):
                return httpx.Response(422, json={"message": "not granted"})
            value = f"lease-{len(self.tokens) + 1}"
            slug = next(
                slug
                for slug, _ in self.pulls
                if slug.endswith("/" + body["repositories"][0])
            )
            self.tokens[value] = {"slug": slug, "permissions": wanted}
            return httpx.Response(
                201,
                json={
                    "token": value,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "repositories": [{"full_name": slug}],
                    "permissions": {
                        **wanted,
                        "metadata": "read",
                        **self.extra_returned,
                    },
                },
            )
        if method == "DELETE" and path == "/installation/token":
            value = request.headers["Authorization"].removeprefix("Bearer ")
            self.revoked.append(value)
            return httpx.Response(204)
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)/(.+)", path)
        assert match, path
        slug, rest = match.groups()
        permission = "statuses" if "statuses" in rest else "pull_requests"
        token = self._token(request, permission)
        if token is None or token["slug"] != slug:
            return httpx.Response(403, json={"message": "Resource not accessible"})
        pull = re.fullmatch(r"pulls/(\d+)(.*)", rest)
        if pull:
            key = (slug, int(pull.group(1)))
            tail = pull.group(2)
            if key not in self.pulls:
                return httpx.Response(404, json={})
            if method == "GET" and tail == "":
                return httpx.Response(200, json=self.pulls[key])
            if method == "GET" and tail == "/files":
                return httpx.Response(200, json=self.files[key])
            if method == "GET" and tail == "/reviews":
                return httpx.Response(200, json=self.reviews.get(key, []))
            if method == "POST" and tail == "/reviews":
                body = json.loads(request.content)
                self.sequence += 1
                stored = "tampered" if self.tamper_review_body else body["body"]
                review = {
                    "id": self.sequence,
                    "user": {"login": "preloop[bot]", "type": "Bot"},
                    "body": stored,
                    "commit_id": body["commit_id"],
                    "state": {
                        "COMMENT": "COMMENTED",
                        "APPROVE": "APPROVED",
                        "REQUEST_CHANGES": "CHANGES_REQUESTED",
                    }[body["event"]],
                    "comments": body.get("comments", []),
                }
                self.reviews.setdefault(key, []).append(review)
                if self.lose_response_after_review:
                    self.lose_response_after_review = False
                    return httpx.Response(502, json={})
                return httpx.Response(200, json=review)
            found = re.fullmatch(r"/reviews/(\d+)", tail)
            if method == "GET" and found:
                if self.review_hidden or self.review_misses > 0:
                    if not self.review_hidden:
                        self.review_misses -= 1
                    return httpx.Response(404, json={})
                for review in self.reviews.get(key, []):
                    if str(review["id"]) == found.group(1):
                        if self.review_bodyless_misses > 0:
                            self.review_bodyless_misses -= 1
                            hidden = dict(review)
                            hidden.pop("body", None)
                            return httpx.Response(200, json=hidden)
                        return httpx.Response(200, json=review)
                return httpx.Response(404, json={})
        status = re.fullmatch(r"statuses/([0-9a-f]+)", rest)
        if method == "POST" and status:
            body = json.loads(request.content)
            self.sequence += 1
            entry = {"id": self.sequence, **body, "creator": {"type": "Bot"}}
            self.statuses.setdefault((slug, status.group(1)), []).insert(0, entry)
            return httpx.Response(201, json=entry)
        listed = re.fullmatch(r"commits/([0-9a-f]+)/statuses", rest)
        if method == "GET" and listed:
            entries = list(self.statuses.get((slug, listed.group(1)), []))
            if entries and (self.status_hidden or self.status_misses > 0):
                if not self.status_hidden:
                    self.status_misses -= 1
                newest = max(entry["id"] for entry in entries)
                entries = [entry for entry in entries if entry["id"] != newest]
            return httpx.Response(200, json=entries)
        raise AssertionError(f"Unexpected request {method} {path}")


@pytest.fixture(autouse=True)
def instant_readback(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Read-back waits are recorded and do not sleep."""
    delays: list[float] = []

    async def _instant(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(platform_review, "_pause", _instant)
    return delays


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(
        platform_review,
        "_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setattr(
        publication_credentials.settings,
        "github_app",
        SimpleNamespace(app_id="1", private_key="-----BEGIN KEY-----"),
    )
    monkeypatch.setattr(publication_credentials.jwt, "encode", lambda *a, **k: "jwt")
    return fake


def _project(db: Session, organization: Any, slug: str) -> models.Project:
    return crud.crud_project.create(
        db,
        obj_in={
            "name": slug,
            "identifier": str(uuid4().int % 10**9),
            "slug": slug,
            "organization_id": organization.id,
            "is_active": True,
        },
    )


@pytest.fixture
def world(db_session: Session, github: FakeGitHub) -> SimpleNamespace:
    """One shared read-only flow serving two repositories, two identities."""
    db = db_session
    account = crud.crud_account.create(
        db, obj_in={"organization_name": "Synthetic", "is_active": True}
    )
    email = f"{uuid4()}@example.com"
    person = CRUDBase(models.Person).create(
        db,
        obj_in={
            "username": f"operator-{uuid4()}",
            "email": email,
            "email_normalized": email,
            "hashed_password": "fixture-only",
        },
    )
    owner = crud.crud_user.create(
        db,
        obj_in={
            "username": person.username,
            "email": email,
            "account_id": account.id,
            "person_id": person.id,
            "is_active": True,
        },
    )
    crud.crud_account.update(db, db_obj=account, obj_in={"primary_user_id": owner.id})
    installation = CRUDBase(models.OAuthAppInstallation).create(
        db,
        obj_in={
            "provider": "github",
            "external_id": 4242,
            "target_type": "Organization",
            "target_id": 77,
            "target_name": "example",
            "account_id": account.id,
        },
    )
    tracker = crud.crud_tracker.create(
        db,
        obj_in={
            "name": "Synthetic App",
            "tracker_type": "github",
            "auth_type": "github_app",
            "oauth_installation_id": installation.id,
            "account_id": account.id,
            "is_active": True,
        },
    )
    organization = crud.crud_organization.create(
        db,
        obj_in={
            "name": "Example",
            "identifier": str(uuid4()),
            "tracker_id": tracker.id,
            "is_active": True,
        },
    )
    projects = [_project(db, organization, f"example/{name}") for name in ("a", "b")]
    flow = CRUDBase(models.Flow).create(
        db,
        obj_in={
            "name": "Organisation review",
            "prompt_template": "Review the change",
            "agent_type": "codex",
            "agent_config": {},
            "account_id": account.id,
            "is_enabled": True,
            "runner_pool": "server",
            "result_schema": SCHEMA,
            "validate_result": True,
            "trigger_project_ids": [str(p.id) for p in projects],
            "git_clone_config": {
                "enabled": True,
                "publication_mode": "read_only",
                "ci_project_selection": True,
                "platform_review": OPT_IN,
                "repositories": [
                    {"project_id": str(p.id), "tracker_id": str(tracker.id)}
                    for p in projects
                ],
            },
        },
    )
    for project in projects:
        github.add_pull(project.slug, project.identifier)
    return SimpleNamespace(
        db=db,
        account=account,
        owner=owner,
        tracker=tracker,
        flow=flow,
        projects=projects,
        github=github,
    )


def identity(world: SimpleNamespace, project: models.Project) -> SimpleNamespace:
    grant = CiGrant(
        version=1,
        project_id=project.id,
        flow_id=world.flow.id,
        actions=tuple(CiAction),
    )
    principal, key, token = crud.crud_ci_principal.provision(
        world.db, actor=world.owner, name=f"CI {project.slug}", grant=grant
    )
    context = crud.crud_ci_principal.authenticate(world.db, token=token)
    assert context is not None
    return SimpleNamespace(principal=principal, key=key, token=token, context=context)


def inspect(db: Session, execution: Any, item: str = "item_1") -> None:
    """Record one native command event, as the controller does for a run."""
    import json

    from preloop.models.crud.execution_tool_evidence import (
        persist_native_command_evidence,
    )

    persist_native_command_evidence(
        db,
        execution_id=str(execution.id),
        line=json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": item,
                    "type": "command_execution",
                    "command": f"git diff {execution.ci_review_binding['base_sha']}...{execution.ci_review_binding['head_sha']}",
                    "exit_code": 0,
                },
            }
        ),
    )
    db.commit()


def finished(
    world: SimpleNamespace,
    who: SimpleNamespace,
    *,
    status: str = "SUCCEEDED",
    result: Any = RESULT,
    valid: bool | None = True,
    head: str = HEAD,
    inspected: bool = True,
) -> models.FlowExecution:
    values = asdict(who.context)
    values.pop("actions")
    values.pop("credential_version")
    binding = CiReviewBinding(
        **values,
        version=1,
        pr_number=7,
        head_sha=head,
        provider_pr_id="9001",
        base_branch="develop",
        base_sha="b" * 40,
    )
    execution = crud.crud_ci_execution.create(
        world.db, context=who.context, binding=binding, event={}
    )
    if inspected:
        inspect(world.db, execution)
    return CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={
            "status": status,
            "end_time": datetime.now(timezone.utc),
            "result": result,
            "result_schema_validation": (
                None
                if valid is None
                else {"valid": valid, "errors": [], "error_count": 0 if valid else 1}
            ),
        },
    )


async def publish(
    world: SimpleNamespace, execution: Any, *, explicit: bool = False
) -> dict[str, Any] | None:
    return await publish_platform_review(
        world.db,
        account_id=world.account.id,
        execution_id=execution.id,
        explicit=explicit,
    )


# --- the happy path and the exact calls made -------------------------------


@pytest.mark.asyncio
async def test_valid_result_posts_one_review_and_one_status_on_the_bound_commit(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["head_sha"] == HEAD
    assert outcome["review"] == "posted" and outcome["review_action"] == "comment"
    assert outcome["commit_status"] == "failure"
    assert outcome["status_context"] == "preloop/review"
    reviews = world.github.reviews[(project.slug, 7)]
    assert len(reviews) == 1
    assert reviews[0]["commit_id"] == HEAD
    assert reviews[0]["state"] == "COMMENTED"
    assert str(reviews[0]["id"]) == outcome["review_id"]
    statuses = world.github.statuses[(project.slug, HEAD)]
    assert len(statuses) == 1
    assert statuses[0]["state"] == "failure"
    assert statuses[0]["context"] == "preloop/review"
    assert statuses[0]["description"] == "Review verdict: fail"
    assert str(execution.id) in statuses[0]["target_url"]
    # The only writes: one review, one status, and the lease revocation.
    assert world.github.writes == [
        ("POST", f"/repos/{project.slug}/pulls/7/reviews"),
        ("POST", f"/repos/{project.slug}/statuses/{HEAD}"),
        ("DELETE", "/installation/token"),
    ]
    receipts = crud.crud_review_publication_receipt.list_for_execution(
        world.db, account_id=world.account.id, execution_id=execution.id
    )
    assert [r.provider_review_id for r in receipts] == [outcome["review_id"]]


@pytest.mark.asyncio
async def test_publish_lease_is_one_repository_two_permissions_and_revoked(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    await publish(world, finished(world, identity(world, project)))
    assert world.github.mints == [
        {
            "repositories": ["a"],
            "permissions": {"pull_requests": "write", "statuses": "write"},
        }
    ]
    assert "contents" not in world.github.mints[0]["permissions"]
    assert world.github.revoked == ["lease-1"]


@pytest.mark.asyncio
async def test_broader_permission_set_returned_by_github_is_refused(
    world: SimpleNamespace,
) -> None:
    world.github.extra_returned = {"contents": "write"}
    execution = finished(world, identity(world, world.projects[0]))
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "lease_unavailable"
    assert world.github.writes == []
    assert world.github.reviews == {} and world.github.statuses == {}


@pytest.mark.asyncio
async def test_review_body_is_readable_and_inline_only_for_diff_lines(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    await publish(world, execution)
    review = world.github.reviews[(project.slug, 7)][0]
    body = review["body"]
    assert body.startswith("<!-- preloop-platform-review v1 ")
    assert "### Preloop review: fail" in body
    assert f"Reviewed commit `{HEAD}`" in body
    assert "One problem in the parser" in body
    assert "**Findings (2)**" in body
    assert "&#91;high&#93; Unchecked length" in body
    assert "&#91;low&#93; Naming" in body
    assert "(`src/parser.py:12`)" in body
    assert "- correctness" in body and "- security" in body
    assert f"/console/flows/executions/{execution.id}" in body
    # Only the finding whose path and line are in the diff is inline.
    assert [(c["path"], c["line"], c["side"]) for c in review["comments"]] == [
        ("src/parser.py", 12, "RIGHT")
    ]


# --- what is never posted ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "valid", "reason"),
    [
        ("FAILED", False, "run_not_succeeded"),
        ("STOPPED", None, "run_not_succeeded"),
        ("FAILED", True, "run_not_succeeded"),
        ("SUCCEEDED", False, "result_not_valid"),
        ("SUCCEEDED", None, "result_not_valid"),
    ],
)
async def test_invalid_stopped_and_failed_runs_post_no_verdict(
    world: SimpleNamespace, status: str, valid: bool | None, reason: str
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project), status=status, valid=valid)
    outcome = await publish(world, execution)
    assert outcome is not None
    assert world.github.reviews == {}
    assert outcome["review"] == "skipped" and outcome["review_reason"] == reason
    # The flow asked for a neutral status so a required check is not left open.
    status_entry = world.github.statuses[(project.slug, HEAD)][0]
    assert status_entry["state"] == "error"
    assert "verdict" in status_entry["description"]
    assert "fail" not in status_entry["description"]
    assert world.github.mints[0]["permissions"] == {"statuses": "write"}


@pytest.mark.asyncio
async def test_incomplete_run_posts_nothing_without_incomplete_state(
    world: SimpleNamespace,
) -> None:
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {
        "enabled": True,
        "status": {"verdict_states": {"pass": "success", "fail": "failure"}},
    }
    world.flow.git_clone_config = config
    world.db.commit()
    execution = finished(world, identity(world, world.projects[0]), status="FAILED")
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "skipped"
    assert world.github.calls == []


@pytest.mark.asyncio
async def test_stored_result_that_no_longer_matches_the_schema_is_not_posted(
    world: SimpleNamespace,
) -> None:
    execution = finished(
        world,
        identity(world, world.projects[0]),
        result={"verdict": "pass", "summary": "x", "findings": "not a list"},
    )
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["review_reason"] == "result_not_valid"
    assert world.github.reviews == {}


@pytest.mark.asyncio
async def test_opt_in_off_mints_no_lease_and_makes_no_call(
    world: SimpleNamespace,
) -> None:
    config = dict(world.flow.git_clone_config)
    config.pop("platform_review")
    world.flow.git_clone_config = config
    world.db.commit()
    execution = finished(world, identity(world, world.projects[0]))
    assert await publish(world, execution) is None
    assert world.github.calls == [] and world.github.mints == []
    assert (
        crud.crud_platform_review_publication.get_for_execution(
            world.db, execution_id=execution.id
        )
        is None
    )
    with pytest.raises(platform_review.PlatformReviewRefusedError):
        await publish(world, execution, explicit=True)
    assert world.github.calls == []


# --- where: the target is the execution's own binding ----------------------


@pytest.mark.asyncio
async def test_result_naming_another_pull_request_is_ignored(
    world: SimpleNamespace,
) -> None:
    project, other = world.projects
    hostile = {
        **RESULT,
        "pr_number": 99,
        "repository": other.slug,
        "head_sha": OTHER_HEAD,
    }
    execution = finished(world, identity(world, project), result=hostile)
    await publish(world, execution)
    # The target is still only the execution's own binding, and a result
    # naming another commit is not a review of this one (#2100): no review,
    # and the status on the bound commit is the error state.
    assert list(world.github.reviews) == []
    assert list(world.github.statuses) == [(project.slug, HEAD)]
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"
    assert all(other.slug not in path for _, path in world.github.calls)


# --- what: only a verified, inspected review is posted (#2100) -------------


@pytest.mark.asyncio
async def test_reported_placeholder_pass_posts_an_error_status_and_no_review(
    world: SimpleNamespace,
) -> None:
    """A schema-valid pass with an all-zero head and no tool call or command."""
    project = world.projects[0]
    placeholder = {
        "verdict": "pass",
        "summary": "",
        "findings": [],
        "head_sha": "0" * 40,
    }
    execution = finished(
        world, identity(world, project), result=placeholder, inspected=False
    )
    outcome = await publish(world, execution)
    assert outcome is not None
    assert world.github.reviews == {}
    assert outcome["review"] == "skipped"
    assert outcome["review_reason"] == "review_degenerate"
    status_entry = world.github.statuses[(project.slug, HEAD)][0]
    assert status_entry["state"] == "error"
    assert status_entry["description"].startswith("Review not verified")


@pytest.mark.asyncio
async def test_uninspected_pass_on_the_right_head_is_not_posted(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(
        world,
        identity(world, project),
        result={**RESULT, "verdict": "pass", "head_sha": HEAD},
        inspected=False,
    )
    outcome = await publish(world, execution)
    assert outcome is not None and world.github.reviews == {}
    assert outcome["review_reason"] == "review_not_inspected"
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"


@pytest.mark.asyncio
async def test_unverified_result_is_an_error_even_without_incomplete_state(
    world: SimpleNamespace,
) -> None:
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {
        "enabled": True,
        "status": {"verdict_states": {"pass": "success", "fail": "failure"}},
    }
    world.flow.git_clone_config = config
    world.db.commit()
    project = world.projects[0]
    execution = finished(
        world,
        identity(world, project),
        result={**RESULT, "verdict": "pass"},
        inspected=False,
    )
    await publish(world, execution)
    assert world.github.reviews == {}
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"


@pytest.mark.asyncio
async def test_posted_verdict_must_name_the_reviewed_commit(
    world: SimpleNamespace,
) -> None:
    """An optional head_sha the result omits is not a binding for posting."""
    project = world.projects[0]
    unbound = {k: v for k, v in RESULT.items() if k != "head_sha"}
    execution = finished(
        world, identity(world, project), result={**unbound, "verdict": "pass"}
    )
    outcome = await publish(world, execution)
    assert outcome is not None and world.github.reviews == {}
    assert outcome["review_reason"] == "review_binding_mismatch"
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"


@pytest.mark.asyncio
async def test_floor_opt_out_posts_a_bound_review_without_tool_evidence(
    world: SimpleNamespace,
) -> None:
    config = dict(world.flow.git_clone_config)
    config["review_integrity"] = {"require_inspection": False}
    world.flow.git_clone_config = config
    world.db.commit()
    project = world.projects[0]
    execution = finished(
        world,
        identity(world, project),
        result={**RESULT, "verdict": "pass", "head_sha": HEAD},
        inspected=False,
    )
    await publish(world, execution)
    assert list(world.github.reviews) == [(project.slug, 7)]
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "success"


@pytest.mark.asyncio
async def test_run_failed_by_the_floor_is_reported_with_its_category(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project), status="FAILED")
    execution.failure_category = "review_not_inspected"
    world.db.commit()
    outcome = await publish(world, execution)
    assert outcome is not None and world.github.reviews == {}
    assert outcome["review_reason"] == "review_not_inspected"
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"


@pytest.mark.asyncio
async def test_two_projects_on_one_shared_flow_never_cross(
    world: SimpleNamespace,
) -> None:
    project_a, project_b = world.projects
    who_a, who_b = identity(world, project_a), identity(world, project_b)
    run_a = finished(world, who_a)
    run_b = finished(world, who_b, result={**RESULT, "verdict": "pass"})
    await publish(world, run_a)
    calls_a = list(world.github.calls)
    assert all(project_b.slug not in path for _, path in calls_a)
    await publish(world, run_b)
    calls_b = world.github.calls[len(calls_a) :]
    assert all(project_a.slug not in path for _, path in calls_b)
    assert [m["repositories"] for m in world.github.mints] == [["a"], ["b"]]
    assert world.github.statuses[(project_a.slug, HEAD)][0]["state"] == "failure"
    assert world.github.statuses[(project_b.slug, HEAD)][0]["state"] == "success"
    # A's key can neither read B's execution nor cause a post for it.
    for action in (CiAction.READ_RESULT, CiAction.PUBLISH_REVIEW):
        assert (
            crud.crud_ci_execution.get(
                world.db, context=who_a.context, execution_id=run_b.id, action=action
            )
            is None
        )


@pytest.mark.asyncio
async def test_lease_for_one_repository_cannot_write_to_the_other(
    world: SimpleNamespace,
) -> None:
    """The fake enforces what GitHub enforces: the token is repository-bound."""
    project_a, project_b = world.projects
    await publish(world, finished(world, identity(world, project_a)))
    token = "lease-1"
    world.github.revoked.clear()
    async with httpx.AsyncClient(transport=world.github.transport()) as client:
        response = await client.post(
            f"https://api.github.com/repos/{project_b.slug}/statuses/{HEAD}",
            headers={"Authorization": f"Bearer {token}"},
            json={"state": "success"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_project_removed_from_the_flow_revokes_posting(
    world: SimpleNamespace,
) -> None:
    project_a, project_b = world.projects
    execution = finished(world, identity(world, project_a))
    config = dict(world.flow.git_clone_config)
    config["repositories"] = [
        repo
        for repo in config["repositories"]
        if repo["project_id"] != str(project_a.id)
    ]
    world.flow.git_clone_config = config
    world.flow.trigger_project_ids = [str(project_b.id)]
    world.db.commit()
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "admission_revoked"
    assert world.github.calls == []


@pytest.mark.asyncio
async def test_disabled_identity_revokes_posting(world: SimpleNamespace) -> None:
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    crud.crud_ci_principal.change(
        world.db, actor=world.owner, principal_id=who.principal.id, enabled=False
    )
    outcome = await publish(world, execution, explicit=True)
    assert outcome is not None and outcome["reason"] == "admission_revoked"
    assert world.github.calls == []


@pytest.mark.asyncio
async def test_pull_request_identity_mismatch_is_refused(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    world.github.pulls[(project.slug, 7)]["id"] = 123456
    outcome = await publish(world, finished(world, identity(world, project)))
    assert outcome is not None and outcome["reason"] == "binding_changed"
    assert world.github.reviews == {}


# --- exact commit, stale head, replay --------------------------------------


@pytest.mark.asyncio
async def test_moved_head_skips_the_review_and_keeps_the_status_on_the_reviewed_sha(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    world.github.pulls[(project.slug, 7)]["head"]["sha"] = OTHER_HEAD
    outcome = await publish(world, execution)
    assert outcome is not None
    assert outcome["review"] == "skipped" and outcome["review_reason"] == "head_moved"
    assert outcome["reason"] == "head_moved"
    assert world.github.reviews == {}
    # The status is true for the commit that was reviewed, and only for it.
    assert list(world.github.statuses) == [(project.slug, HEAD)]
    assert (project.slug, OTHER_HEAD) not in world.github.statuses


@pytest.mark.asyncio
async def test_closed_pull_request_gets_no_review(world: SimpleNamespace) -> None:
    project = world.projects[0]
    world.github.pulls[(project.slug, 7)]["state"] = "closed"
    outcome = await publish(world, finished(world, identity(world, project)))
    assert outcome is not None and outcome["review_reason"] == "pull_request_closed"
    assert world.github.reviews == {}


@pytest.mark.asyncio
async def test_duplicate_delivery_and_republish_post_once(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    first = await publish(world, execution)
    calls = len(world.github.calls)
    second = await publish(world, execution)
    third = await publish(world, execution, explicit=True)
    assert first == second == third
    assert len(world.github.calls) == calls
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.asyncio
async def test_live_claim_blocks_a_concurrent_attempt(world: SimpleNamespace) -> None:
    execution = finished(world, identity(world, world.projects[0]))
    store = crud.crud_platform_review_publication
    target = {
        "account_id": world.account.id,
        "execution_id": execution.id,
        "repository": world.projects[0].slug,
        "pull_request": 7,
        "head_sha": HEAD,
    }
    assert store.claim(world.db, **target) is not None
    assert store.claim(world.db, **target) is None
    outcome = await publish(world, execution, explicit=True)
    assert outcome is not None and outcome["status"] == "publishing"
    assert world.github.calls == []


@pytest.mark.asyncio
async def test_lost_response_after_posting_is_adopted_not_reposted(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    world.github.lose_response_after_review = True
    first = await publish(world, execution)
    assert first is not None and first["status"] == "failed"
    assert first["reason"] == "provider_unavailable" and first["retry_scheduled"]
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    # The status was still set: one failed step does not hold up the other.
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1
    second = await publish(world, execution, explicit=True)
    assert second is not None and second["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1
    assert second["review_id"] == str(world.github.reviews[(project.slug, 7)][0]["id"])


@pytest.mark.asyncio
async def test_review_with_a_foreign_marker_is_not_adopted(
    world: SimpleNamespace,
) -> None:
    """A stranger cannot suppress the review by guessing the marker."""
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    forged = platform_review.review_marker(execution.id, HEAD, "0" * 32)
    world.github.reviews[(project.slug, 7)] = [
        {
            "id": 1,
            "user": {"type": "User"},
            "body": forged,
            "commit_id": HEAD,
            "state": "COMMENTED",
        }
    ]
    world.github.fail[("GET", f"/repos/{project.slug}/pulls/7")] = [500]
    await publish(world, execution)
    outcome = await publish(world, execution, explicit=True)
    assert outcome is not None and outcome["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 2


@pytest.mark.asyncio
async def test_read_back_mismatch_is_recorded_and_never_cleared_by_a_retry(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    world.github.tamper_review_body = True
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "readback_mismatch"
    assert outcome["review_id"] is not None
    again = await publish(world, execution, explicit=True)
    assert again is not None and again["reason"] == "readback_mismatch"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


# --- failure isolation and recovery ----------------------------------------


@pytest.mark.asyncio
async def test_posting_failure_leaves_status_and_result_untouched_then_recovers(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    path = f"/repos/{project.slug}/pulls/7/reviews"
    world.github.fail[("POST", path)] = [500]
    world.github.fail[("POST", f"/repos/{project.slug}/statuses/{HEAD}")] = [503]
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "provider_unavailable"
    assert outcome["retry_scheduled"] is True
    world.db.refresh(execution)
    assert execution.status == "SUCCEEDED"
    assert execution.result == RESULT
    assert world.github.revoked == ["lease-1"]
    # Recovery: the same stored result, no model run, published once.
    recovered = await publish(world, execution, explicit=True)
    assert recovered is not None and recovered["status"] == "posted"
    assert recovered["attempts"] == 2
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.asyncio
async def test_automatic_retries_are_bounded(world: SimpleNamespace) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    path = f"/repos/{project.slug}/pulls/7"
    world.github.fail[("GET", path)] = [500] * 20
    world.github.fail[("POST", f"/repos/{project.slug}/statuses/{HEAD}")] = [500] * 20
    store = crud.crud_platform_review_publication
    for _ in range(platform_review.MAX_AUTOMATIC_ATTEMPTS + 2):
        await publish(world, execution)
        row = store.get_for_execution(world.db, execution_id=execution.id)
        if row.next_attempt_at is not None:
            assert row.next_attempt_at > datetime.now(timezone.utc).replace(tzinfo=None)
            row.next_attempt_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(seconds=1)
            world.db.commit()
    row = store.get_for_execution(world.db, execution_id=execution.id)
    assert row.attempts == platform_review.MAX_AUTOMATIC_ATTEMPTS
    assert row.retryable is False and row.next_attempt_at is None
    assert store.due_execution_ids(world.db) == []


@pytest.mark.asyncio
async def test_sweep_lists_due_retries_and_unclaimed_finished_runs(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    who = identity(world, project)
    failed = finished(world, who)
    world.github.fail[("GET", f"/repos/{project.slug}/pulls/7")] = [500]
    world.github.fail[("POST", f"/repos/{project.slug}/statuses/{HEAD}")] = [500]
    await publish(world, failed)
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=failed.id)
    row.next_attempt_at = datetime(2020, 1, 1)
    world.db.commit()
    assert (world.account.id, failed.id) in store.due_execution_ids(world.db)
    unclaimed = finished(world, who)
    unclaimed.end_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    world.flow.updated_at = datetime(2020, 1, 1)
    world.db.commit()
    found = store.unclaimed_execution_ids(world.db, finished_within=timedelta(hours=24))
    assert (world.account.id, unclaimed.id) in found
    assert (world.account.id, failed.id) not in found


@pytest.mark.asyncio
async def test_missing_statuses_permission_posts_the_review_and_says_why(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    world.github.app_permissions = {"pull_requests": "write"}
    execution = finished(world, identity(world, project))
    outcome = await publish(world, execution)
    # The review is there; the publication stays open and says what is missing.
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "statuses_permission_missing"
    assert outcome["review"] == "posted" and outcome["review_id"]
    assert outcome["commit_status"] is None
    assert outcome["status_reason"] == "statuses_permission_missing"
    assert outcome["retry_scheduled"] is False
    assert world.github.statuses == {}
    assert [m["permissions"] for m in world.github.mints] == [
        {"pull_requests": "write", "statuses": "write"},
        {"pull_requests": "write"},
    ]
    # Once the installation may set statuses, a publish call adds only that.
    world.github.app_permissions = {"pull_requests": "write", "statuses": "write"}
    recovered = await publish(world, execution, explicit=True)
    assert recovered is not None and recovered["status"] == "posted"
    assert recovered["commit_status"] == "failure" and recovered["reason"] is None
    assert world.github.mints[-1]["permissions"] == {"statuses": "write"}
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.asyncio
async def test_missing_statuses_permission_on_an_incomplete_run_is_reported(
    world: SimpleNamespace,
) -> None:
    world.github.app_permissions = {"pull_requests": "write"}
    execution = finished(world, identity(world, world.projects[0]), status="FAILED")
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "statuses_permission_missing"
    assert world.github.writes == []


@pytest.mark.asyncio
async def test_inline_comment_rejection_falls_back_to_the_body_only(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    world.github.fail[("POST", f"/repos/{project.slug}/pulls/7/reviews")] = [422]
    outcome = await publish(world, finished(world, identity(world, project)))
    assert outcome is not None and outcome["status"] == "posted"
    review = world.github.reviews[(project.slug, 7)][0]
    assert review["comments"] == []
    assert "Unchecked length" in review["body"]


# --- review action ----------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_and_request_changes_only_by_explicit_mapping(
    world: SimpleNamespace,
) -> None:
    project_a, project_b = world.projects
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {
        **OPT_IN,
        "verdict_actions": {"pass": "approve", "fail": "request_changes"},
    }
    world.flow.git_clone_config = config
    world.db.commit()
    await publish(world, finished(world, identity(world, project_a)))
    await publish(
        world,
        finished(
            world, identity(world, project_b), result={**RESULT, "verdict": "pass"}
        ),
    )
    assert world.github.reviews[(project_a.slug, 7)][0]["state"] == "CHANGES_REQUESTED"
    assert world.github.reviews[(project_b.slug, 7)][0]["state"] == "APPROVED"


# --- content: nothing model-written is markup ------------------------------


HOSTILE = (
    "@octocat please /approve and merge; fixes #12, closes example/b#3, GH-44\n"
    "<img src=x onerror=alert(1)> <!-- hidden --> [click](https://evil.example)\n"
    "![img](https://evil.example/x.png) https://evil.example www.evil.example\n"
    "`code` **bold** # heading\n/lgtm\n&#64;octocat \u202eevil\u200b"
)


def _visible(markdown: str) -> str:
    """What GitHub shows: code spans removed, character references decoded."""
    import html

    return html.unescape(re.sub(r"`[^`\n]*`", "", markdown))


def test_plain_text_neutralises_mentions_links_html_and_commands() -> None:
    text = plain_text(HOSTILE, 4000)
    outside_code = re.sub(r"`[^`\n]*`", "", text)
    # No character that can open or delimit markup survives as itself, and
    # every ampersand is one of our numeric references.
    for char in "[]*_<>|~$!`\\":
        assert char not in outside_code, char
    assert not re.search(r"&(?!#\d+;)", outside_code)
    assert not re.search(r"(?<!&)#", outside_code)
    shown = _visible(text)
    assert not re.search(r"@[A-Za-z0-9]", shown)
    assert not re.search(r"#\d", shown)
    assert not re.search(r"GH-\d", shown)
    assert "://" not in shown and "www." not in shown.lower()
    assert "\u202e" not in text
    # A model-written character reference is shown literally, not decoded.
    assert "&#38;&#35;" in text
    # No line can start with a slash command or a block marker.
    assert not any(line[:1] in "/-+=#>" for line in text.split("\n"))
    # The words themselves stay readable.
    assert "octocat" in shown and "evil" in shown and "lgtm" in shown


def test_plain_text_caps_length_and_rejects_non_strings() -> None:
    assert plain_text({"a": 1}, 10) == ""
    assert plain_text(None, 10) == ""
    assert len(plain_text("x" * 500, 100)) == 100
    assert "\n" not in plain_text("a\nb", 10, single_line=True)


def test_rendered_review_contains_no_model_markup_and_respects_caps() -> None:
    result = {
        "verdict": "fail",
        "summary": HOSTILE,
        "findings": [
            {
                "severity": "<b>high</b>",
                "title": HOSTILE,
                "body": HOSTILE * 50,
                "path": "src/[x](https://evil.example).py",
                "line": 3,
            }
        ]
        * 300,
        "checked": [HOSTILE] * 100,
    }
    body, comments = render_review(
        result,
        verdict="fail",
        execution_id=uuid4(),
        head_sha=HEAD,
        nonce="n" * 32,
        inline={},
    )
    assert comments == []
    assert len(body) <= platform_review.MAX_BODY_CHARS
    assert "**Findings (300)**" in body
    assert "further finding(s) are not shown" in body
    lines = body.split("\n")
    # The marker is ours; no other HTML and no model line at column zero.
    assert lines[0].startswith("<!-- preloop-platform-review v1 ")
    rest = "\n".join(lines[1:])
    assert "<" not in rest and "\\" not in rest
    shown = _visible(rest)
    assert not re.search(r"@[A-Za-z0-9]", shown)
    assert not re.search(r"#\d", shown)
    # Inside a code span GitHub interprets nothing; outside, no link syntax.
    assert "](" not in re.sub(r"`[^`\n]*`", "", body)
    assert "https://evil" not in shown and "www.evil" not in shown
    assert not any(line.startswith("/") for line in lines)


def test_inline_comments_require_a_path_and_line_in_the_diff() -> None:
    lines = platform_review.diff_lines(
        [
            {"filename": "a.py", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n-c\n d"},
            {"filename": "bin.dat"},
        ]
    )
    assert lines == {"a.py": {1, 2, 3}}
    result = {
        "verdict": "fail",
        "findings": [
            {"title": "in diff", "path": "a.py", "line": 2},
            {"title": "wrong line", "path": "a.py", "line": 40},
            {"title": "wrong file", "path": "../../etc/passwd", "line": 1},
            {"title": "bool line", "path": "a.py", "line": True},
            {"title": "no patch", "path": "bin.dat", "line": 1},
        ],
    }
    body, comments = render_review(
        result,
        verdict="fail",
        execution_id=uuid4(),
        head_sha=HEAD,
        nonce="n" * 32,
        inline=lines,
    )
    assert [(c["path"], c["line"]) for c in comments] == [("a.py", 2)]
    for title in ("wrong line", "wrong file", "bool line", "no patch"):
        assert title in body


# --- opt-in validation ------------------------------------------------------


def _clone(**changes: Any) -> dict[str, Any]:
    base = {
        "enabled": True,
        "publication_mode": "read_only",
        "repositories": [{"project_id": str(uuid4()), "tracker_id": str(uuid4())}],
        "platform_review": OPT_IN,
    }
    base.update(changes)
    return base


def test_opt_in_is_accepted_in_read_only_mode_only() -> None:
    assert GitCloneConfig.model_validate(_clone()).platform_review.enabled is True
    for mode in ("legacy", "isolated"):
        with pytest.raises(ValueError, match="read_only"):
            GitCloneConfig.model_validate(_clone(publication_mode=mode))


@pytest.mark.parametrize(
    "key",
    ["create_pull_request", "publication_approval", "repository_bindings"],
)
def test_read_only_mode_still_rejects_every_other_publication_key(key: str) -> None:
    value: Any = [{"repository": "example/a"}] if key == "repository_bindings" else True
    with pytest.raises(ValueError):
        GitCloneConfig.model_validate(_clone(**{key: value}))


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": True, "action": "approve"},
        {"enabled": True, "target": {"pr_number": 1}},
        {"enabled": True, "status": {"verdict_states": {}}},
        {"enabled": True, "status": {"verdict_states": {"pass": "pending"}}},
        {
            "enabled": True,
            "status": {"context": "x\ny", "verdict_states": {"p": "error"}},
        },
        {
            "enabled": True,
            "status": {"verdict_states": {"p": "error"}, "incomplete_state": "success"},
        },
        {"enabled": True, "verdict_actions": {"pass": "merge"}},
    ],
)
def test_malformed_opt_in_is_rejected(block: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GitCloneConfig.model_validate(_clone(platform_review=block))
    with pytest.raises(ValueError):
        platform_review.validate_platform_review_block(block)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"result_schema": None}, "result_schema"),
        ({"validate_result": False}, "validate_result"),
        ({"result_schema": {"type": "object"}}, "verdict"),
        (
            {
                "result_schema": {
                    **SCHEMA,
                    "properties": {"verdict": {"type": "string"}},
                }
            },
            "verdict",
        ),
        (
            {
                "result_schema": {
                    **SCHEMA,
                    "properties": {
                        "verdict": {"type": "string", "enum": ["pass", "fail", "warn"]}
                    },
                }
            },
            "warn",
        ),
    ],
)
def test_opt_in_requires_a_validated_result_with_a_fully_mapped_verdict(
    changes: dict[str, Any], message: str
) -> None:
    flow = SimpleNamespace(
        git_clone_config=_clone(), result_schema=SCHEMA, validate_result=True
    )
    platform_review.validate_platform_review_flow(flow)
    for name, value in changes.items():
        setattr(flow, name, value)
    with pytest.raises(ValueError, match=message):
        platform_review.validate_platform_review_flow(flow)


def test_opt_in_is_rejected_for_personal_token_and_non_github_trackers(
    world: SimpleNamespace,
) -> None:
    config = world.flow.git_clone_config
    platform_review.validate_platform_review_trackers(
        world.db, account_id=world.account.id, config=config
    )
    world.tracker.auth_type = "api_key"
    world.db.commit()
    with pytest.raises(ValueError, match="GitHub App"):
        platform_review.validate_platform_review_trackers(
            world.db, account_id=world.account.id, config=config
        )
    world.tracker.auth_type = "github_app"
    world.tracker.tracker_type = "gitlab"
    world.db.commit()
    with pytest.raises(ValueError, match="GitHub App"):
        platform_review.validate_platform_review_trackers(
            world.db, account_id=world.account.id, config=config
        )
    with pytest.raises(ValueError):
        platform_review.validate_platform_review_trackers(
            world.db, account_id=uuid4(), config=config
        )


# --- identities are never silently upgraded --------------------------------


# --- lease profile ----------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_request_must_name_exactly_one_of_write_or_profile() -> None:
    tracker = SimpleNamespace(
        tracker_type="github",
        auth_type="github_app",
        oauth_installation=SimpleNamespace(external_id="1"),
    )
    url = "https://github.com/example/a.git"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        for kwargs in (
            {},
            {"write": False, "profile": "review"},
            {"profile": "contents"},
            {"profile": "review", "allow_legacy_oauth_app": True},
        ):
            with pytest.raises(PublicationError, match="Invalid GitHub App lease"):
                await publication_credentials.mint_repository_lease(
                    tracker, url, client=client, **kwargs
                )


def test_no_publish_profile_carries_contents() -> None:
    for permissions in publication_credentials.REVIEW_PUBLISH_PROFILES.values():
        assert set(permissions) <= {"pull_requests", "statuses"}


# --- the run never sees the credential --------------------------------------


@pytest.mark.asyncio
async def test_container_context_of_an_opted_in_flow_has_no_write_credential(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import read_only_review
    from preloop.services.trusted_publisher import PublicationLease

    who = identity(world, world.projects[0])
    execution = finished(world, who, status="PENDING")
    requested: list[dict[str, Any]] = []

    async def mint(tracker: Any, url: str, **kwargs: Any) -> PublicationLease:
        requested.append(kwargs)
        return PublicationLease(
            "read-token", url, datetime.now(timezone.utc) + timedelta(hours=1)
        )

    monkeypatch.setattr(read_only_review, "mint_repository_lease", mint)
    context: dict[str, Any] = {}
    await read_only_review.prepare_read_only_review(
        world.db, world.flow, execution, context
    )
    assert [r["write"] for r in requested] == [False]
    assert all("profile" not in r for r in requested)
    credentials = context["git_credentials_map"]
    assert [c["permission"] for c in credentials.values()] == ["read"]
    assert "platform_review" not in json.dumps(credentials)
    assert world.github.mints == []


# --- HTTP: publish and read -------------------------------------------------


@pytest.fixture
def client(
    db_session: Session, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    from tests.api.test_ci_execution_http import client as make_client

    client = make_client.__wrapped__(db_session, monkeypatch)
    client.app.dependency_overrides[get_current_active_user] = lambda: world.owner
    return client


@pytest.fixture
def human_client(client: TestClient, world: SimpleNamespace) -> TestClient:
    """The same app, called by an account user instead of a CI key."""
    from preloop.api.auth.ci import get_current_actor

    client.app.dependency_overrides[get_current_actor] = lambda: world.owner
    return client


def test_ci_identity_publishes_its_own_execution_and_reads_the_record(
    world: SimpleNamespace, client: TestClient
) -> None:
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    headers = {"Authorization": f"Bearer {who.token}"}
    path = f"/api/v1/flows/executions/{execution.id}"
    before = client.get(path, headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["platform_review"]["status"] == "pending"
    posted = client.post(path + "/platform-review", headers=headers)
    assert posted.status_code == 200, posted.text
    record = posted.json()["platform_review"]
    assert record["status"] == "posted" and record["head_sha"] == HEAD
    again = client.post(path + "/platform-review", headers=headers)
    assert again.json()["platform_review"] == record
    assert "tool_evidence" not in record
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    status = client.get(path, headers=headers)
    assert status.status_code == 200, status.text
    # A status poll does not decode the evidence records.
    assert status.json()["platform_review"] == record
    result = client.get(path + "/result", headers=headers)
    assert result.status_code == 200, result.text
    result_record = result.json()["platform_review"]
    assert result.json()["tool_evidence"] == result_record["tool_evidence"]
    assert {
        key: value for key, value in result_record.items() if key != "tool_evidence"
    } == record
    assert "lease-" not in posted.text


def test_ci_identity_cannot_publish_another_principals_execution(
    world: SimpleNamespace, client: TestClient
) -> None:
    project_a, project_b = world.projects
    who_a, who_b = identity(world, project_a), identity(world, project_b)
    run_b = finished(world, who_b)
    denied = client.post(
        f"/api/v1/flows/executions/{run_b.id}/platform-review",
        headers={"Authorization": f"Bearer {who_a.token}"},
    )
    assert denied.status_code == 404
    assert world.github.calls == []
    read = client.get(
        f"/api/v1/flows/executions/{run_b.id}/result",
        headers={"Authorization": f"Bearer {who_a.token}"},
    )
    assert read.status_code == 404


def test_publish_needs_the_explicit_action(
    world: SimpleNamespace, client: TestClient
) -> None:
    project = world.projects[0]
    grant = CiGrant(
        version=1,
        project_id=project.id,
        flow_id=world.flow.id,
        actions=(CiAction.TRIGGER, CiAction.READ_EXECUTION, CiAction.READ_RESULT),
    )
    _, _, token = crud.crud_ci_principal.provision(
        world.db, actor=world.owner, name="Reader", grant=grant
    )
    who = identity(world, project)
    execution = finished(world, who)
    denied = client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403
    assert world.github.calls == []


def test_publish_request_cannot_name_a_target(
    world: SimpleNamespace, client: TestClient
) -> None:
    project, other = world.projects
    who = identity(world, project)
    execution = finished(world, who)
    posted = client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review",
        headers={"Authorization": f"Bearer {who.token}"},
        json={"repository": other.slug, "pr_number": 99, "head_sha": OTHER_HEAD},
    )
    # A member that is not a precondition is refused, not ignored: a
    # misspelt precondition must never post unchecked.
    assert posted.status_code == 400, posted.text
    assert posted.json()["detail"]["code"] == "review_precondition_malformed"
    assert world.github.reviews == {}
    assert world.github.writes == []


def test_account_user_republishes_and_unfinished_or_opted_out_is_refused(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    client = human_client
    project = world.projects[0]
    who = identity(world, project)
    running = finished(world, who, status="RUNNING")
    refused = client.post(f"/api/v1/flows/executions/{running.id}/platform-review")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "execution_not_finished"
    done = finished(world, who)
    posted = client.post(f"/api/v1/flows/executions/{done.id}/platform-review")
    assert posted.status_code == 200, posted.text
    assert posted.json()["platform_review"]["status"] == "posted"
    result = client.get(f"/api/v1/flows/executions/{done.id}/result")
    assert result.json()["platform_review"]["status"] == "posted"
    missing = client.post(f"/api/v1/flows/executions/{uuid4()}/platform-review")
    assert missing.status_code == 404
    assert (
        world.github.writes.count(("POST", f"/repos/{project.slug}/pulls/7/reviews"))
        == 1
    )


def test_ci_identity_cannot_enable_the_opt_in(
    world: SimpleNamespace, client: TestClient
) -> None:
    who = identity(world, world.projects[0])
    for method, path in (
        ("put", f"/api/v1/flows/{world.flow.id}"),
        ("patch", f"/api/v1/flows/{world.flow.id}"),
        ("post", "/api/v1/flows"),
    ):
        response = getattr(client, method)(
            path,
            headers={"Authorization": f"Bearer {who.token}"},
            json={"git_clone_config": {"platform_review": {"enabled": True}}},
        )
        assert response.status_code in {401, 403, 404, 405}, (method, response.text)


def test_finished_event_carries_the_record(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    client = human_client
    from preloop.services.event_webhooks import emitters

    who = identity(world, world.projects[0])
    execution = finished(world, who)
    assert emitters._platform_review(world.db, execution, world.flow)["status"] == (
        "pending"
    )
    client.post(f"/api/v1/flows/executions/{execution.id}/platform-review")
    record = emitters._platform_review(world.db, execution, world.flow)
    assert record["status"] == "posted" and record["head_sha"] == HEAD


# --- completion path --------------------------------------------------------


def _orchestrator(flow: Any, execution: Any) -> Any:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.flow = flow
    orchestrator.execution_log = execution
    return orchestrator


@pytest.mark.asyncio
async def test_completion_path_publishes_on_its_own_session_without_a_model_run(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.models.db import session as db_session_module

    project = world.projects[0]
    execution = finished(world, identity(world, project))
    close = world.db.close
    monkeypatch.setattr(world.db, "close", lambda: None)
    monkeypatch.setattr(db_session_module, "get_db_session", lambda: iter([world.db]))
    await _orchestrator(world.flow, execution)._publish_platform_review()
    monkeypatch.setattr(world.db, "close", close)
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.asyncio
async def test_completion_path_does_nothing_without_the_opt_in_or_a_ci_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[Any] = []

    async def record(**kwargs: Any) -> None:
        called.append(kwargs)

    monkeypatch.setattr(platform_review, "publish_finished_execution", record)
    opted_out = SimpleNamespace(
        account_id=uuid4(),
        git_clone_config={"enabled": True, "publication_mode": "read_only"},
    )
    opted_in = SimpleNamespace(account_id=uuid4(), git_clone_config=_clone())
    bound = SimpleNamespace(id=uuid4(), ci_review_binding={"version": 1})
    human = SimpleNamespace(id=uuid4(), ci_review_binding=None)
    await _orchestrator(opted_out, bound)._publish_platform_review()
    await _orchestrator(opted_in, human)._publish_platform_review()
    assert called == []
    await _orchestrator(opted_in, bound)._publish_platform_review()
    assert called == [{"account_id": opted_in.account_id, "execution_id": bound.id}]


@pytest.mark.asyncio
async def test_completion_path_swallows_a_publication_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(**kwargs: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(platform_review, "publish_finished_execution", explode)
    flow = SimpleNamespace(account_id=uuid4(), git_clone_config=_clone())
    execution = SimpleNamespace(id=uuid4(), ci_review_binding={"version": 1})
    await _orchestrator(flow, execution)._publish_platform_review()


@pytest.mark.asyncio
async def test_unexpected_error_during_publication_is_recorded_not_raised(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = finished(world, identity(world, world.projects[0]))

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(platform_review, "plan_publication", explode)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "failed"
    assert outcome["reason"] == "internal_error"
    world.db.refresh(execution)
    assert execution.status == "SUCCEEDED" and execution.result == RESULT


@pytest.mark.parametrize("opted_in", [True, False])
def test_finished_event_for_a_separate_publisher_is_unchanged_by_the_opt_in(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, opted_in: bool
) -> None:
    """The subscription route keeps its fields; the record is one more key."""
    from preloop.services.event_webhooks import emitters

    if not opted_in:
        config = dict(world.flow.git_clone_config)
        config.pop("platform_review")
        world.flow.git_clone_config = config
        world.db.commit()
    execution = finished(world, identity(world, world.projects[0]))
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        emitters.outbox, "enqueue_event", lambda db, **kwargs: queued.append(kwargs)
    )
    emitters.emit_flow_execution_finished(
        world.db, execution, world.flow, status="SUCCEEDED"
    )
    assert len(queued) == 1
    data = queued[0]["data"]
    baseline = emitters.flow_execution_finished_data(
        execution, world.flow, status="SUCCEEDED", failure_category=None
    )
    assert {key: data[key] for key in baseline} == baseline
    assert set(data) - set(baseline) == {"platform_review"}
    assert (data["platform_review"] is not None) is opted_in
    assert world.github.calls == []


# --- review round 1 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_attempt_cancelled_after_the_claim_is_completed_by_the_next_sweep(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    project = world.projects[0]
    execution = finished(world, identity(world, project))
    real = platform_review._publish_claimed

    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(platform_review, "_publish_claimed", hang)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(publish(world, execution), timeout=0.05)
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    assert row.state == "failed" and row.reason == "attempt_interrupted"
    assert row.retryable is True and row.next_attempt_at is not None
    row.next_attempt_at = datetime(2020, 1, 1)
    world.db.commit()
    assert (world.account.id, execution.id) in store.due_execution_ids(world.db)
    monkeypatch.setattr(platform_review, "_publish_claimed", real)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_claim_abandoned_by_a_dead_worker_is_due_and_completed(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    store = crud.crud_platform_review_publication
    row = store.claim(
        world.db,
        account_id=world.account.id,
        execution_id=execution.id,
        repository=project.slug,
        pull_request=7,
        head_sha=HEAD,
    )
    assert store.due_execution_ids(world.db) == []
    row.claimed_at = datetime(2020, 1, 1)
    world.db.commit()
    assert store.due_execution_ids(world.db) == [(world.account.id, execution.id)]
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["attempts"] == 2
    # An abandoned row that used every attempt waits for an explicit call.
    row = store.get_for_execution(world.db, execution_id=execution.id)
    row.state, row.claimed_at = "publishing", datetime(2020, 1, 1)
    row.attempts = platform_review.MAX_AUTOMATIC_ATTEMPTS
    world.db.commit()
    assert store.due_execution_ids(world.db) == []


@pytest.mark.asyncio
async def test_retry_of_a_flow_that_opted_out_leaves_the_queue(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    world.github.fail[("GET", f"/repos/{project.slug}/pulls/7")] = [500]
    world.github.fail[("POST", f"/repos/{project.slug}/statuses/{HEAD}")] = [500]
    await publish(world, execution)
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    row.next_attempt_at = datetime(2020, 1, 1)
    config = dict(world.flow.git_clone_config)
    config.pop("platform_review")
    world.flow.git_clone_config = config
    world.db.commit()
    assert store.due_execution_ids(world.db) == [(world.account.id, execution.id)]
    calls = len(world.github.calls)
    assert await publish(world, execution) is None
    assert store.due_execution_ids(world.db) == []
    row = store.get_for_execution(world.db, execution_id=execution.id)
    assert (row.state, row.reason) == ("failed", "platform_review_disabled")
    assert row.retryable is False and row.next_attempt_at is None
    assert len(world.github.calls) == calls


def test_identity_narrowed_to_publish_only_can_still_republish(
    world: SimpleNamespace, client: TestClient
) -> None:
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=who.principal.id,
        grant=CiGrant(
            version=1,
            project_id=project.id,
            flow_id=world.flow.id,
            actions=(CiAction.PUBLISH_REVIEW,),
        ),
    )
    posted = client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review",
        headers={"Authorization": f"Bearer {who.token}"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["platform_review"]["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_identity_with_neither_trigger_nor_publish_cannot_cause_a_post(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=who.principal.id,
        grant=CiGrant(
            version=1,
            project_id=project.id,
            flow_id=world.flow.id,
            actions=(CiAction.READ_RESULT,),
        ),
    )
    outcome = await publish(world, execution, explicit=True)
    assert outcome is not None and outcome["reason"] == "admission_revoked"
    assert world.github.calls == []


def test_dispatch_authorization_accepts_only_trigger_or_publish(
    world: SimpleNamespace,
) -> None:
    execution = finished(world, identity(world, world.projects[0]))
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.authorize_dispatch(
            world.db, execution=execution, action=CiAction.READ_RESULT
        )


# --- posting settings never invalidate an identity -------------------------


def _set_review(world: SimpleNamespace, block: Any) -> None:
    config = dict(world.flow.git_clone_config)
    if block is None:
        config.pop("platform_review", None)
    else:
        config["platform_review"] = block
    world.flow.git_clone_config = config
    world.db.commit()


def _dedicated(world: SimpleNamespace) -> None:
    """Turn the fixture flow into a dedicated flow for the first project."""
    project = world.projects[0]
    config = dict(world.flow.git_clone_config)
    config.pop("ci_project_selection")
    config["repositories"] = [
        repo for repo in config["repositories"] if repo["project_id"] == str(project.id)
    ]
    world.flow.git_clone_config = config
    world.flow.trigger_project_ids = [str(project.id)]
    world.db.commit()


@pytest.mark.parametrize("shared", [True, False])
def test_existing_identity_survives_adding_changing_and_removing_posting(
    world: SimpleNamespace, client: TestClient, shared: bool
) -> None:
    if not shared:
        _dedicated(world)
    _set_review(world, None)
    who = identity(world, world.projects[0])
    assert "platform_review" not in who.principal.repository_binding
    execution = finished(world, who)
    headers = {"Authorization": f"Bearer {who.token}"}
    path = f"/api/v1/flows/executions/{execution.id}"
    blocks = [
        OPT_IN,
        {**OPT_IN, "verdict_actions": {"pass": "approve"}},
        {"enabled": True},
        {"enabled": False},
        None,
    ]
    for block in blocks:
        _set_review(world, block)
        assert crud.crud_ci_principal.authenticate(world.db, token=who.token)
        for suffix in ("", "/result"):
            response = client.get(path + suffix, headers=headers)
            assert response.status_code == 200, (block, response.text)
        # Dispatch (what a trigger and a launch re-check) still authorizes.
        assert crud.crud_ci_execution.authorize_dispatch(world.db, execution=execution)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False])
async def test_first_run_after_enabling_posts_with_the_existing_identity(
    world: SimpleNamespace, shared: bool
) -> None:
    if not shared:
        _dedicated(world)
    _set_review(world, None)
    project = world.projects[0]
    who = identity(world, project)
    _set_review(world, OPT_IN)
    execution = finished(world, who)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.parametrize(
    "recorded",
    [None, ["review:comment", "status"], ["review:approve", "review:comment"]],
)
def test_snapshot_written_by_an_earlier_release_is_honoured(
    world: SimpleNamespace, recorded: list[str] | None
) -> None:
    """1.0.x wrote no posting key; the first posting release wrote a list."""
    who = identity(world, world.projects[0])
    snapshot = dict(who.principal.repository_binding)
    assert set(snapshot) == {
        "slug",
        "tracker_type",
        "tracker_url",
        "organization_id",
        "publication_mode",
        "ci_project_selection",
    }
    if recorded is not None:
        snapshot["platform_review"] = recorded
    who.principal.repository_binding = snapshot
    world.db.commit()
    for block in (OPT_IN, None):
        _set_review(world, block)
        assert crud.crud_ci_principal.authenticate(world.db, token=who.token)


@pytest.mark.parametrize(
    "change",
    ["project_removed", "selection_mode", "publication_mode", "tracker", "slug"],
)
def test_what_must_invalidate_an_identity_still_does(
    world: SimpleNamespace, change: str
) -> None:
    project, other = world.projects
    who = identity(world, project)
    config = dict(world.flow.git_clone_config)
    if change == "project_removed":
        config["repositories"] = [
            r for r in config["repositories"] if r["project_id"] != str(project.id)
        ]
        world.flow.trigger_project_ids = [str(other.id)]
    elif change == "selection_mode":
        config.pop("ci_project_selection")
        config["repositories"] = [
            r for r in config["repositories"] if r["project_id"] == str(project.id)
        ]
        world.flow.trigger_project_ids = [str(project.id)]
    elif change == "publication_mode":
        config["publication_mode"] = "legacy"
        config.pop("ci_project_selection")
        config.pop("platform_review")
        config["repositories"] = [
            r for r in config["repositories"] if r["project_id"] == str(project.id)
        ]
        world.flow.trigger_project_ids = [str(project.id)]
    elif change == "tracker":
        config["repositories"] = [
            {**r, "tracker_id": str(uuid4())}
            if r["project_id"] == str(project.id)
            else r
            for r in config["repositories"]
        ]
    else:
        project.slug = "example/renamed"
    world.flow.git_clone_config = config
    world.db.commit()
    assert crud.crud_ci_principal.authenticate(world.db, token=who.token) is None


@pytest.mark.asyncio
async def test_broken_posting_settings_block_posting_but_not_the_identity(
    world: SimpleNamespace,
) -> None:
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    world.flow.validate_result = False
    world.db.commit()
    assert crud.crud_ci_principal.authenticate(world.db, token=who.token)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["reason"] == "configuration_invalid"
    assert world.github.calls == []
    world.flow.validate_result = True
    _set_review(world, {"enabled": True, "status": {"verdict_states": {}}})
    assert crud.crud_ci_principal.authenticate(world.db, token=who.token)
    again = await publish(world, execution, explicit=True)
    assert again is not None and again["reason"] == "configuration_invalid"
    assert world.github.calls == []


def test_publish_action_is_added_to_an_existing_key_in_place(
    world: SimpleNamespace, client: TestClient
) -> None:
    project = world.projects[0]
    base = (CiAction.TRIGGER, CiAction.READ_EXECUTION, CiAction.READ_RESULT)
    grant = CiGrant(
        version=1, project_id=project.id, flow_id=world.flow.id, actions=base
    )
    principal, key, token = crud.crud_ci_principal.provision(
        world.db, actor=world.owner, name="Existing", grant=grant
    )
    context = crud.crud_ci_principal.authenticate(world.db, token=token)
    execution = finished(world, SimpleNamespace(context=context))
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/flows/executions/{execution.id}/platform-review"
    assert client.post(path, headers=headers).status_code == 403
    wider = CiGrant(
        version=1,
        project_id=project.id,
        flow_id=world.flow.id,
        actions=(*base, CiAction.PUBLISH_REVIEW),
    )
    # Widening the grant alone does not widen a key that is already issued.
    crud.crud_ci_principal.change(
        world.db, actor=world.owner, principal_id=principal.id, grant=grant
    )
    assert client.post(path, headers=headers).status_code == 403
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=principal.id,
        grant=wider,
        extend_keys=True,
    )
    world.db.refresh(key)
    assert key.ci_actions == [a.value for a in (*base, CiAction.PUBLISH_REVIEW)]
    assert key.is_active is True
    posted = client.post(path, headers=headers)
    assert posted.status_code == 200, posted.text
    assert posted.json()["platform_review"]["status"] == "posted"
    # The same token still does what it did before.
    assert (
        client.get(path.removesuffix("/platform-review"), headers=headers).status_code
        == 200
    )


def test_extending_keys_needs_a_widening_grant_and_adds_nothing_else(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    base = (CiAction.TRIGGER, CiAction.READ_EXECUTION)
    grant = CiGrant(
        version=1, project_id=project.id, flow_id=world.flow.id, actions=base
    )
    principal, key, _ = crud.crud_ci_principal.provision(
        world.db,
        actor=world.owner,
        name="Narrow key",
        grant=grant,
        key_actions=(CiAction.TRIGGER,),
    )
    with pytest.raises(ValueError):
        crud.crud_ci_principal.change(
            world.db, actor=world.owner, principal_id=principal.id, extend_keys=True
        )
    # A grant that adds nothing extends nothing, and is not an error.
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=principal.id,
        grant=grant,
        enabled=True,
        extend_keys=True,
    )
    world.db.refresh(key)
    assert key.ci_actions == ["flow:trigger"]
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=principal.id,
        grant=CiGrant(
            version=1,
            project_id=project.id,
            flow_id=world.flow.id,
            actions=(*base, CiAction.PUBLISH_REVIEW),
        ),
        extend_keys=True,
    )
    world.db.refresh(key)
    # Only the newly granted action is added; the key's narrower ceiling for
    # actions the grant already had (execution:read) is kept.
    assert key.ci_actions == ["flow:trigger", "execution:review:publish"]
    # Repeating the documented call is a no-op, not an error.
    wider = CiGrant(
        version=1,
        project_id=project.id,
        flow_id=world.flow.id,
        actions=(*base, CiAction.PUBLISH_REVIEW),
    )
    crud.crud_ci_principal.change(
        world.db,
        actor=world.owner,
        principal_id=principal.id,
        grant=wider,
        extend_keys=True,
    )
    world.db.refresh(key)
    assert key.ci_actions == ["flow:trigger", "execution:review:publish"]


def test_sweep_is_bounded_by_when_posting_was_enabled_not_by_later_saves(
    world: SimpleNamespace,
) -> None:
    from preloop.models.schemas.flow import GitCloneConfig as Config

    who = identity(world, world.projects[0])
    before = finished(world, who)
    before.end_time = datetime(2019, 6, 1)
    after = finished(world, who)
    after.end_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    # Posting was enabled half an hour ago; the flow was saved again since
    # (an unrelated edit), after the run finished.
    config = dict(world.flow.git_clone_config)
    enabled = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
    config["platform_review"] = {**OPT_IN, "enabled_at": enabled.isoformat()}
    Config.model_validate(config)
    world.flow.git_clone_config = config
    world.flow.updated_at = datetime.now(timezone.utc) + timedelta(hours=1)
    world.db.commit()
    store = crud.crud_platform_review_publication
    found = store.unclaimed_execution_ids(
        world.db, finished_within=timedelta(days=36500)
    )
    assert (world.account.id, after.id) in found
    assert (world.account.id, before.id) not in found
    # A block stored without a stamp falls back to the flow's last save.
    config = dict(config)
    config["platform_review"] = OPT_IN
    world.flow.git_clone_config = config
    world.flow.updated_at = datetime.now(timezone.utc) + timedelta(hours=1)
    world.db.commit()
    assert (
        store.unclaimed_execution_ids(world.db, finished_within=timedelta(days=36500))
        == []
    )


def test_enabled_at_is_set_by_the_server_and_kept_across_saves() -> None:
    from preloop.models.schemas.flow import GitCloneConfig as Config

    submitted = Config.model_validate(
        _clone(platform_review={**OPT_IN, "enabled_at": "1999-01-01T00:00:00"})
    )
    platform_review.stamp_enabled_at(submitted, None)
    first = submitted.platform_review.enabled_at
    assert first is not None and not first.startswith("1999")
    stored = submitted.model_dump()
    # An unrelated save, with or without a client-sent value, keeps the stamp.
    for sent in (None, "2001-01-01T00:00:00"):
        again = Config.model_validate(
            _clone(platform_review={**OPT_IN, "enabled_at": sent})
        )
        platform_review.stamp_enabled_at(again, stored)
        assert again.platform_review.enabled_at == first
    # Off clears it; on again starts a new period.
    off = Config.model_validate(
        _clone(platform_review={"enabled": False, "enabled_at": first})
    )
    platform_review.stamp_enabled_at(off, stored)
    assert off.platform_review.enabled_at is None
    on = Config.model_validate(_clone())
    platform_review.stamp_enabled_at(on, off.model_dump())
    assert on.platform_review.enabled_at is not None
    platform_review.stamp_enabled_at(
        Config.model_validate(_clone(platform_review=None)), stored
    )
    platform_review.validate_platform_review_block(on.model_dump()["platform_review"])


def test_admin_update_extends_keys_and_requires_a_grant(
    world: SimpleNamespace,
) -> None:
    from pydantic import ValidationError

    from preloop.schemas.ci_administration import CiAdminUpdate

    with pytest.raises(ValidationError):
        CiAdminUpdate.model_validate({"extend_keys": True})
    project = world.projects[0]
    base = (CiAction.TRIGGER, CiAction.READ_RESULT)
    principal, key, _ = crud.crud_ci_principal.provision(
        world.db,
        actor=world.owner,
        name="Admin path",
        grant=CiGrant(
            version=1, project_id=project.id, flow_id=world.flow.id, actions=base
        ),
    )
    payload = CiAdminUpdate.model_validate(
        {
            "grant": {
                "version": 1,
                "project_id": str(project.id),
                "flow_id": str(world.flow.id),
                "actions": [*[a.value for a in base], "execution:review:publish"],
            },
            "extend_keys": True,
        }
    )
    crud.crud_ci_administration.change(
        world.db, actor=world.owner, principal_id=principal.id, payload=payload
    )
    world.db.refresh(key)
    assert key.ci_actions[-1] == "execution:review:publish"
    audited = (
        world.db.query(models.AuditLog)
        .filter(models.AuditLog.action == "ci_identity_extend_key")
        .count()
    )
    assert audited == 1


# --- found by the first live run -------------------------------------------


@pytest.mark.asyncio
async def test_record_of_a_run_without_a_review_says_status_only(
    world: SimpleNamespace,
) -> None:
    """No review plus a commit status is never reported as ``posted``."""
    project = world.projects[0]
    who = identity(world, project)
    failed_run = finished(world, who, status="FAILED", valid=None)
    outcome = await publish(world, failed_run)
    assert outcome is not None
    assert outcome["status"] == "status_only"
    assert outcome["reason"] == "run_not_succeeded"
    assert outcome["review"] == "skipped" and outcome["review_id"] is None
    assert outcome["commit_status"] == "error"
    assert outcome["posted_at"] is not None
    # Terminal like ``posted``: asking again changes and posts nothing.
    calls = len(world.github.calls)
    assert await publish(world, failed_run, explicit=True) == outcome
    assert len(world.github.calls) == calls
    # A moved head is the same shape: status on the reviewed commit only.
    world.github.pulls[(project.slug, 7)]["head"]["sha"] = OTHER_HEAD
    moved = await publish(world, finished(world, who))
    assert moved is not None
    assert (moved["status"], moved["reason"]) == ("status_only", "head_moved")
    assert moved["review"] == "skipped" and moved["commit_status"] == "failure"


@pytest.mark.asyncio
async def test_record_states_are_consistent_with_what_was_written(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    who = identity(world, project)
    both = await publish(world, finished(world, who))
    assert both is not None
    assert (both["status"], both["reason"]) == ("posted", None)
    assert both["review"] == "posted" and both["commit_status"] == "failure"
    _set_review(world, {"enabled": True})
    review_only = await publish(world, finished(world, who))
    assert review_only is not None
    assert (review_only["status"], review_only["reason"]) == ("posted", None)
    assert review_only["commit_status"] is None
    nothing = await publish(world, finished(world, who, status="FAILED"))
    assert nothing is not None
    assert (nothing["status"], nothing["reason"]) == ("skipped", "run_not_succeeded")
    assert nothing["review"] == "skipped" and nothing["commit_status"] is None
    assert nothing["posted_at"] is None


@pytest.mark.parametrize(
    "path",
    [
        "calculator.py",
        "src/my_module/file-name.test.py",
        "packages/@scope/pkg/src/[id]/page.tsx",
        "docs/a (copy)/READ_ME.md",
        "__init__.py",
    ],
)
def test_paths_render_in_code_spans_without_backslashes(path: str) -> None:
    body, _ = render_review(
        {
            "verdict": "fail",
            "summary": "See calculator.py and `add_numbers()` in my_module.",
            "findings": [{"title": "Check __init__.py", "path": path, "line": 3}],
        },
        verdict="fail",
        execution_id=uuid4(),
        head_sha=HEAD,
        nonce="n" * 32,
        inline={},
    )
    assert "\\" not in body
    assert f"`{path}:3`" in body
    assert "See calculator.py and `add_numbers()` in my" in body


def test_ordinary_text_is_left_readable() -> None:
    assert plain_text("calculator.py returns 1.5 (not 2).", 100) == (
        "calculator.py returns 1.5 (not 2)."
    )
    assert plain_text("Use `snake_case` here", 100) == "Use `snake_case` here"
    assert "\\" not in plain_text(HOSTILE, 4000)


def test_path_that_could_leave_a_code_span_is_rendered_as_text() -> None:
    body, _ = render_review(
        {
            "verdict": "fail",
            "findings": [{"title": "t", "path": "a`b` @octocat [x](y).py", "line": 1}],
        },
        verdict="fail",
        execution_id=uuid4(),
        head_sha=HEAD,
        nonce="n" * 32,
        inline={},
    )
    section = body.split("**Findings")[1]
    assert "`" not in section.replace("&#96;", "")
    assert "@octocat" not in section and "](" not in section


def _render(result: dict[str, Any], **kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
    return render_review(
        result,
        verdict=kwargs.pop("verdict", result.get("verdict", "fail")),
        execution_id=kwargs.pop("execution_id", uuid4()),
        head_sha=kwargs.pop("head_sha", HEAD),
        nonce=kwargs.pop("nonce", "n" * 32),
        inline=kwargs.pop("inline", {}),
        **kwargs,
    )


def test_old_field_names_render_as_they_do_today() -> None:
    """Title, body, path, line, summary and checked stay byte-identical."""
    execution_id = uuid4()
    body, comments = render_review(
        RESULT,
        verdict="fail",
        execution_id=execution_id,
        head_sha=HEAD,
        nonce="n" * 32,
        inline={"src/parser.py": {12}},
    )
    url = platform_review.execution_url(execution_id)
    expected = (
        "<!-- preloop-platform-review v1 "
        f"execution={execution_id} head={HEAD} nonce={'n' * 32} -->\n\n"
        "### Preloop review: fail\n\n"
        f"Reviewed commit `{HEAD}`. This review covers that commit only.\n\n"
        "**Summary**\n\n"
        "> One problem in the parser.\n\n"
        "**Findings (2)**\n\n"
        "**1&#46; &#91;high&#93; Unchecked length** (`src/parser.py:12`)\n\n"
        "> The length is read before it is validated.\n\n"
        "**2&#46; &#91;low&#93; Naming** (`src/other.py:400`)\n\n"
        "> Outside the diff.\n\n"
        "**What was checked**\n\n"
        "- correctness\n"
        "- security\n\n"
        f"Execution: {url}\n\n"
        "Posted by Preloop from the validated result of a read-only review run."
    )
    assert body == expected
    assert comments == [
        {
            "path": "src/parser.py",
            "line": 12,
            "side": "RIGHT",
            "body": (
                "**1&#46; &#91;high&#93; Unchecked length**\n\n"
                "> The length is read before it is validated."
            ),
        }
    ]


def test_problem_evidence_fix_and_location_are_posted() -> None:
    result = {
        "verdict": "changes_requested",
        "review": "Reserved stock can still be sold.",
        "findings": [
            {
                "severity": "MEDIUM",
                "problem": "A sale ignores reservations.",
                "evidence": "The count is written with no reservation check.",
                "fix": "Refuse the sale when reserved units cover it.",
                "location": "shop/inventory.py:17",
            }
        ],
    }
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string"},
                        "problem": {"type": "string"},
                        "evidence": {"type": "string"},
                        "fix": {"type": "string"},
                        "location": {"type": "string"},
                    },
                },
            }
        },
    }
    body, comments = _render(result, inline={"shop/inventory.py": {17}}, schema=schema)
    assert "Reserved stock can still be sold." in body
    assert "A sale ignores reservations." in body
    assert "**evidence**" in body
    assert "The count is written with no reservation check." in body
    assert "**fix**" in body
    assert "Refuse the sale when reserved units cover it." in body
    assert "`shop/inventory.py:17`" in body
    assert "Finding" not in body.split("**Findings")[1].split("**evidence**")[0]
    assert comments == [
        {
            "path": "shop/inventory.py",
            "line": 17,
            "side": "RIGHT",
            "body": (
                "**1&#46; &#91;MEDIUM&#93;**\n\n"
                "> A sale ignores reservations.\n\n"
                "**evidence**\n\n"
                "> The count is written with no reservation check.\n\n"
                "**fix**\n\n"
                "> Refuse the sale when reserved units cover it."
            ),
        }
    ]


def test_file_line_category_decimal_line_and_unparsed_location() -> None:
    body, comments = _render(
        {
            "verdict": "fail",
            "findings": [
                {
                    "title": "edge",
                    "file": "a.py",
                    "line": "2",
                    "category": "style",
                }
            ],
        },
        inline={"a.py": {2}},
    )
    assert "`a.py:2`" in body
    assert "**category**" in body and "style" in body
    assert comments[0]["line"] == 2 and comments[0]["path"] == "a.py"
    assert "**category**" in comments[0]["body"]

    bare, no_comments = _render(
        {
            "verdict": "fail",
            "findings": [
                {
                    "severity": "low",
                    "problem": "Noted.",
                    "location": "src/foo.py:line",
                }
            ],
        },
        inline={"src/foo.py": {1}},
    )
    assert "`src/foo.py:line`" in bare
    assert no_comments == []

    ranged, ranged_comments = _render(
        {
            "verdict": "fail",
            "findings": [
                {"severity": "low", "problem": "Span.", "location": "a.py:2-9"}
            ],
        },
        inline={"a.py": {2}},
    )
    assert "`a.py:2`" in ranged
    assert [(item["path"], item["line"]) for item in ranged_comments] == [("a.py", 2)]
    hashed, hashed_comments = _render(
        {
            "verdict": "fail",
            "findings": [{"severity": "low", "problem": "Hash.", "where": "a.py#L2"}],
        },
        inline={"a.py": {2}},
    )
    assert "`a.py:2`" in hashed
    assert hashed_comments[0]["line"] == 2


def test_object_field_is_counted_and_labels_are_escaped() -> None:
    counted, _ = _render(
        {"verdict": "fail", "findings": [{"severity": "low", "meta": {"a": 1}}]}
    )
    heading = counted.split("**Findings (1)**", 1)[1]
    assert "1&#46;" in heading
    assert "Finding" not in heading.split("Execution:")[0]
    assert "1 field not shown" in counted
    assert "nested" not in counted and '"a"' not in counted

    hostile = {
        "verdict": "fail",
        "findings": [
            {
                "severity": "low",
                "problem": HOSTILE,
                "evidence": HOSTILE,
                "location": "src/[x](https://evil.example).py:3",
                "@user": HOSTILE,
                "see #1 <b>": "click https://evil.example and close #1",
            }
        ],
    }
    body, comments = _render(hostile, inline={})
    assert comments == []
    rest = "\n".join(body.split("\n")[1:])
    assert "<" not in rest and "\\" not in rest
    shown = _visible(rest)
    assert not re.search(r"@[A-Za-z0-9]", shown)
    assert not re.search(r"#\d", shown)
    assert "](" not in re.sub(r"`[^`\n]*`", "", body)
    assert "https://evil" not in shown and "www.evil" not in shown
    assert "user" in shown and "click" in shown
    assert not any(line.startswith("/") for line in body.split("\n"))


def test_findings_cut_for_size_name_how_many_are_not_shown() -> None:
    findings = [
        {
            "severity": "low",
            "problem": "p" * 3000,
            "evidence": "e" * 3000,
        }
        for _ in range(20)
    ]
    body, _ = _render({"verdict": "fail", "findings": findings})
    rendered = body.count("&#46;")
    hidden = re.search(r"(\d+) further finding\(s\) are not shown", body)
    assert hidden is not None
    assert int(hidden.group(1)) == 20 - rendered
    assert rendered < 20
    assert "the execution holds the full result" in body
    assert "/console/flows/executions/" in body
    assert "text shortened" in body


def test_schema_order_wins_over_document_order() -> None:
    body, _ = _render(
        {
            "verdict": "fail",
            "findings": [
                {
                    "severity": "low",
                    "problem": "Text.",
                    "fix": "Later.",
                    "evidence": "Earlier.",
                }
            ],
        },
        schema={
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {},
                            "problem": {},
                            "evidence": {},
                            "fix": {},
                        },
                    },
                }
            },
        },
    )
    assert body.index("**evidence**") < body.index("**fix**")


def test_save_time_names_finding_fields_and_warns_when_none_are_text() -> None:
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string"},
                        "problem": {"type": "string"},
                        "evidence": {"type": "string"},
                        "location": {"type": "string"},
                    },
                },
            },
        },
    }
    appearances = platform_review.finding_field_appearances(schema)
    assert appearances == [
        {"field": "severity", "appears_as": "severity"},
        {"field": "problem", "appears_as": "text"},
        {"field": "evidence", "appears_as": "labelled block"},
        {"field": "location", "appears_as": "location"},
    ]
    flow = SimpleNamespace(
        git_clone_config={"platform_review": {"enabled": True}},
        result_schema=schema,
    )
    warnings = platform_review.review_posting_warnings(flow)
    assert warnings[0]["code"] == "review_finding_fields"
    assert "problem (text)" in warnings[0]["message"]
    bare = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"severity": {"type": "string"}, "meta": {}},
                },
            }
        },
    }
    missing = platform_review.review_posting_warnings(
        SimpleNamespace(
            git_clone_config={"platform_review": {"enabled": True}},
            result_schema=bare,
        )
    )
    assert missing[0]["code"] == "review_finding_text_missing"
    assert "no text field" in missing[0]["message"]
    # Information only: a schema with no finding text still passes the save check.
    checked = SimpleNamespace(
        git_clone_config={
            "publication_mode": "read_only",
            "platform_review": OPT_IN,
        },
        result_schema={
            **SCHEMA,
            "properties": {
                **SCHEMA["properties"],
                "findings": bare["properties"]["findings"],
            },
        },
        validate_result=True,
    )
    platform_review.validate_platform_review_flow(checked)


def _provider_creates(github: FakeGitHub) -> list[tuple[str, str]]:
    return [
        call
        for call in github.calls
        if call[0] == "POST"
        and (call[1].endswith("/reviews") or "/statuses/" in call[1])
    ]


@pytest.mark.asyncio
async def test_status_list_lag_then_visible_is_posted(
    world: SimpleNamespace, instant_readback: list[float]
) -> None:
    world.github.status_misses = 1
    execution = finished(world, identity(world, world.projects[0]))
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["readback_check"] is None
    assert instant_readback == [0.5]
    assert len(_provider_creates(world.github)) == 2


@pytest.mark.asyncio
async def test_review_get_not_found_then_found_is_posted(
    world: SimpleNamespace, instant_readback: list[float]
) -> None:
    world.github.review_misses = 1
    execution = finished(world, identity(world, world.projects[0]))
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert instant_readback == [0.5]
    assert world.github.revoked


@pytest.mark.asyncio
async def test_review_that_stays_invisible_is_pending_then_verified(
    world: SimpleNamespace,
    instant_readback: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = world.projects[0]
    world.github.review_hidden = True
    execution = finished(world, identity(world, project))
    outcome = await publish(world, execution)
    assert outcome is not None
    assert outcome["status"] == "failed"
    assert outcome["reason"] == "readback_pending"
    assert outcome["readback_check"] == "review_absent"
    assert outcome["review"] == "posted" and outcome["review_id"]
    assert outcome["retry_scheduled"] is True
    assert outcome["posted_at"] is None
    assert instant_readback == list(platform_review.READBACK_RETRY_DELAYS)
    assert str(execution.id) in caplog.text and "review_absent" in caplog.text
    assert "The length is read" not in caplog.text
    created = _provider_creates(world.github)
    assert world.github.revoked
    world.github.review_hidden = False
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    assert (world.account.id, execution.id) in store.due_execution_ids(world.db) or (
        row.retryable and row.reason == "readback_pending"
    )
    row.next_attempt_at = datetime(2020, 1, 1)
    world.db.commit()
    assert (world.account.id, execution.id) in store.due_execution_ids(world.db)
    again = await publish(world, execution)
    assert again is not None and again["status"] == "posted"
    assert again["posted_at"] is not None
    assert again["readback_check"] is None
    assert _provider_creates(world.github) == created
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert len(world.github.statuses[(project.slug, HEAD)]) == 1


@pytest.mark.asyncio
async def test_present_difference_stays_final(
    world: SimpleNamespace, instant_readback: list[float]
) -> None:
    project = world.projects[0]
    world.github.tamper_review_body = True
    execution = finished(world, identity(world, project))
    outcome = await publish(world, execution)
    assert outcome is not None
    assert outcome["reason"] == "readback_mismatch"
    assert outcome["readback_check"] == "review_body"
    assert outcome["retry_scheduled"] is False
    assert instant_readback == []
    store = crud.crud_platform_review_publication
    assert (world.account.id, execution.id) not in store.due_execution_ids(world.db)
    created = _provider_creates(world.github)
    again = await publish(world, execution)
    assert again is not None and again["reason"] == "readback_mismatch"
    assert again["readback_check"] == "review_body"
    # An automatic attempt does not take the row, so nothing new is created.
    assert _provider_creates(world.github) == created
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_explicit_repair_of_a_matching_mismatch_row_posts_nothing_new(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    posted = await publish(world, execution)
    assert posted is not None and posted["status"] == "posted"
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    store.record(
        world.db,
        row=row,
        state="failed",
        reason="readback_mismatch",
        retryable=False,
        posted_at=None,
        next_attempt_at=None,
        readback_check="review_actor",
    )
    created = _provider_creates(world.github)
    automatic = await publish(world, execution)
    assert automatic is not None and automatic["reason"] == "readback_mismatch"
    assert _provider_creates(world.github) == created
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None and repaired["status"] == "posted"
    assert repaired["posted_at"] is not None
    assert repaired["readback_check"] is None
    assert _provider_creates(world.github) == created
    assert len(world.github.reviews[(project.slug, 7)]) == 1
    assert world.github.revoked


@pytest.mark.asyncio
async def test_explicit_repair_of_a_different_body_stays_failed(
    world: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    await publish(world, execution)
    review = world.github.reviews[(project.slug, 7)][0]
    review["body"] = "different"
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    store.record(
        world.db,
        row=row,
        state="failed",
        reason="readback_mismatch",
        retryable=False,
        posted_at=None,
        readback_check="review_actor",
    )
    created = _provider_creates(world.github)
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None
    assert repaired["status"] == "failed"
    assert repaired["reason"] == "readback_mismatch"
    assert repaired["readback_check"] == "review_body"
    assert _provider_creates(world.github) == created
    assert "review_body" in caplog.text and str(execution.id) in caplog.text
    assert "different" not in caplog.text
    # A later automatic attempt still cannot clear it.
    again = await publish(world, execution)
    assert again is not None and again["reason"] == "readback_mismatch"
    assert again["readback_check"] == "review_body"
    assert _provider_creates(world.github) == created


def _legacy_mismatch(world: SimpleNamespace, execution: Any) -> None:
    """A mismatch recorded before ``readback_check`` existed."""
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    store.record(
        world.db,
        row=row,
        state="failed",
        reason="readback_mismatch",
        retryable=False,
        posted_at=None,
        next_attempt_at=None,
        readback_check=None,
    )


@pytest.mark.asyncio
async def test_legacy_mismatch_without_a_check_is_verified_when_it_matches(
    world: SimpleNamespace,
) -> None:
    """A pre-change mismatch row has no readback_check and must still be read."""
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    posted = await publish(world, execution)
    assert posted is not None and posted["status"] == "posted"
    _legacy_mismatch(world, execution)
    created = _provider_creates(world.github)
    seen = len(world.github.calls)
    repaired = await publish(world, execution, explicit=True)
    fresh = world.github.calls[seen:]
    assert any(method == "GET" and "/reviews/" in path for method, path in fresh)
    assert any(method == "GET" and path.endswith("/statuses") for method, path in fresh)
    assert repaired is not None and repaired["status"] == "posted"
    assert repaired["posted_at"] is not None
    assert repaired["readback_check"] is None
    assert _provider_creates(world.github) == created


@pytest.mark.asyncio
async def test_legacy_mismatch_with_a_different_body_stays_failed(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    await publish(world, execution)
    world.github.reviews[(project.slug, 7)][0]["body"] = "different"
    _legacy_mismatch(world, execution)
    created = _provider_creates(world.github)
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None
    assert repaired["status"] == "failed"
    assert repaired["reason"] == "readback_mismatch"
    assert repaired["readback_check"] == "review_body"
    assert _provider_creates(world.github) == created


@pytest.mark.asyncio
async def test_legacy_mismatch_whose_review_is_missing_is_not_posted(
    world: SimpleNamespace,
) -> None:
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    await publish(world, execution)
    _legacy_mismatch(world, execution)
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    store.record(world.db, row=row, review_id="424242")
    created = _provider_creates(world.github)
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None
    assert repaired["status"] != "posted"
    assert repaired["posted_at"] is None
    assert _provider_creates(world.github) == created


@pytest.mark.asyncio
async def test_repair_uses_the_sent_digest_when_the_console_url_changes(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = finished(world, identity(world, world.projects[0]))
    posted = await publish(world, execution)
    assert posted is not None and posted["status"] == "posted"
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    assert row.body_digest and row.status_target_url
    store.record(
        world.db,
        row=row,
        state="failed",
        reason="readback_mismatch",
        retryable=False,
        posted_at=None,
        readback_check=None,
    )
    monkeypatch.setattr(
        platform_review.settings, "preloop_url", "https://other.example"
    )
    created = _provider_creates(world.github)
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None and repaired["status"] == "posted"
    assert repaired["readback_check"] is None
    assert _provider_creates(world.github) == created


@pytest.mark.asyncio
async def test_mismatch_without_a_stored_digest_stays_failed_when_rerender_differs(
    world: SimpleNamespace,
) -> None:
    """A pre-digest row is not repaired when a fresh render differs."""
    execution = finished(world, identity(world, world.projects[0]))
    await publish(world, execution)
    review = world.github.reviews[(world.projects[0].slug, 7)][0]
    review["body"] = "different"
    store = crud.crud_platform_review_publication
    row = store.get_for_execution(world.db, execution_id=execution.id)
    store.record(
        world.db,
        row=row,
        state="failed",
        reason="readback_mismatch",
        retryable=False,
        posted_at=None,
        readback_check=None,
        body_digest=None,
    )
    created = _provider_creates(world.github)
    repaired = await publish(world, execution, explicit=True)
    assert repaired is not None
    assert repaired["status"] == "failed"
    assert repaired["reason"] == "readback_mismatch"
    assert repaired["readback_check"] == "review_body"
    assert _provider_creates(world.github) == created


# --- evidence gate ----------------------------------------------------------


def _set_evidence(world: SimpleNamespace, level: str | None) -> None:
    """Store platform_review.evidence. None leaves the key absent."""
    block = {**OPT_IN}
    if level is not None:
        block["evidence"] = level
    config = {**world.flow.git_clone_config, "platform_review": block}
    CRUDBase(models.Flow).update(
        world.db, db_obj=world.flow, obj_in={"git_clone_config": config}
    )
    world.db.refresh(world.flow)


def _age(world: SimpleNamespace, execution: Any, *, seconds: int = 31) -> None:
    """Move end_time outside the late-evidence window.

    The column is naive UTC. An aware value is shifted into the session time
    zone on the way in, which would leave the run inside the window.
    """
    execution.end_time = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).replace(tzinfo=None)
    world.db.commit()
    world.db.refresh(execution)


def _gateway_usage(world: SimpleNamespace, execution: Any) -> models.ApiUsage:
    """One gateway request row, with no evidence until a test stores it."""
    usage = models.ApiUsage(
        account_id=world.account.id,
        endpoint="/v1/responses",
        method="POST",
        status_code=200,
        duration=0.01,
        action_type="model_gateway",
        flow_id=world.flow.id,
        flow_execution_id=execution.id,
    )
    world.db.add(usage)
    world.db.commit()
    world.db.refresh(usage)
    return usage


def _store_gateway(
    world: SimpleNamespace, execution: Any, usage: Any, document: dict[str, Any]
) -> None:
    from preloop.models.crud.execution_tool_evidence import persist_tool_evidence

    persist_tool_evidence(world.db, usage=usage, document=document)
    world.db.commit()


def _complete_tool_document() -> dict[str, Any]:
    from preloop.services.execution_tool_evidence import capture_tools

    return capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"command":"git diff"}',
                }
            ]
        },
        capture_content=True,
    )


def _masked_tool_document() -> dict[str, Any]:
    from preloop.services.execution_tool_evidence import capture_tools

    return capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"command":"echo sk-1234567890abcd"}',
                }
            ]
        },
        capture_content=True,
    )


def _ready(
    world: SimpleNamespace,
    *,
    evidence: str | None = "captured",
    document: dict[str, Any] | None = None,
    usage: bool = True,
) -> tuple[Any, Any]:
    """A finished run, aged past the settle window, with optional evidence."""
    if evidence is not None:
        _set_evidence(world, evidence)
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    _age(world, execution)
    row = _gateway_usage(world, execution) if usage else None
    if document is not None and row is not None:
        _store_gateway(world, execution, row, document)
    return project, execution


@pytest.mark.asyncio
async def test_absent_evidence_setting_posts_as_before(world: SimpleNamespace) -> None:
    """A missing gateway record does not matter until the setting is on."""
    project, execution = _ready(world, evidence=None, document=None, usage=True)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["review"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_publish_reads_the_stored_records_once(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition, the plan and the returned record share one read."""
    _project, execution = _ready(world, document=_complete_tool_document())
    calls = 0
    real = platform_review.stored_tool_evidence

    def counted(db: Any, row: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return real(db, row)

    monkeypatch.setattr(platform_review, "stored_tool_evidence", counted)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert calls == 1


@pytest.mark.asyncio
async def test_captured_posts_when_records_are_complete(world: SimpleNamespace) -> None:
    project, execution = _ready(world, document=_complete_tool_document())
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["review"] == "posted"
    assert outcome["commit_status"] == "failure"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_captured_posts_when_the_only_incomplete_record_is_a_masked_credential(
    world: SimpleNamespace,
) -> None:
    document = _masked_tool_document()
    assert document["capture_complete"] is False
    assert document["incomplete_reasons"] == ["masking"]
    project, execution = _ready(world, document=document)
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert outcome["review"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        ("missing", "missing gateway record"),
        ("failed", "capture-failure marker"),
        ("truncated", "truncated record"),
        ("legacy", "old incomplete record"),
    ],
)
async def test_captured_refuses_incomplete_evidence(
    world: SimpleNamespace, mutate: str, label: str
) -> None:
    """Each non-masking gap refuses the review and sets the incomplete status."""
    del label
    document = _complete_tool_document()
    store = mutate != "missing"
    if mutate == "failed":
        document = {
            "version": 1,
            "capture_complete": False,
            "capture_failed": True,
            "content_capture_enabled": True,
            "observations": [],
        }
    elif mutate == "truncated":
        document = {
            **document,
            "capture_complete": False,
            "incomplete_reasons": ["truncated"],
        }
    elif mutate == "legacy":
        document = {**document, "capture_complete": False}
        document.pop("incomplete_reasons", None)
    project, execution = _ready(world, document=document if store else None)
    outcome = await publish(world, execution)
    assert outcome is not None
    assert outcome["review"] == "skipped"
    assert outcome["review_reason"] == "evidence_incomplete"
    assert outcome["reason"] == "evidence_incomplete"
    assert outcome["commit_status"] == "error"
    assert world.github.reviews == {}
    assert world.github.statuses[(project.slug, HEAD)][0]["state"] == "error"
    counts = outcome["tool_evidence"]
    assert set(counts) == {
        "gateway_requests",
        "missing_records",
        "records",
        "incomplete_records",
        "masking_only_records",
        "masking_by_rule",
    }
    if mutate == "missing":
        assert counts["missing_records"] == 1


@pytest.mark.asyncio
async def test_complete_refuses_a_masked_credential(world: SimpleNamespace) -> None:
    project, execution = _ready(
        world, evidence="complete", document=_masked_tool_document()
    )
    del project
    outcome = await publish(world, execution)
    assert outcome is not None and outcome["reason"] == "evidence_incomplete"
    assert outcome["review"] == "skipped"
    assert world.github.reviews == {}
    assert world.github.statuses[(world.projects[0].slug, HEAD)][0]["state"] == "error"


@pytest.mark.asyncio
async def test_automatic_sweep_and_explicit_publish_agree_on_the_evidence_gate(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Automatic, the sweep and an explicit call apply the same refusal."""
    from preloop.models.db import session as db_session_module

    _set_evidence(world, "captured")
    project = world.projects[0]
    who = identity(world, project)
    document = {**_complete_tool_document(), "capture_complete": False}
    document["incomplete_reasons"] = ["truncated"]

    async def refused(explicit: bool = False) -> dict[str, Any]:
        execution = finished(world, who)
        _age(world, execution, seconds=180)
        usage = _gateway_usage(world, execution)
        _store_gateway(world, execution, usage, document)
        outcome = await publish(world, execution, explicit=explicit)
        assert outcome is not None and outcome["reason"] == "evidence_incomplete"
        assert outcome["review"] == "skipped"
        return outcome

    await refused(False)
    await refused(True)
    execution = finished(world, who)
    _age(world, execution, seconds=180)
    usage = _gateway_usage(world, execution)
    _store_gateway(world, execution, usage, document)
    world.flow.updated_at = datetime(2020, 1, 1)
    world.db.commit()
    close = world.db.close
    monkeypatch.setattr(world.db, "close", lambda: None)
    monkeypatch.setattr(db_session_module, "get_db_session", lambda: iter([world.db]))
    from preloop.services.platform_review import sweep_platform_reviews

    await sweep_platform_reviews()
    monkeypatch.setattr(world.db, "close", close)
    row = crud.crud_platform_review_publication.get_for_execution(
        world.db, execution_id=execution.id
    )
    assert row is not None and row.reason == "evidence_incomplete"
    assert row.review_state == "skipped"
    assert world.github.reviews == {}


@pytest.mark.asyncio
async def test_later_publish_succeeds_once_nothing_is_missing(
    world: SimpleNamespace,
) -> None:
    project, execution = _ready(world, document=None, usage=True)
    refused = await publish(world, execution, explicit=True)
    assert refused is not None and refused["reason"] == "evidence_incomplete"
    assert world.github.reviews == {}
    usage = (
        world.db.query(models.ApiUsage)
        .filter(models.ApiUsage.flow_execution_id == execution.id)
        .one()
    )
    _store_gateway(world, execution, usage, _complete_tool_document())
    posted = await publish(world, execution, explicit=True)
    assert posted is not None and posted["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


@pytest.mark.asyncio
async def test_late_gateway_evidence_is_not_refused_or_posted_early(
    world: SimpleNamespace,
) -> None:
    """A run that just finished is neither posted nor refused as incomplete.

    Streaming evidence is committed after the body, so it can land after
    end_time. The gate waits out that window.
    """
    _set_evidence(world, "captured")
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    usage = _gateway_usage(world, execution)
    early = await publish(world, execution)
    assert early is None
    assert world.github.reviews == {}
    assert (
        crud.crud_platform_review_publication.get_for_execution(
            world.db, execution_id=execution.id
        )
        is None
    )
    _store_gateway(world, execution, usage, _complete_tool_document())
    _age(world, execution)
    posted = await publish(world, execution)
    assert posted is not None and posted["status"] == "posted"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


def test_status_projection_does_not_read_tool_evidence(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list and a status poll must not decode the stored records."""
    execution = finished(world, identity(world, world.projects[0]))

    def scanned(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("tool evidence was read")

    monkeypatch.setattr(platform_review, "stored_tool_evidence", scanned)
    record = platform_review.platform_review_projection(
        world.db, execution, flow=world.flow
    )
    assert record is not None
    assert "tool_evidence" not in record


def test_tool_evidence_summary_counts_match(
    world: SimpleNamespace, client: TestClient
) -> None:
    """One call returns the counts, and another identity cannot read them."""
    _set_evidence(world, "captured")
    project_a, project_b = world.projects
    who = identity(world, project_a)
    other = identity(world, project_b)
    execution = finished(world, who)
    _age(world, execution)
    first = _gateway_usage(world, execution)
    _gateway_usage(world, execution)
    _store_gateway(world, execution, first, _masked_tool_document())
    headers = {"Authorization": f"Bearer {who.token}"}
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result", headers=headers
    )
    assert response.status_code == 200, response.text
    counts = response.json()["tool_evidence"]
    assert counts["gateway_requests"] == 2
    assert counts["missing_records"] == 1
    # The inspecting native command plus the one gateway record.
    assert counts["records"] == 2
    assert counts["incomplete_records"] == 1
    assert counts["masking_only_records"] == 1
    assert counts["masking_by_rule"]
    assert response.json()["platform_review"]["tool_evidence"] == counts
    blob = json.dumps(counts)
    assert "sk-" not in blob and "git diff" not in blob and "/" not in blob
    denied = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {other.token}"},
    )
    assert denied.status_code == 404
    assert "masking_by_rule" not in denied.text


def test_evidence_values_are_validated_and_content_capture_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown values are rejected. The gate is rejected when it cannot pass."""
    from preloop.config import settings

    with pytest.raises(ValueError):
        GitCloneConfig.model_validate(
            _clone(platform_review={**OPT_IN, "evidence": "sometimes"})
        )
    with pytest.raises(ValueError, match="evidence"):
        platform_review.validate_platform_review_block({**OPT_IN, "evidence": "full"})
    accepted = GitCloneConfig.model_validate(
        _clone(platform_review={**OPT_IN, "evidence": "captured"})
    )
    assert accepted.platform_review.evidence == "captured"
    flow = SimpleNamespace(
        git_clone_config=_clone(platform_review={**OPT_IN, "evidence": "complete"}),
        result_schema=SCHEMA,
        validate_result=True,
    )
    monkeypatch.setattr(settings, "model_gateway_capture_content", False)
    # Publish keeps evaluating the stored records. Only the save path
    # rejects a gate that the instance cannot satisfy.
    platform_review.validate_platform_review_flow(flow)
    with pytest.raises(ValueError, match="content capture"):
        platform_review.validate_platform_review_flow(
            flow, enforce_content_capture=True
        )
    monkeypatch.setattr(settings, "model_gateway_capture_content", True)
    platform_review.validate_platform_review_flow(flow, enforce_content_capture=True)
