"""Provisioning cannot grant a historical native flow an unsupported resume policy."""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.base import CRUDBase
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


def test_provision_rejects_historical_native_resume_policy(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    flow = ci_resources[2]
    CRUDBase(models.Flow).update(
        db_session,
        db_obj=flow,
        obj_in={"agent_config": {"native_result": True, "feedback": {"enabled": True}}},
    )
    with pytest.raises(ValueError, match="native_result.*feedback"):
        provision(db_session, ci_resources)
