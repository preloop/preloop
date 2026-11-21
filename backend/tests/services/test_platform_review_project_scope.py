"""``platform_review.project_ids``: post for, and start from events for, some projects.

A shared read-only review flow admits several projects. The optional list
names the ones Preloop posts for. Because an event-started review exists only
as a posting run, the same list decides where a pull request event starts a
review. Unset keeps today's behaviour for every project.

Every resource is synthetic; GitHub is an in-memory fake.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints.flows import _reject_invalid_platform_review
from preloop.models import crud, models
from preloop.models.schemas.flow import GitCloneConfig
from preloop.schemas.ci_execution import CiReviewBinding
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.services import platform_review
from preloop.services.platform_review import (
    PlatformReviewRefusedError,
    review_posting_warnings,
)
from tests.services.test_event_review_posting import (  # noqa: F401
    ALLOWLIST,
    BASE,
    HEAD,
    _document,
    _event,
    _rows,
    account_owner,
    arm,
    deliver,
    finish,
    github_app_signing,
    post,
    publish_github,
    quiet_dispatch,
    world_for,
)
from tests.services.test_platform_review import (  # noqa: F401
    RESULT,
    instant_readback,
    reset_ci_authority,
)

OUT_OF_SCOPE = "project_not_in_posting_scope"

# Event deliveries create the execution row and do not run the orchestrator.
pytestmark = pytest.mark.usefixtures("quiet_dispatch")


def scope(world: SimpleNamespace, *projects: models.Project) -> None:
    """Store ``project_ids`` on the flow, as a save would."""
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {
        **config["platform_review"],
        "project_ids": [str(project.id) for project in projects],
    }
    world.flow.git_clone_config = config
    world.db.flush()


def ci_run(
    world: SimpleNamespace,
    project: models.Project,
    *,
    head: str = "d" * 40,
    number: int = 7,
) -> tuple[Any, str]:
    """A finished run started by the project's own CI key."""
    grant = CiGrant(
        version=1,
        project_id=project.id,
        flow_id=world.flow.id,
        actions=tuple(CiAction),
    )
    _principal, _key, token = crud.crud_ci_principal.provision(
        world.db, actor=world.user, name=f"CI {project.slug}", grant=grant
    )
    context = crud.crud_ci_principal.authenticate(world.db, token=token)
    assert context is not None
    values = asdict(context)
    values.pop("actions")
    values.pop("credential_version")
    binding = CiReviewBinding(
        **values,
        version=1,
        pr_number=number,
        head_sha=head,
        base_sha=BASE,
        provider_pr_id="9001",
        base_branch="develop",
    )
    execution = crud.crud_ci_execution.create(
        world.db, context=context, binding=binding, event={}
    )
    return finish(world, execution, {**RESULT, "head_sha": head}), token


