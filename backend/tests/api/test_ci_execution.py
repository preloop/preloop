"""Synthetic CI review bindings and stable execution ownership."""

from dataclasses import asdict
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.schemas.ci_execution import CiReviewBinding, CiReviewRequest
from preloop.schemas.ci_principal import CiAction
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


def review_binding(context: Any) -> CiReviewBinding:
    values = asdict(context)
    values.pop("actions")
    values.pop("credential_version")
    return CiReviewBinding(
        **values,
        version=1,
        pr_number=7,
        head_sha="a" * 40,
        provider_pr_id="synthetic-pr-7",
        base_branch="develop",
    )


@pytest.mark.parametrize(
    "extra",
    [
        "project_id",
        "repository",
        "matrix",
        "ai_model_id",
        "agent_type",
        "runner_pool",
        "credentials",
        "workspace_files",
        "_resume",
        "_ci_binding",
        "trigger_event_details",
        "source_execution_id",
        "prompt_template",
    ],
)
def test_review_request_rejects_every_override(extra: str) -> None:
    with pytest.raises(ValidationError):
        CiReviewRequest.model_validate(
            {"pr_number": 7, "head_sha": "a" * 40, extra: "synthetic-override"}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"pr_number": True, "head_sha": "a" * 40},
        {"pr_number": "7", "head_sha": "a" * 40},
        {"pr_number": 0, "head_sha": "a" * 40},
        {"pr_number": 7, "head_sha": "main"},
        {"pr_number": 7, "head_sha": "a" * 41},
    ],
)
def test_review_request_requires_exact_typed_pr_and_head(payload: Any) -> None:
    with pytest.raises(ValidationError):
        CiReviewRequest.model_validate(payload)


def test_owned_execution_rotation_and_filters_before_pagination(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    _, _, other_token = provision(db_session, ci_resources)
    other_context = crud.crud_ci_principal.authenticate(db_session, token=other_token)
    own = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    crud.crud_ci_execution.create(
        db_session,
        context=other_context,
        binding=review_binding(other_context),
        event={},
    )
    history = CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": context.flow_id}
    )
    assert own.ci_principal_id == principal.id
    assert own.initiating_ci_key_id == key.id
    assert [
        row.id
        for row in crud.crud_ci_execution.list(db_session, context=context, limit=1)
    ] == [own.id]
    for denied in (history.id, uuid4()):
        assert (
            crud.crud_ci_execution.get(
                db_session,
                context=context,
                execution_id=denied,
                action=CiAction.READ_EXECUTION,
            )
            is None
        )
    assert (
        crud.crud_ci_execution.get(
            db_session,
            context=other_context,
            execution_id=own.id,
            action=CiAction.READ_RESULT,
        )
        is None
    )
    _, rotated = crud.crud_ci_principal.rotate(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    rotated_context = crud.crud_ci_principal.authenticate(db_session, token=rotated)
    assert (
        crud.crud_ci_execution.get(
            db_session,
            context=rotated_context,
            execution_id=own.id,
            action=CiAction.READ_RESULT,
        ).id
        == own.id
    )


def test_dispatch_uses_principal_not_revoked_initiating_key(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    crud.crud_ci_execution.authorize_dispatch(db_session, execution=execution)
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.get(
            db_session,
            context=context,
            execution_id=execution.id,
            action=CiAction.READ_EXECUTION,
        )
    grant = ci_resources[3].model_copy(update={"actions": (CiAction.READ_EXECUTION,)})
    crud.crud_ci_principal.change(
        db_session, actor=ci_resources[0], principal_id=principal.id, grant=grant
    )
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.authorize_dispatch(db_session, execution=execution)


def test_binding_move_denies_dispatch_and_reads(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    crud.crud_project.update(
        db_session, db_obj=ci_resources[1], obj_in={"identifier": "moved-repository"}
    )
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.authorize_dispatch(db_session, execution=execution)
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.get(
            db_session,
            context=context,
            execution_id=execution.id,
            action=CiAction.READ_EXECUTION,
        )


@pytest.mark.parametrize(
    "control",
    ["_feedback_prompt", "_answers_prompt", "_resume", "_matrix", "future_control"],
)
def test_internal_creation_rejects_unknown_control_before_writes(
    db_session: Session, ci_resources: tuple[Any, ...], control: str
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    before = crud.crud_ci_execution.count(db_session, context=context)
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.create(
            db_session,
            context=context,
            binding=review_binding(context),
            event={control: "synthetic-override"},
        )
    assert crud.crud_ci_execution.count(db_session, context=context) == before


@pytest.mark.parametrize(
    "corruption", ["missing_head", "extra", "branch", "key", "number"]
)
def test_corrupt_bindings_are_filtered_consistently_before_pagination(
    db_session: Session, ci_resources: tuple[Any, ...], corruption: str
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    binding = review_binding(context)
    own = crud.crud_ci_execution.create(
        db_session, context=context, binding=binding, event={}
    )
    invalid = binding.model_dump(mode="json")
    if corruption == "missing_head":
        invalid.pop("head_sha")
    elif corruption == "extra":
        invalid["unknown"] = "synthetic"
    elif corruption == "branch":
        invalid["base_branch"] = "../escape"
    elif corruption == "key":
        invalid["key_id"] = "invalid-key-id"
    else:
        invalid["pr_number"] = True
    malformed = CRUDBase(models.FlowExecution).create(
        db_session,
        obj_in={
            "flow_id": context.flow_id,
            "ci_principal_id": context.principal_id,
            "initiating_ci_key_id": context.key_id,
            "ci_review_binding": invalid,
        },
    )
    assert (
        crud.crud_ci_execution.get(
            db_session,
            context=context,
            execution_id=malformed.id,
            action=CiAction.READ_EXECUTION,
        )
        is None
    )
    assert [
        row.id
        for row in crud.crud_ci_execution.list(db_session, context=context, limit=1)
    ] == [own.id]
    assert crud.crud_ci_execution.count(db_session, context=context) == 1


def test_partial_machine_marker_never_uses_human_dispatch(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    _, key, _ = provision(db_session, ci_resources)
    partial = CRUDBase(models.FlowExecution).create(
        db_session,
        obj_in={"flow_id": ci_resources[2].id, "initiating_ci_key_id": key.id},
    )
    with pytest.raises(PermissionError):
        crud.crud_ci_execution.authorize_dispatch(db_session, execution=partial)


def test_accepted_snapshot_is_immutable_in_storage(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    own = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    changed = {**own.ci_review_binding, "head_sha": "b" * 40}
    with pytest.raises(SQLAlchemyError, match="CI execution attribution is immutable"):
        CRUDBase(models.FlowExecution).update(
            db_session, db_obj=own, obj_in={"ci_review_binding": changed}
        )
    db_session.rollback()
