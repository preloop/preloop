"""Manual review posting and the caller preconditions of the publish call.

Builds on the fake GitHub and the typed PostgreSQL rows of
``test_platform_review``: no request leaves the process, and every test
asserts which writes reached the provider.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.schemas.flow import GitCloneConfig
from preloop.services import platform_review
from preloop.services.platform_review import (
    PreconditionFailedError,
    PreconditionMalformedError,
    ReviewPreconditions,
    parse_preconditions,
    platform_review_mode,
    sweep_platform_reviews,
)
from preloop.services.review_completion_evidence import result_sha256
from tests.services import test_platform_review as base
from tests.services.test_platform_review import (
    HEAD,
    OPT_IN,
    OTHER_HEAD,
    RESULT,
    FakeGitHub,
    _clone,
    _orchestrator,
    finished,
    identity,
    publish,
)

# Fixtures of the base suite, bound here so pytest finds them.
github = base.github
world = base.world
client = base.client
human_client = base.human_client
reset_ci_authority = base.reset_ci_authority

BASE = "b" * 40
REVIEW_POST = "/repos/{slug}/pulls/7/reviews"


def set_mode(world: SimpleNamespace, mode: str | None, *, enabled: bool = True) -> None:
    """Store the flow's platform review block with the given mode."""
    block: dict[str, Any] = {**OPT_IN, "enabled": enabled}
    if mode is not None:
        block["mode"] = mode
    config = {**world.flow.git_clone_config, "platform_review": block}
    CRUDBase(models.Flow).update(
        world.db, db_obj=world.flow, obj_in={"git_clone_config": config}
    )
    world.db.refresh(world.flow)


def ci_post(
    client: TestClient, who: SimpleNamespace, execution: Any, body: Any = None
) -> Any:
    kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {who.token}"}}
    if body is not None:
        kwargs["json"] = body
    return client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review", **kwargs
    )


def all_matching(world: SimpleNamespace, execution: Any) -> dict[str, Any]:
    world.db.refresh(world.flow)
    return {
        "expected_head_sha": HEAD,
        "expected_base_sha": BASE,
        "expected_result_sha256": result_sha256(execution.result),
        "expected_flow_revision": world.flow.config_revision,
    }


def audit_entries(world: SimpleNamespace, execution: Any) -> list[models.AuditLog]:
    return (
        world.db.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "platform_review_publish",
            models.AuditLog.resource_id == str(execution.id),
        )
        .order_by(models.AuditLog.timestamp.asc())
        .all()
    )


# --- stored shape -----------------------------------------------------------


def test_stored_shape_keeps_existing_blocks_automatic() -> None:
    assert platform_review_mode(None) == "off"
    assert platform_review_mode({"platform_review": {"enabled": False}}) == "off"
    assert platform_review_mode({"platform_review": {"enabled": True}}) == "automatic"
    assert (
        platform_review_mode({"platform_review": {"enabled": True, "mode": "manual"}})
        == "manual"
    )
    # Off wins over a stored manual mode.
    assert (
        platform_review_mode({"platform_review": {"enabled": False, "mode": "manual"}})
        == "off"
    )
    platform_review.validate_platform_review_block({**OPT_IN, "mode": "manual"})
    with pytest.raises(ValueError, match="mode"):
        platform_review.validate_platform_review_block({**OPT_IN, "mode": "later"})


def test_api_schema_accepts_the_mode_and_defaults_to_automatic() -> None:
    legacy = GitCloneConfig.model_validate(_clone())
    assert legacy.platform_review is not None
    assert legacy.platform_review.mode == "automatic"
    manual = GitCloneConfig.model_validate(
        _clone(platform_review={**OPT_IN, "mode": "manual"})
    )
    assert manual.platform_review is not None
    assert manual.platform_review.mode == "manual"
    with pytest.raises(ValueError):
        GitCloneConfig.model_validate(
            _clone(platform_review={**OPT_IN, "mode": "sometimes"})
        )


