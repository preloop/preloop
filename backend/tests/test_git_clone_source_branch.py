"""A blank flow source branch stays blank when the form sends null (#2211)."""

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import FlowCreate


def _create(db_session: Session, user, git_clone_config: dict):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Branch field reviewer",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
            prompt_template="Review",
            agent_type="codex",
            agent_config={},
            git_clone_config=git_clone_config,
            account_id=user.account_id,
        ),
        account_id=user.account_id,
    )


def test_explicit_null_source_branch_is_stored_as_null(
    db_session: Session, test_user
) -> None:
    flow = _create(db_session, test_user, {"enabled": True, "source_branch": None})
    db_session.refresh(flow)
    assert flow.git_clone_config["source_branch"] is None


def test_pinned_source_branch_is_stored_as_given(
    db_session: Session, test_user
) -> None:
    flow = _create(
        db_session, test_user, {"enabled": True, "source_branch": "release/1.0"}
    )
    db_session.refresh(flow)
    assert flow.git_clone_config["source_branch"] == "release/1.0"
