"""Policy notice hits, excerpts, debounce, delivery and digest (#959)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_policy_notice_hit,
    crud_user,
    notification_preferences,
)
from preloop.models.models.policy_notice_hit import PolicyNoticeHit
from preloop.models.models.user import User
from preloop.services import policy_notice_delivery as delivery
from preloop.services.policy_notices import (
    PolicyNotice,
    build_excerpt,
    build_policy_notice_digest_section,
    locate_match,
    process_policy_notice,
    record_policy_notice,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
SECRET = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


# --- Excerpts -------------------------------------------------------------


def test_excerpt_is_capped_at_280_characters_around_the_match() -> None:
    text = "lorem ipsum " * 400 + "the project-x launch plan " + "dolor sit " * 400
    excerpt = build_excerpt(text, "request.text.contains('project-x')")

    assert excerpt is not None
    assert len(excerpt) <= 280
    assert "project-x" in excerpt


def test_excerpt_redacts_secrets_next_to_the_match() -> None:
    text = f"deploy project-x with key {SECRET} tonight"
    excerpt = build_excerpt(text, "request.text.contains('project-x')")

    assert excerpt is not None
    assert "project-x" in excerpt
    assert SECRET not in excerpt
    assert SECRET[:16] not in excerpt


def test_excerpt_is_none_when_redaction_fails() -> None:
    def broken(_text):
        raise RuntimeError("scrubber down")

    assert build_excerpt("project-x", "x.contains('project-x')", scrub=broken) is None


def test_excerpt_uses_last_regex_match() -> None:
    text = "code AB-1 early, then later AB-2 is mentioned"
    span = locate_match(text, 'request.text.matches("AB-[0-9]")')
    assert span is not None
    assert text[span[0] : span[1]] == "AB-2"


def test_excerpt_without_locatable_literal_starts_at_the_beginning() -> None:
    text = "Contact me at someone@example.org please"
    assert locate_match(text, "pii.found == true") is None
    excerpt = build_excerpt(text, "pii.found == true")
    assert excerpt is not None
    assert excerpt.startswith("Contact me")


def test_excerpt_of_empty_text_is_none() -> None:
    assert build_excerpt("", "x.contains('a')") is None


# --- Recording and debounce ----------------------------------------------


def _notice(user: User, **overrides) -> PolicyNotice:
    values = {
        "account_id": user.account_id,
        "user_id": user.id,
        "target": "model.request",
        "rule_id": "notify-codename",
        "rule_description": "Mentions of the codename",
        "text_sha256": "a" * 64,
        "excerpt": "the project-x plan",
    }
    values.update(overrides)
    return PolicyNotice(**values)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[PolicyNoticeHit] = []

    def __call__(self, _db: Session, hit: PolicyNoticeHit) -> dict:
        self.calls.append(hit)
        return {"email": 1}


def test_record_writes_a_hit_without_full_text(
    db_session: Session, test_user: User
) -> None:
    deliver = _Recorder()
    outcome = record_policy_notice(
        db_session, _notice(test_user), now=NOW, deliver=deliver
    )

    hit = db_session.get(PolicyNoticeHit, outcome.hit_id)
    assert hit is not None
    assert hit.account_id == test_user.account_id
    assert hit.user_id == test_user.id
    assert hit.rule_id == "notify-codename"
    assert hit.text_sha256 == "a" * 64
    assert hit.excerpt == "the project-x plan"
    assert outcome.notified is True
    assert len(deliver.calls) == 1


def test_debounce_sends_once_per_rule_user_and_hour(
    db_session: Session, test_user: User
) -> None:
    deliver = _Recorder()
    first = record_policy_notice(
        db_session, _notice(test_user), now=NOW, deliver=deliver
    )
    second = record_policy_notice(
        db_session, _notice(test_user), now=NOW + timedelta(minutes=30), deliver=deliver
    )
    other_rule = record_policy_notice(
        db_session,
        _notice(test_user, rule_id="notify-other"),
        now=NOW + timedelta(minutes=31),
        deliver=deliver,
    )
    later = record_policy_notice(
        db_session, _notice(test_user), now=NOW + timedelta(minutes=61), deliver=deliver
    )

    assert [first.notified, second.notified, other_rule.notified, later.notified] == [
        True,
        False,
        True,
        True,
    ]
    assert len(deliver.calls) == 3
    # Every hit is stored, debounced or not.
    count = (
        db_session.query(PolicyNoticeHit)
        .filter(PolicyNoticeHit.account_id == test_user.account_id)
        .count()
    )
    assert count == 4


def test_debounce_is_per_user(db_session: Session, test_user: User) -> None:
    other = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "second@example.com",
            "username": "seconduser",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    deliver = _Recorder()
    a = record_policy_notice(db_session, _notice(test_user), now=NOW, deliver=deliver)
    b = record_policy_notice(db_session, _notice(other), now=NOW, deliver=deliver)
    assert a.notified and b.notified


def test_delivery_failure_keeps_the_hit(db_session: Session, test_user: User) -> None:
    def broken(_db, _hit):
        raise RuntimeError("smtp down")

    outcome = record_policy_notice(
        db_session, _notice(test_user), now=NOW, deliver=broken
    )
    assert outcome.delivery == {"error": "delivery_failed"}
    assert db_session.get(PolicyNoticeHit, outcome.hit_id) is not None


def test_hit_without_excerpt_keeps_hash_and_rule(
    db_session: Session, test_user: User
) -> None:
    outcome = record_policy_notice(
        db_session, _notice(test_user, excerpt=None), now=NOW, deliver=_Recorder()
    )
    hit = db_session.get(PolicyNoticeHit, outcome.hit_id)
    assert hit.excerpt is None
    assert hit.rule_id == "notify-codename"
    assert hit.text_sha256 == "a" * 64


def test_process_policy_notice_never_raises() -> None:
    with (
        patch(
            "preloop.services.policy_notices.record_policy_notice",
            side_effect=RuntimeError("db down"),
        ),
        patch("preloop.models.db.session.get_db_session") as get_db,
        patch("preloop.models.db.session._safe_close_db_session") as close,
    ):
        get_db.return_value = iter([_FakeSession()])
        assert process_policy_notice(_notice(_FakeUser())) is None
    close.assert_called_once()


class _FakeSession:
    def rollback(self) -> None:
        pass


class _FakeUser:
    account_id = uuid4()
    id = uuid4()


# --- Summary and digest section -------------------------------------------


def test_summary_counts_by_rule_with_latest_excerpt(
    db_session: Session, test_user: User
) -> None:
    deliver = _Recorder()
    for minutes, excerpt in ((0, "first"), (5, "second"), (10, "third")):
        record_policy_notice(
            db_session,
            _notice(test_user, excerpt=excerpt),
            now=NOW + timedelta(minutes=minutes),
            deliver=deliver,
        )
    record_policy_notice(
        db_session,
        _notice(test_user, rule_id="notify-other", excerpt="other"),
        now=NOW + timedelta(minutes=1),
        deliver=deliver,
    )
    # Outside the 7 day window.
    record_policy_notice(
        db_session,
        _notice(test_user, rule_id="notify-old"),
        now=NOW - timedelta(days=8),
        deliver=deliver,
    )

    rows = crud_policy_notice_hit.summarize_by_rule(
        db_session, account_id=test_user.account_id, since=NOW - timedelta(days=7)
    )

    assert [row.rule_id for row in rows] == ["notify-codename", "notify-other"]
    assert rows[0].count == 3
    assert rows[0].last_excerpt == "third"
    assert rows[0].last_username == "testuser"
    assert rows[0].last_hit_at.tzinfo is not None


def test_digest_section_renders_rule_count_user_and_excerpt(
    db_session: Session, test_user: User
) -> None:
    for minutes in (0, 1):
        record_policy_notice(
            db_session,
            _notice(test_user, excerpt="<b>project-x</b> plan"),
            now=NOW + timedelta(minutes=minutes),
            deliver=_Recorder(),
        )

    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=NOW + timedelta(hours=1)
    )

    assert section.title == "Policy notices"
    text = section.render_text()
    assert "notify-codename: 2 hits" in text
    assert "Last user: testuser" in text
    assert "<b>project-x</b> plan" in text
    rendered = section.render_html()
    assert "&lt;b&gt;project-x&lt;/b&gt;" in rendered
    assert "<b>project-x</b>" not in rendered


def test_digest_section_empty(db_session: Session, test_user: User) -> None:
    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=NOW
    )
    assert section.is_empty
    assert "No notify rule matched" in section.render_text()


# --- Delivery ---------------------------------------------------------------


def _hit(db_session: Session, user: User) -> PolicyNoticeHit:
    hit = crud_policy_notice_hit.record(
        db_session,
        account_id=user.account_id,
        user_id=user.id,
        target="model.request",
        rule_id="notify-codename",
        rule_description="Mentions of the codename",
        text_sha256="b" * 64,
        excerpt="the project-x plan",
        now=NOW,
    )
    return hit


def test_policy_owners_are_users_who_manage_policies(
    db_session: Session, test_user: User
) -> None:
    from preloop.models.crud import crud_role, crud_user_role

    viewer = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "viewer2@example.com",
            "username": "viewer2",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    viewer_role = crud_role.get_by_name(db_session, name="viewer")
    assert viewer_role is not None
    crud_user_role.create(
        db_session, obj_in={"user_id": viewer.id, "role_id": viewer_role.id}
    )
    db_session.flush()

    owners = delivery.policy_owners(db_session, test_user.account_id)

    assert [owner.id for owner in owners] == [test_user.id]


def test_email_goes_to_owners_and_has_no_approval_link(
    db_session: Session, test_user: User
) -> None:
    hit = _hit(db_session, test_user)
    with patch("preloop.utils.email.send_email") as send:
        result = delivery.deliver_policy_notice(db_session, hit)

    assert result["email"] == 1
    to, subject, body_text, body_html = send.call_args.args
    assert to == test_user.email
    assert subject == "Policy notice: notify-codename"
    assert "the project-x plan" in body_text
    assert "not blocked" in body_text
    for body in (body_text, body_html):
        assert "approve" not in body.lower()
        assert "deny" not in body.lower()
        assert "/console/approval" not in body


def test_email_respects_preference(db_session: Session, test_user: User) -> None:
    notification_preferences.create(
        db_session, user_id=test_user.id, enable_email=False
    )
    hit = _hit(db_session, test_user)
    with patch("preloop.utils.email.send_email") as send:
        result = delivery.deliver_policy_notice(db_session, hit)
    assert result["email"] == 0
    send.assert_not_called()


def test_push_only_when_enabled(db_session: Session, test_user: User) -> None:
    prefs = notification_preferences.create(
        db_session,
        user_id=test_user.id,
        enable_email=False,
        enable_mobile_push=True,
        mobile_device_tokens=[{"token": "tok-android-1", "platform": "android"}],
    )
    assert prefs.get_device_tokens(platform="android") == ["tok-android-1"]
    hit = _hit(db_session, test_user)

    sent = []

    async def fake_android(**kwargs):
        sent.append(kwargs)
        return {"success": True}

    with (
        patch(
            "preloop.services.push_notifications.get_apns_service", return_value=None
        ),
        patch(
            "preloop.services.push_notifications.is_fcm_configured", return_value=True
        ),
        patch(
            "preloop.services.approval_service._send_android_push_transport",
            side_effect=fake_android,
        ),
    ):
        result = delivery.deliver_policy_notice(db_session, hit)

    assert result["push"] == 1
    assert sent[0]["token"] == "tok-android-1"
    assert sent[0]["data"]["type"] == "policy_notice"
    assert sent[0]["title"] == "Policy notice: notify-codename"


def test_push_skipped_when_disabled(db_session: Session, test_user: User) -> None:
    hit = _hit(db_session, test_user)
    with patch("preloop.utils.email.send_email"):
        result = delivery.deliver_policy_notice(db_session, hit)
    assert result["push"] == 0


@pytest.mark.parametrize(
    ("approval_type", "expect_text"),
    [("slack", True), ("mattermost", True), ("webhook", False)],
)
def test_workflow_webhook_receives_notice(
    db_session: Session, test_user: User, approval_type: str, expect_text: bool
) -> None:
    from preloop.models.crud import crud_approval_workflow
    from preloop.models.models.webhook_endpoint import WebhookDelivery

    crud_approval_workflow.create(
        db_session,
        account_id=str(test_user.account_id),
        obj_in={
            "name": f"chat-{approval_type}",
            "approval_type": approval_type,
            "approval_config": {"webhook_url": "https://hooks.example.test/notice"},
            "is_default": True,
        },
    )
    db_session.flush()
    hit = _hit(db_session, test_user)

    with patch("preloop.utils.email.send_email"):
        result = delivery.deliver_policy_notice(db_session, hit)

    assert result["webhook"] is True
    row = (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == test_user.account_id)
        .one()
    )
    assert row.event_type == "policy.notice"
    assert row.subject_id is None
    body = row.payload
    if expect_text:
        assert "notify-codename" in body["text"]
        assert "the project-x plan" in body["text"]
        assert "approv" not in body["text"].lower()
    else:
        assert body["type"] == "policy_notice"
        assert body["rule_id"] == "notify-codename"
        assert body["excerpt"] == "the project-x plan"
        assert "actions" not in body


def test_no_webhook_for_email_workflow(db_session: Session, test_user: User) -> None:
    from preloop.models.crud import crud_approval_workflow

    crud_approval_workflow.create(
        db_session,
        account_id=str(test_user.account_id),
        obj_in={
            "name": "standard",
            "approval_type": "standard",
            "is_default": True,
        },
    )
    hit = _hit(db_session, test_user)
    with patch("preloop.utils.email.send_email"):
        result = delivery.deliver_policy_notice(db_session, hit)
    assert result["webhook"] is False
