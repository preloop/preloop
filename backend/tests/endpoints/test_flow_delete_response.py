"""Deleting a model-backed flow returns a complete response after commit."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models, schemas
from preloop.models.crud import crud_ai_model, crud_flow
from preloop.models.db.session import get_db_session


def test_delete_model_backed_flow_response(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Lazy model names must be read before deletion detaches the flow."""
    model = crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Example model",
            "provider_name": "openai",
            "model_identifier": "example-model",
        },
        account_id=test_user.account_id,
    )
    flow = crud_flow.create(
        db_session,
        flow_in=schemas.FlowCreate(
            name="Disposable flow",
            prompt_template="Reply OK",
            agent_config={},
            ai_model_id=model.id,
        ),
        account_id=test_user.account_id,
    )
    flow_id = flow.id
    db_session.expire(flow)

    # Real requests use a fresh identity map and detach deletions on commit.
    # Share only the fixture's connection so its outer rollback still isolates us.
    with Session(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    ) as request_db:
        original_override = client.app.dependency_overrides[get_db_session]
        client.app.dependency_overrides[get_db_session] = lambda: request_db
        try:
            response = client.delete(f"/api/v1/flows/{flow_id}")
        finally:
            client.app.dependency_overrides[get_db_session] = original_override

    assert response.status_code == 200
    assert response.json()["id"] == str(flow_id)
    assert response.json()["ai_model_name"] == "Example model"
    assert (
        crud_flow.get(db_session, id=flow_id, account_id=test_user.account_id) is None
    )
    follow_up = client.delete(f"/api/v1/flows/{flow_id}")
    assert follow_up.status_code == 404
