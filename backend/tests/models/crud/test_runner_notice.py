"""Runner notices remain private to their owner and bounded for recovery."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from preloop.models import models
from preloop.models.crud.runner_notice import recent, record
from tests.models.crud.test_runner_share import _runner, _user


def test_notices_are_account_owner_and_host_scoped(db_session, test_user):
    runner = _runner(db_session, test_user)
    member = _user(db_session, test_user.account_id, "requester")
    for host in (True, False):
        record(
            db_session,
            runner=runner,
            actor=member,
            title="Runner event",
            message="An account member requested a session.",
            href="/console/runners",
            host=host,
        )
    params = dict(account_id=test_user.account_id, owner_user_id=test_user.id)
    assert len(recent(db_session, **params)) == 2
    assert len(recent(db_session, **params, runner_id=runner.id, host_only=True)) == 1
    assert recent(db_session, account_id=uuid4(), owner_user_id=test_user.id) == []
    assert (
        recent(db_session, account_id=test_user.account_id, owner_user_id=member.id)
        == []
    )
    assert recent(db_session, **params, runner_id=uuid4()) == []
    for row in db_session.query(models.AuditLog).filter_by(
        action="runner.owner_notice"
    ):
        row.timestamp = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.commit()
    assert recent(db_session, **params) == []
