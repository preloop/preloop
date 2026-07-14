"""Tests for the growth analytics CRUD modules (visitor, identity, milestones)."""

import uuid
from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_account_milestone,
    crud_event,
    crud_identity_link,
    crud_user,
    crud_visitor,
)


def _make_account(db, name="Growth Org"):
    return crud_account.create(
        db, obj_in={"organization_name": name, "is_active": True}
    )


def _make_user(db, account, email="growth@example.com"):
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": email.split("@")[0],
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


class TestVisitorCRUD:
    def test_upsert_creates_and_counts_sessions(self, db_session):
        visitor_id = uuid.uuid4()
        visitor = crud_visitor.upsert_observation(
            db_session,
            visitor_id=visitor_id,
            profile={"browser": "Firefox", "timezone": "Europe/Madrid"},
            count_session=True,
            commit=False,
        )
        assert visitor.session_count == 1
        assert visitor.browser == "Firefox"

        visitor = crud_visitor.upsert_observation(
            db_session,
            visitor_id=visitor_id,
            profile={"browser": "Chrome"},
            count_session=True,
            commit=False,
        )
        assert visitor.session_count == 2
        assert visitor.browser == "Chrome"  # latest profile wins
        assert visitor.timezone == "Europe/Madrid"  # untouched fields survive

    def test_attribution_is_first_touch(self, db_session):
        visitor_id = uuid.uuid4()
        crud_visitor.upsert_observation(
            db_session,
            visitor_id=visitor_id,
            attribution={"first_landing_path": "/pricing", "utm_source": "hn"},
            commit=False,
        )
        visitor = crud_visitor.upsert_observation(
            db_session,
            visitor_id=visitor_id,
            attribution={"first_landing_path": "/other", "utm_source": "google"},
            commit=False,
        )
        assert visitor.first_landing_path == "/pricing"
        assert visitor.utm_source == "hn"

    def test_link_user_write_once(self, db_session):
        account = _make_account(db_session)
        user_a = _make_user(db_session, account, "a@example.com")
        user_b = _make_user(db_session, account, "b@example.com")
        visitor_id = uuid.uuid4()
        crud_visitor.upsert_observation(db_session, visitor_id=visitor_id, commit=False)

        crud_visitor.link_user(
            db_session, visitor_id=visitor_id, user_id=user_a.id, commit=False
        )
        crud_visitor.link_user(
            db_session, visitor_id=visitor_id, user_id=user_b.id, commit=False
        )
        visitor = crud_visitor.get_by_id(db_session, visitor_id=visitor_id)
        assert visitor.user_id == user_a.id  # first identification sticks


class TestIdentityLinkCRUD:
    def test_upsert_is_idempotent(self, db_session):
        account = _make_account(db_session)
        user = _make_user(db_session, account)
        for _ in range(3):
            crud_identity_link.upsert_link(
                db_session,
                principal_type="fingerprint",
                principal_id="fp-123",
                user_id=user.id,
                account_id=account.id,
                source="ws_auth",
                commit=False,
            )
        links = crud_identity_link.get_for_user(db_session, user_id=user.id)
        assert len(links) == 1
        assert links[0].source == "ws_auth"

    def test_account_filled_in_when_missing(self, db_session):
        account = _make_account(db_session)
        user = _make_user(db_session, account)
        crud_identity_link.upsert_link(
            db_session,
            principal_type="cli_client",
            principal_id="cli-1",
            user_id=user.id,
            account_id=None,
            source="cli_check_in",
            commit=False,
        )
        crud_identity_link.upsert_link(
            db_session,
            principal_type="cli_client",
            principal_id="cli-1",
            user_id=user.id,
            account_id=account.id,
            source="cli_check_in",
            commit=False,
        )
        links = crud_identity_link.get_for_principal(
            db_session, principal_type="cli_client", principal_id="cli-1"
        )
        assert len(links) == 1
        assert links[0].account_id == account.id

    def test_distinct_users_get_distinct_edges(self, db_session):
        account = _make_account(db_session)
        user_a = _make_user(db_session, account, "edge-a@example.com")
        user_b = _make_user(db_session, account, "edge-b@example.com")
        for user in (user_a, user_b):
            crud_identity_link.upsert_link(
                db_session,
                principal_type="visitor",
                principal_id="shared-device",
                user_id=user.id,
                commit=False,
            )
        links = crud_identity_link.get_for_principal(
            db_session, principal_type="visitor", principal_id="shared-device"
        )
        assert len(links) == 2


class TestAccountMilestoneCRUD:
    def test_create_if_absent_idempotent(self, db_session):
        account = _make_account(db_session)
        first = crud_account_milestone.create_if_absent(
            db_session, account_id=account.id, milestone="first_agent", commit=False
        )
        second = crud_account_milestone.create_if_absent(
            db_session, account_id=account.id, milestone="first_agent", commit=False
        )
        assert first is True
        assert second is False
        milestones = crud_account_milestone.get_for_account(
            db_session, account_id=account.id
        )
        assert [m.milestone for m in milestones] == ["first_agent"]

    def test_count_by_milestone_with_window(self, db_session):
        account_a = _make_account(db_session, "Growth A")
        account_b = _make_account(db_session, "Growth B")
        old = datetime.now(timezone.utc) - timedelta(days=90)
        crud_account_milestone.create_if_absent(
            db_session,
            account_id=account_a.id,
            milestone="first_flow",
            achieved_at=old,
            commit=False,
        )
        crud_account_milestone.create_if_absent(
            db_session, account_id=account_b.id, milestone="first_flow", commit=False
        )
        counts = crud_account_milestone.count_by_milestone(db_session)
        assert counts["first_flow"] == 2
        recent = crud_account_milestone.count_by_milestone(
            db_session, since=datetime.now(timezone.utc) - timedelta(days=7)
        )
        assert recent["first_flow"] == 1


class TestEventCRUD:
    def test_log_event_and_get_session_start(self, db_session):
        session_id = uuid.uuid4()
        visitor_id = uuid.uuid4()
        crud_event.log_event(
            db_session,
            event_type="session_start",
            session_id=session_id,
            visitor_id=visitor_id,
            fingerprint="fp-evt",
            client_type="web",
            ip_address="8.8.8.8",
            country="Germany",
            commit=False,
        )
        row = crud_event.get_session_start(db_session, session_id=session_id)
        assert row is not None
        assert row.visitor_id == visitor_id
        assert row.client_type == "web"
        assert row.country == "Germany"