# --- each mode when the execution finishes ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "enabled", "posts"),
    [(None, True, True), ("automatic", True, True), ("manual", True, False)]
    + [("automatic", False, False)],
)
async def test_execution_finish_posts_only_in_automatic_mode(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None,
    enabled: bool,
    posts: bool,
) -> None:
    from preloop.models.db import session as db_session_module

    set_mode(world, mode, enabled=enabled)
    project = world.projects[0]
    execution = finished(world, identity(world, project))
    monkeypatch.setattr(world.db, "close", lambda: None)
    monkeypatch.setattr(db_session_module, "get_db_session", lambda: iter([world.db]))
    await _orchestrator(world.flow, execution)._publish_platform_review()
    if posts:
        assert len(world.github.reviews[(project.slug, 7)]) == 1
        world.db.refresh(execution)
        record = platform_review.platform_review_projection(
            world.db, execution, flow=world.flow
        )
        assert record is not None
        assert record["posted_by"] == "automatic"
        assert record["mode"] == "automatic"
    else:
        assert world.github.calls == []
        assert (
            crud.crud_platform_review_publication.get_for_execution(
                world.db, execution_id=execution.id
            )
            is None
        )


@pytest.mark.asyncio
async def test_sweep_never_posts_for_a_manual_flow(world: SimpleNamespace) -> None:
    from datetime import datetime, timedelta, timezone

    set_mode(world, "manual")
    execution = finished(world, identity(world, world.projects[0]))
    CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={"end_time": datetime.now(timezone.utc) - timedelta(minutes=10)},
    )
    store = crud.crud_platform_review_publication
    found = store.unclaimed_execution_ids(world.db, finished_within=timedelta(hours=24))
    assert (world.account.id, execution.id) not in found
    # The automatic entry point leaves a manual flow alone as well.
    assert await publish(world, execution) is None
    assert world.github.calls == []


@pytest.mark.asyncio
async def test_retry_left_from_automatic_mode_stops_when_the_flow_turns_manual(
    world: SimpleNamespace,
) -> None:
    execution = finished(world, identity(world, world.projects[0]))
    world.github.fail[("GET", f"/repos/{world.projects[0].slug}/pulls/7")] = [502]
    failed = await publish(world, execution)
    assert failed is not None and failed["retry_scheduled"] is True
    set_mode(world, "manual")
    assert await publish(world, execution) is None
    row = crud.crud_platform_review_publication.get_for_execution(
        world.db, execution_id=execution.id
    )
    assert row is not None and row.retryable is False
    assert row.reason == "manual_mode"
    assert world.github.reviews == {}


@pytest.mark.asyncio
async def test_failed_manual_attempt_is_not_retried_automatically(
    world: SimpleNamespace,
) -> None:
    set_mode(world, "manual")
    execution = finished(world, identity(world, world.projects[0]))
    world.github.fail[("GET", f"/repos/{world.projects[0].slug}/pulls/7")] = [502]
    failed = await publish(world, execution, explicit=True)
    assert failed is not None and failed["status"] == "failed"
    assert failed["retry_scheduled"] is False
    assert crud.crud_platform_review_publication.due_execution_ids(world.db) == []
    again = await publish(world, execution, explicit=True)
    assert again is not None and again["status"] == "posted"
    assert again["posted_by"] == "manual"


# --- the explicit call in each mode -----------------------------------------


@pytest.mark.parametrize(
    ("mode", "enabled", "status_code"),
    [("automatic", True, 200), ("manual", True, 200), ("manual", False, 409)],
)
def test_explicit_call_in_each_mode(
    world: SimpleNamespace,
    client: TestClient,
    mode: str,
    enabled: bool,
    status_code: int,
) -> None:
    set_mode(world, mode, enabled=enabled)
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    response = ci_post(client, who, execution)
    assert response.status_code == status_code, response.text
    if status_code == 409:
        assert response.json()["detail"]["code"] == "platform_review_disabled"
        assert world.github.calls == []
        return
    record = response.json()["platform_review"]
    assert record["status"] == "posted"
    assert record["mode"] == mode
    assert record["posted_by"] == "manual"
    assert len(world.github.reviews[(project.slug, 7)]) == 1