def ci_github(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Any:
    github = publish_github(world, monkeypatch)
    for project in world.projects:
        github.pulls[(project.slug, 7)]["head"]["sha"] = "d" * 40
    return github


def publications(world: SimpleNamespace, execution: Any) -> list[Any]:
    return (
        world.db.query(models.PlatformReviewPublication)
        .filter(models.PlatformReviewPublication.execution_id == execution.id)
        .all()
    )


# --- unset: today's behaviour ------------------------------------------------


@pytest.mark.asyncio
async def test_unset_list_starts_from_any_admitted_project_and_posts_ci_runs(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    assert "project_ids" not in world.flow.git_clone_config["platform_review"]
    arm(monkeypatch, world)
    started = []
    for index, project in enumerate(world.projects):
        rows = await deliver(world, _event(world, project, number=7))
        assert len(rows) == index + 1
        started.append(
            next(
                row
                for row in rows
                if row.ci_review_binding["project_id"] == str(project.id)
            )
        )
    github = publish_github(world, monkeypatch)
    for execution in started:
        outcome = await post(world, finish(world, execution))
        assert outcome is not None and outcome["review"] == "posted"
        assert "project_in_posting_scope" not in outcome
    ci_execution, _token = ci_run(world, world.projects[1], head=HEAD, number=7)
    outcome = await post(world, ci_execution)
    assert outcome is not None and outcome["status"] == "posted"
    assert len(github.reviews[(world.projects[1].slug, 7)]) == 2


@pytest.mark.asyncio
async def test_explicit_null_list_is_the_same_as_unset(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user)
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {**config["platform_review"], "project_ids": None}
    world.flow.git_clone_config = config
    world.db.flush()
    arm(monkeypatch, world)
    execution = finish(
        world, (await deliver(world, _event(world, world.projects[0])))[0]
    )
    publish_github(world, monkeypatch)
    outcome = await post(world, execution)
    assert outcome is not None and outcome["review"] == "posted"


# --- a list of one of two projects ------------------------------------------


@pytest.mark.asyncio
async def test_event_on_the_listed_project_starts_a_run_that_is_posted(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, _other = world.projects
    scope(world, listed)
    arm(monkeypatch, world)
    rows = await deliver(world, _event(world, listed))
    assert len(rows) == 1
    github = publish_github(world, monkeypatch)
    outcome = await post(world, finish(world, rows[0]))
    assert outcome is not None and outcome["review"] == "posted"
    assert outcome["project_in_posting_scope"] is True
    assert list(github.reviews) == [(listed.slug, 7)]


@pytest.mark.asyncio
async def test_event_on_the_unlisted_project_inserts_no_execution(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    broker = arm(monkeypatch, world)
    event = _event(world, other)
    with caplog.at_level("WARNING"):
        rows = await deliver(world, event)
    # Neither a posting run nor the failed execution of the older path.
    assert rows == []
    assert f"reason={OUT_OF_SCOPE}" in caplog.text
    assert event["delivery_id"] in caplog.text
    # Refused before the provider is read.
    assert broker.calls == []


@pytest.mark.asyncio
async def test_ci_run_of_the_unlisted_project_finishes_and_is_not_posted(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    execution, _token = ci_run(world, other)
    github = ci_github(world, monkeypatch)
    assert await post(world, execution) is None
    assert github.calls == []
    assert github.mints == []
    # Nothing that reads as a failure or a pending retry.
    assert publications(world, execution) == []
    world.db.refresh(execution)
    assert execution.status == "SUCCEEDED"
    projection = platform_review.platform_review_projection(world.db, execution)
    assert projection is not None
    assert projection["status"] == "skipped"
    assert projection["reason"] == OUT_OF_SCOPE
    assert projection["retry_scheduled"] is False
    assert projection["project_in_posting_scope"] is False
    # The listed project's CI run is still posted.
    listed_run, _ = ci_run(world, listed)
    outcome = await post(world, listed_run)
    assert outcome is not None and outcome["status"] == "posted"


@pytest.mark.asyncio
async def test_sweep_does_not_select_runs_outside_the_list(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    inside, _ = ci_run(world, listed)
    outside, _ = ci_run(world, other)
    finished_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    inside.end_time = outside.end_time = finished_at
    world.flow.updated_at = datetime(2020, 1, 1)
    world.db.commit()
    found = crud.crud_platform_review_publication.unclaimed_execution_ids(
        world.db, finished_within=timedelta(hours=24)
    )
    assert (world.account.id, inside.id) in found
    assert (world.account.id, outside.id) not in found


# --- explicit publish --------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_publish_out_of_scope_is_refused_with_its_own_reason(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    execution, _token = ci_run(world, other)
    github = ci_github(world, monkeypatch)
    with pytest.raises(PlatformReviewRefusedError) as raised:
        await post(world, execution, explicit=True)
    assert raised.value.reason == OUT_OF_SCOPE
    assert raised.value.reason != "platform_review_disabled"
    assert raised.value.status_code == 409
    assert github.calls == []
    assert publications(world, execution) == []


def test_user_publish_out_of_scope_answers_409(
    client: TestClient,
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    execution, _token = ci_run(world, other)
    github = ci_github(world, monkeypatch)
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review", json={}
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == OUT_OF_SCOPE
    assert "posting scope" in detail["message"]
    assert github.writes == []


def test_ci_identity_publish_out_of_scope_answers_409(
    client: TestClient,
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed)
    execution, token = ci_run(world, other)

    def sessions() -> Any:
        yield db_session

    monkeypatch.setattr("preloop.api.middleware.ci_auth.get_auth_db_session", sessions)
    github = ci_github(world, monkeypatch)
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/platform-review",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == OUT_OF_SCOPE
    assert str(listed.id) not in response.text
    assert github.writes == []


@pytest.mark.asyncio
async def test_binding_project_decides_never_the_caller(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both binding versions: the stored project, not a request field."""
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    arm(monkeypatch, world)
    event_run = finish(world, (await deliver(world, _event(world, other)))[0])
    ci_execution, _ = ci_run(world, other)
    scope(world, listed)
    ci_github(world, monkeypatch)
    for execution in (event_run, ci_execution):
        assert execution.ci_review_binding["project_id"] == str(other.id)
        with pytest.raises(PlatformReviewRefusedError) as raised:
            await post(world, execution, explicit=True)
        assert raised.value.reason == OUT_OF_SCOPE


# --- in flight ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_removed_from_the_list_while_in_flight_is_not_posted(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.event_review import authorize_event_review
    from preloop.services.platform_review import PlatformReviewError

    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed, other)
    arm(monkeypatch, world)
    execution = (await deliver(world, _event(world, other)))[0]
    assert authorize_event_review(world.db, execution).project_id == other.id
    scope(world, listed)
    with pytest.raises(PlatformReviewError) as raised:
        authorize_event_review(world.db, execution)
    assert raised.value.reason == "admission_revoked"
    github = publish_github(world, monkeypatch)
    assert await post(world, finish(world, execution)) is None
    assert github.writes == []


@pytest.mark.asyncio
async def test_list_change_between_check_and_claim_is_not_posted(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt itself re-checks the list on the flow it posts for."""
    world = world_for(db_session, test_user, projects=2)
    listed, other = world.projects
    scope(world, listed, other)
    execution, _ = ci_run(world, other)
    github = ci_github(world, monkeypatch)
    claim = crud.crud_platform_review_publication.claim

    def narrowing_claim(*args: Any, **kwargs: Any) -> Any:
        scope(world, listed)
        return claim(*args, **kwargs)

    monkeypatch.setattr(crud.crud_platform_review_publication, "claim", narrowing_claim)
    outcome = await post(world, execution)
    assert outcome is not None and outcome["reason"] == "admission_revoked"
    assert github.writes == []
    row = publications(world, execution)[0]
    assert row.retryable is False and row.next_attempt_at is None


# --- empty list ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_list_posts_for_no_project(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = world_for(db_session, test_user, projects=2)
    scope(world)
    assert world.flow.git_clone_config["platform_review"]["project_ids"] == []
    broker = arm(monkeypatch, world)
    for project in world.projects:
        assert await deliver(world, _event(world, project)) == []
    assert broker.calls == []
    execution, _ = ci_run(world, world.projects[0])
    github = ci_github(world, monkeypatch)
    assert await post(world, execution) is None
    assert github.calls == []


# --- save-time refusals --------------------------------------------------------


def _save(world: SimpleNamespace, project_ids: Any, **block: Any) -> None:
    config = dict(world.flow.git_clone_config)
    config["platform_review"] = {
        **config["platform_review"],
        **block,
        "project_ids": project_ids,
    }
    _reject_invalid_platform_review(
        world.db,
        account_id=world.account.id,
        git_clone_config=GitCloneConfig.model_validate(config),
        result_schema=world.flow.result_schema,
        validate_result=True,
        trigger_event_source=world.flow.trigger_event_source,
        trigger_event_types=world.flow.trigger_event_types,
        trigger_project_ids=world.flow.trigger_project_ids,
        webhook_config=world.flow.webhook_config,
    )


def test_save_accepts_a_member_and_refuses_an_id_the_flow_does_not_admit(
    db_session: Session, test_user: Any
) -> None:
    world = world_for(db_session, test_user, projects=2)
    _save(world, [str(world.projects[0].id)])
    stranger = str(uuid4())
    with pytest.raises(HTTPException) as raised:
        _save(world, [str(world.projects[0].id), stranger])
    assert raised.value.status_code == 422
    assert "platform_review.project_ids" in raised.value.detail
    assert stranger in raised.value.detail


@pytest.mark.parametrize(
    ("project_ids", "offending"),
    [
        (["{a}", "{a}"], "{a}"),
        (["not-a-uuid"], "not-a-uuid"),
        ([7], "7"),
        ("{a}", None),
    ],
)
def test_save_refuses_a_duplicate_a_non_uuid_and_a_non_list(
    project_ids: Any, offending: str | None
) -> None:
    project = str(uuid4())

    def fill(value: Any) -> Any:
        return value.replace("{a}", project) if isinstance(value, str) else value

    ids = (
        [fill(item) for item in project_ids]
        if isinstance(project_ids, list)
        else fill(project_ids)
    )
    block = {"enabled": True, "project_ids": ids}
    config = {
        "enabled": True,
        "publication_mode": "read_only",
        "repositories": [{"project_id": project, "tracker_id": str(uuid4())}],
        "platform_review": block,
    }
    with pytest.raises(ValueError) as raised:
        GitCloneConfig.model_validate(config)
    assert "platform_review.project_ids" in str(raised.value)
    if offending is not None:
        assert fill(offending) in str(raised.value)
    with pytest.raises(ValueError):
        platform_review.validate_platform_review_block(block)


def test_save_refuses_the_list_on_a_flow_that_is_not_a_posting_flow() -> None:
    project = str(uuid4())
    for block in (
        {"enabled": False, "project_ids": [project]},
        {"project_ids": [project]},
    ):
        config = {
            "enabled": True,
            "publication_mode": "read_only",
            "repositories": [{"project_id": project, "tracker_id": str(uuid4())}],
            "platform_review": block,
        }
        with pytest.raises(ValueError, match="platform_review.project_ids"):
            GitCloneConfig.model_validate(config)
        with pytest.raises(ValueError):
            platform_review.validate_platform_review_block(block)
    # Off with no list, and on without one, are unchanged.
    platform_review.validate_platform_review_block({"enabled": False})
    platform_review.validate_platform_review_block({"enabled": True})


def test_stored_ids_are_normalised_and_unset_stays_absent() -> None:
    project = uuid4()
    base = {
        "enabled": True,
        "publication_mode": "read_only",
        "repositories": [{"project_id": str(project), "tracker_id": str(uuid4())}],
    }
    listed = GitCloneConfig.model_validate(
        {
            **base,
            "platform_review": {
                "enabled": True,
                "project_ids": [str(project).upper()],
            },
        }
    )
    assert listed.platform_review is not None
    assert listed.platform_review.project_ids == [str(project)]
    unset = GitCloneConfig.model_validate(
        {**base, "platform_review": {"enabled": True}}
    )
    assert unset.platform_review is not None
    assert unset.platform_review.project_ids is None


# --- read side -----------------------------------------------------------------


def test_flow_warnings_say_posting_is_limited_to_n_of_m(
    db_session: Session, test_user: Any
) -> None:
    world = world_for(db_session, test_user, projects=3)
    assert not any(
        item["code"] == "review_posting_project_scope"
        for item in review_posting_warnings(world.flow)
    )
    scope(world, world.projects[0])
    notes = [
        item
        for item in review_posting_warnings(world.flow)
        if item["code"] == "review_posting_project_scope"
    ]
    assert len(notes) == 1
    assert "1 of 3" in notes[0]["message"]


# --- a listed repository with a CI workflow and pull request events ------------


@pytest.mark.asyncio
async def test_listed_repository_with_ci_and_events_runs_once_per_start(
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records what the guide states about two starts for one commit.

    An event for the commit a CI-started run of the same repository is still
    running is dropped by the commit deduplication of the event path. Once
    that run has finished, an event for the same commit starts a new run.
    """
    from preloop.schemas.ci_execution import ci_review_event

    world = world_for(db_session, test_user)
    project = world.projects[0]
    scope(world, project)
    grant = CiGrant(
        version=1, project_id=project.id, flow_id=world.flow.id, actions=tuple(CiAction)
    )
    _p, _k, token = crud.crud_ci_principal.provision(
        world.db, actor=world.user, name="CI", grant=grant
    )
    context = crud.crud_ci_principal.authenticate(world.db, token=token)
    assert context is not None
    values = asdict(context)
    values.pop("actions")
    values.pop("credential_version")
    binding = CiReviewBinding(
        **values,
        version=1,
        pr_number=7,
        head_sha=HEAD,
        base_sha=BASE,
        provider_pr_id="9001",
        base_branch="develop",
    )
    running = crud.crud_ci_execution.create(
        world.db, context=context, binding=binding, event=ci_review_event(binding)
    )
    world.db.flush()
    assert running.status not in platform_review.TERMINAL_STATUSES
    arm(monkeypatch, world)
    rows = await deliver(world, _event(world, project, sha=HEAD))
    assert [row.id for row in rows] == [running.id]
    # Once that run has finished, an event for the same commit starts one.
    finish(world, running)
    rows = await deliver(world, _event(world, project, sha=HEAD))
    assert len(rows) == 2
