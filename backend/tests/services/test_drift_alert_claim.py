"""Durable billing alerts must survive process restarts and concurrent callers."""

from unittest.mock import MagicMock
from sqlalchemy.orm import Session
from preloop.models import models

from preloop.models.crud.billing import billing


def test_drift_claim_does_not_reclaim_previous_dispatch() -> None:
    """SMTP outcomes can be uncertain; a restart never claims the same send."""
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    assert (
        billing.claim_drift_alert(
            db,
            account_id="00000000-0000-0000-0000-000000000001",
            provider="openai",
            period="2026-01-01",
            fingerprint="a" * 64,
        )
        is False
    )
    assert "ON CONFLICT" in str(db.execute.call_args.args[0])
    db.commit.assert_called_once()


def test_durable_drift_claim_is_scoped_and_change_sensitive(
    db_session: Session,
    test_user: models.User,
) -> None:
    """The real unique constraint suppresses restarts, isolating provider/day/state."""
    account_id = str(test_user.account_id)
    arguments = dict(
        account_id=account_id,
        provider="openai",
        period="2026-01-01",
        fingerprint="a" * 64,
    )
    assert billing.claim_drift_alert(db_session, **arguments)
    assert not billing.claim_drift_alert(db_session, **arguments)
    assert billing.claim_drift_alert(
        db_session, **{**arguments, "provider": "anthropic"}
    )
    assert billing.claim_drift_alert(
        db_session, **{**arguments, "period": "2026-01-02"}
    )
    assert billing.claim_drift_alert(
        db_session, **{**arguments, "fingerprint": "b" * 64}
    )
    db_session.expire_all()
    assert not billing.claim_drift_alert(db_session, **arguments)