def test_manual_mode_record_reads_pending_until_the_explicit_call(
    world: SimpleNamespace, client: TestClient
) -> None:
    from preloop.services.event_webhooks import emitters

    set_mode(world, "manual")
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    event = emitters._platform_review(world.db, execution, world.flow)
    assert event is not None
    assert (event["mode"], event["posted_by"], event["status"]) == (
        "manual",
        "none",
        "pending",
    )
    ci_post(client, who, execution)
    world.db.refresh(execution)
    event = emitters._platform_review(world.db, execution, world.flow)
    assert event is not None
    assert (event["mode"], event["posted_by"], event["status"]) == (
        "manual",
        "manual",
        "posted",
    )


# --- preconditions ----------------------------------------------------------


def test_all_preconditions_matching_post_once_and_repeat_is_idempotent(
    world: SimpleNamespace, client: TestClient
) -> None:
    set_mode(world, "manual")
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    body = all_matching(world, execution)
    first = ci_post(client, who, execution, body)
    assert first.status_code == 200, first.text
    record = first.json()["platform_review"]
    assert record["status"] == "posted" and record["head_sha"] == HEAD
    second = ci_post(client, who, execution, body)
    assert second.status_code == 200, second.text
    assert second.json()["platform_review"] == record
    assert (
        world.github.writes.count(("POST", REVIEW_POST.format(slug=project.slug))) == 1
    )
    entries = audit_entries(world, execution)
    assert [(e.status, e.details["posted_by"]) for e in entries] == [
        ("success", "manual"),
    ]
    assert entries[0].details["mode"] == "manual"


@pytest.mark.parametrize(
    ("field", "value", "current"),
    [
        ("expected_head_sha", OTHER_HEAD, HEAD),
        ("expected_base_sha", "d" * 40, BASE),
        ("expected_result_sha256", "e" * 64, None),
        ("expected_flow_revision", 99, None),
    ],
)
def test_each_mismatching_precondition_refuses_with_412_and_can_be_corrected(
    world: SimpleNamespace,
    client: TestClient,
    field: str,
    value: Any,
    current: Any,
) -> None:
    set_mode(world, "manual")
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    body = all_matching(world, execution)
    expected_current = current if current is not None else body[field]
    refused = ci_post(client, who, execution, {**body, field: value})
    assert refused.status_code == 412, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "review_precondition_failed"
    assert detail["precondition"] == field
    assert detail["expected"] == value
    assert detail["current"] == expected_current
    assert detail["failed"] == [
        {
            "precondition": field,
            "expected": value,
            "current": expected_current,
            "source": {
                "expected_head_sha": "binding",
                "expected_base_sha": "binding",
                "expected_result_sha256": "result",
                "expected_flow_revision": "flow",
            }[field],
        }
    ]
    assert world.github.calls == []
    record = client.get(
        f"/api/v1/flows/executions/{execution.id}",
        headers={"Authorization": f"Bearer {who.token}"},
    ).json()["platform_review"]
    assert record["status"] != "posted" and record["posted_by"] == "none"
    corrected = ci_post(client, who, execution, body)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["platform_review"]["status"] == "posted"
    entries = audit_entries(world, execution)
    assert [e.status for e in entries] == ["denied", "success"]
    assert entries[0].details["preconditions_failed"][0]["precondition"] == field


def test_every_failed_precondition_is_named(
    world: SimpleNamespace, client: TestClient
) -> None:
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    refused = ci_post(
        client,
        who,
        execution,
        {"expected_head_sha": OTHER_HEAD, "expected_flow_revision": 42},
    )
    assert refused.status_code == 412
    assert [item["precondition"] for item in refused.json()["detail"]["failed"]] == [
        "expected_head_sha",
        "expected_flow_revision",
    ]


def test_flow_changed_after_the_caller_reviewed_it_refuses(
    world: SimpleNamespace, client: TestClient
) -> None:
    set_mode(world, "manual")
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    body = all_matching(world, execution)
    seen = body["expected_flow_revision"]
    CRUDBase(models.Flow).update(
        world.db, db_obj=world.flow, obj_in={"prompt_template": "Review it again"}
    )
    world.db.refresh(world.flow)
    assert world.flow.config_revision == seen + 1
    refused = ci_post(client, who, execution, body)
    assert refused.status_code == 412
    assert refused.json()["detail"]["current"] == seen + 1
    assert world.github.calls == []


