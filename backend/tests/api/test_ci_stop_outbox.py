"""Real restricted stop outbox, scoped to the execution's principal."""

from typing import Any

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.schemas.ci_subscription import CiSubscriptionCreate
from tests.api.test_ci_execution import review_binding
import pytest
from tests.api.test_ci_principal import ci_resources as create_ci_resources, provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


def test_stop_enqueues_only_owned_subscription_once(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    _, _, token = provision(db_session, ci_resources)
    _, _, other_token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    other = crud.crud_ci_principal.authenticate(db_session, token=other_token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/own"),
    )
    crud.crud_ci_subscription.create(
        db_session,
        context=other,
        payload=CiSubscriptionCreate(url="https://example.com/other"),
    )
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    assert crud.crud_flow_execution.mark_stopped(
        db_session,
        execution_id=execution.id,
        error_message="budget expired",
        commit=False,
    )
    rows = db_session.query(models.WebhookDelivery).all()
    assert len(rows) == 1
    assert rows[0].endpoint_id == endpoint.id
    assert rows[0].payload["data"]["status"] == "STOPPED"
    assert rows[0].payload["data"]["head_sha"] == "a" * 40
    assert not crud.crud_flow_execution.mark_stopped(
        db_session,
        execution_id=execution.id,
        error_message="repeated stop",
        commit=False,
    )
    assert db_session.query(models.WebhookDelivery).count() == 1
    db_session.rollback()
    assert db_session.query(models.WebhookDelivery).count() == 0