@pytest.mark.parametrize(
    ("live_side", "field"),
    [("head", "expected_head_sha"), ("base", "expected_base_sha")],
)
def test_commit_moved_between_the_check_and_the_post_writes_nothing(
    world: SimpleNamespace, client: TestClient, live_side: str, field: str
) -> None:
    set_mode(world, "manual")
    project = world.projects[0]
    who = identity(world, project)
    execution = finished(world, who)
    body = all_matching(world, execution)
    # Stored values agree; the live pull request moved on.
    world.github.pulls[(project.slug, 7)][live_side]["sha"] = "f" * 40
    refused = ci_post(client, who, execution, body)
    assert refused.status_code == 412, refused.text
    detail = refused.json()["detail"]
    assert detail["precondition"] == field
    assert detail["current"] == "f" * 40
    assert detail["failed"][0]["source"] == "live"
    # Only the lease revocation; nothing was written to the repository.
    assert [c for c in world.github.writes if c[1].startswith("/repos/")] == []
    assert world.github.reviews == {}
    assert world.github.statuses == {}
    # The lease minted for the attempt was revoked.
    assert world.github.revoked and len(world.github.revoked) == len(
        world.github.tokens
    )
    row = crud.crud_platform_review_publication.get_for_execution(
        world.db, execution_id=execution.id
    )
    assert row is not None
    assert (row.state, row.reason, row.review_state, row.posted_at) == (
        "failed",
        "precondition_failed",
        None,
        None,
    )
    assert row.status_state in {None, "skipped"}
    # Once the head is back, the same call posts.
    world.github.pulls[(project.slug, 7)][live_side]["sha"] = (
        HEAD if live_side == "head" else BASE
    )
    posted = ci_post(client, who, execution, body)
    assert posted.status_code == 200, posted.text
    assert posted.json()["platform_review"]["status"] == "posted"


@pytest.mark.asyncio
async def test_result_changed_after_the_first_check_is_caught_at_post_time(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored checks run again on the re-read rows inside the attempt."""
    set_mode(world, "manual")
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    body = all_matching(world, execution)
    original = platform_review.check_stored_preconditions
    calls = {"n": 0}

    def change_after_first(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 2:
            CRUDBase(models.FlowExecution).update(
                world.db,
                db_obj=execution,
                obj_in={"result": {**RESULT, "summary": "Changed underneath."}},
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        platform_review, "check_stored_preconditions", change_after_first
    )
    with pytest.raises(PreconditionFailedError) as caught:
        await platform_review.publish_platform_review(
            world.db,
            account_id=world.account.id,
            execution_id=execution.id,
            explicit=True,
            preconditions=parse_preconditions(body),
        )
    assert calls["n"] == 2
    assert caught.value.failures[0]["precondition"] == "expected_result_sha256"
    assert world.github.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"expected_head_sha": "abc"},
        {"expected_head_sha": "g" * 40},
        {"expected_base_sha": 12},
        {"expected_result_sha256": "a" * 63},
        {"expected_result_sha256": "sha256:" + "a" * 64},
        {"expected_flow_revision": 0},
        {"expected_flow_revision": "3"},
        {"expected_flow_revision": True},
        {"expected_head": HEAD},
        ["expected_head_sha"],
    ],
)
def test_malformed_precondition_is_400_and_posts_nothing(
    world: SimpleNamespace, client: TestClient, body: Any
) -> None:
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    response = ci_post(client, who, execution, body)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "review_precondition_malformed"
    assert world.github.calls == []


def test_uppercase_sha_and_digest_are_accepted() -> None:
    parsed = parse_preconditions(
        {"expected_head_sha": HEAD.upper(), "expected_result_sha256": "A" * 64}
    )
    assert parsed == ReviewPreconditions(head_sha=HEAD, result_sha256="a" * 64)
    with pytest.raises(PreconditionMalformedError):
        parse_preconditions("not an object")


# --- the digest ---------------------------------------------------------------


def test_digest_is_stable_across_key_order_and_whitespace() -> None:
    text_a = '{"verdict": "pass", "summary": "ok", "findings": [{"b": 1, "a": 2}]}'
    text_b = (
        '{\n  "findings" : [ { "a" : 2 , "b" : 1 } ],\n'
        '  "summary":"ok",\n  "verdict":"pass"\n}'
    )
    a, b = json.loads(text_a), json.loads(text_b)
    assert list(a) != list(b)
    assert result_sha256(a) == result_sha256(b)
    # The published canonical form, computed independently.
    canonical = '{"findings":[{"a":2,"b":1}],"summary":"ok","verdict":"pass"}'
    assert result_sha256(a) == hashlib.sha256(canonical.encode()).hexdigest()
    # Non-ASCII text is hashed as UTF-8, not as escapes.
    text = {"summary": "Größe"}
    assert (
        result_sha256(text)
        == hashlib.sha256('{"summary":"Größe"}'.encode("utf-8")).hexdigest()
    )
    # Controller-private members never contribute.
    assert result_sha256({**a, "_private_publication": {"x": 1}}) == result_sha256(a)
    assert result_sha256({**a, "summary": "changed"}) != result_sha256(a)


def test_result_endpoint_exposes_the_digest_the_publish_call_compares(
    world: SimpleNamespace, client: TestClient
) -> None:
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    headers = {"Authorization": f"Bearer {who.token}"}
    read = client.get(
        f"/api/v1/flows/executions/{execution.id}/result", headers=headers
    )
    assert read.status_code == 200, read.text
    payload = read.json()
    canonical = json.dumps(
        payload["result"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert payload["result_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert (
        payload["completion"]["result_captured"]["result_sha256"]
        == payload["result_sha256"]
    )
    posted = ci_post(
        client, who, execution, {"expected_result_sha256": payload["result_sha256"]}
    )
    assert posted.status_code == 200, posted.text


def test_account_user_reads_the_same_digest_and_posts_with_preconditions(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    set_mode(world, "manual")
    who = identity(world, world.projects[0])
    execution = finished(world, who)
    read = human_client.get(f"/api/v1/flows/executions/{execution.id}/result")
    assert read.status_code == 200, read.text
    assert read.json()["result_sha256"] == result_sha256(RESULT)
    posted = human_client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review",
        json=all_matching(world, execution),
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["platform_review"]["posted_by"] == "manual"
    entry = audit_entries(world, execution)[-1]
    assert entry.user_id == world.owner.id


# --- restricted CI identities ---------------------------------------------------


def test_ci_identity_cannot_post_another_identity_execution_even_with_preconditions(
    world: SimpleNamespace, client: TestClient
) -> None:
    set_mode(world, "manual")
    who_a = identity(world, world.projects[0])
    who_b = identity(world, world.projects[1])
    run_b = finished(world, who_b)
    denied = ci_post(client, who_a, run_b, all_matching(world, run_b))
    assert denied.status_code == 404
    assert world.github.calls == []


def test_ci_identity_cannot_switch_the_posting_mode(
    world: SimpleNamespace, client: TestClient
) -> None:
    set_mode(world, "manual")
    who = identity(world, world.projects[0])
    response = client.put(
        f"/api/v1/flows/{world.flow.id}",
        headers={"Authorization": f"Bearer {who.token}"},
        json={
            "git_clone_config": {
                **world.flow.git_clone_config,
                "platform_review": {**OPT_IN, "mode": "automatic"},
            }
        },
    )
    assert response.status_code in {401, 403, 404, 405}, response.text
    world.db.refresh(world.flow)
    assert platform_review_mode(world.flow.git_clone_config) == "manual"


@pytest.mark.asyncio
async def test_sweep_pass_leaves_manual_runs_unposted(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    from datetime import datetime, timedelta, timezone

    from preloop.models.db import session as db_session_module

    set_mode(world, "manual")
    execution = finished(world, identity(world, world.projects[0]))
    CRUDBase(models.FlowExecution).update(
        world.db,
        db_obj=execution,
        obj_in={"end_time": datetime.now(timezone.utc) - timedelta(minutes=10)},
    )
    monkeypatch.setattr(world.db, "close", lambda: None)
    monkeypatch.setattr(db_session_module, "get_db_session", lambda: iter([world.db]))
    await sweep_platform_reviews()
    assert world.github.calls == []
    assert isinstance(world.github, FakeGitHub)
