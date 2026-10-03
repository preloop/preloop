"""Policy notice hits, excerpts, debounce, delivery and digest (#959)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
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


def test_regex_newest_match_is_found_in_the_tail_of_a_long_text() -> None:
    from preloop.services.policy_notices import _REGEX_TAIL_CHARS

    filler = "lorem ipsum " * (_REGEX_TAIL_CHARS // 6)
    text = "old ticket PRJ-1 " + filler + " new ticket PRJ-2 at the end"
    start, end = locate_match(text, "request.text.matches('PRJ-[0-9]+')")
    assert text[start:end] == "PRJ-2"


def test_regex_match_only_before_the_tail_falls_back_to_the_first() -> None:
    from preloop.services.policy_notices import _REGEX_TAIL_CHARS

    filler = "lorem ipsum " * (_REGEX_TAIL_CHARS // 6)
    text = "ticket PRJ-7 early " + filler
    assert len(text) > 2 * _REGEX_TAIL_CHARS
    start, end = locate_match(text, "request.text.matches('PRJ-[0-9]+')")
    assert (start, text[start:end]) == (7, "PRJ-7")


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


# --- One account, one window ------------------------------------------------

#: Frozen end of the window the boundary tests report on.
WINDOW_END = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


def _hit_at(
    db: Session,
    user: User,
    at: datetime,
    *,
    rule_id: str = "notify-codename",
    excerpt: Optional[str] = "the project-x plan",
    user_id: Optional[UUID] = None,
) -> PolicyNoticeHit:
    """One hit stored at an exact instant, without the notify debounce."""
    return crud_policy_notice_hit.record(
        db,
        account_id=user.account_id,
        user_id=user.id if user_id is None else user_id,
        target="model.request",
        rule_id=rule_id,
        rule_description="Mentions of the codename",
        text_sha256="e" * 64,
        excerpt=excerpt,
        now=at,
    )


def _named_user(db: Session, username: str, full_name: str, account_id: UUID) -> User:
    """A user of ``account_id`` with the given login and display name."""
    return crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": f"{username}@example.com",
            "username": username,
            "full_name": full_name,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def _other_account(db: Session) -> models.Account:
    return crud_account.create(
        db, obj_in={"organization_name": "Other Org", "is_active": True}
    )


@contextmanager
def _captured_sql(db_engine: Any) -> Iterator[list[str]]:
    """Collect the statements a builder sends while it is inside the block."""
    statements: list[str] = []

    def capture(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)


def test_digest_window_is_half_open_at_both_ends(
    db_session: Session, test_user: User
) -> None:
    """A hit before the start, at the end or after it is not in the window."""
    _hit_at(db_session, test_user, WINDOW_END - timedelta(days=7, seconds=1))
    _hit_at(db_session, test_user, WINDOW_END - timedelta(days=7), excerpt="at start")
    _hit_at(db_session, test_user, WINDOW_END - timedelta(minutes=1), excerpt="last in")
    _hit_at(db_session, test_user, WINDOW_END, excerpt="at end")
    _hit_at(db_session, test_user, WINDOW_END + timedelta(days=1), excerpt="future")

    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    assert section.window_start == WINDOW_END - timedelta(days=7)
    assert section.window_end == WINDOW_END
    assert section.window_days == 7
    (row,) = section.rows
    assert row.count == 2
    assert row.last_excerpt == "last in"
    assert row.last_hit_at == WINDOW_END - timedelta(minutes=1)


def test_digest_section_follows_an_explicit_two_day_window(
    db_session: Session, test_user: User
) -> None:
    start = WINDOW_END - timedelta(days=2)
    _hit_at(db_session, test_user, start - timedelta(seconds=1))
    _hit_at(db_session, test_user, start, excerpt="at start")
    _hit_at(db_session, test_user, WINDOW_END - timedelta(minutes=1), excerpt="last in")
    _hit_at(db_session, test_user, WINDOW_END, excerpt="at end")
    _hit_at(db_session, test_user, WINDOW_END + timedelta(minutes=1), excerpt="future")

    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, start=start, end=WINDOW_END
    )

    assert (section.window_start, section.window_end) == (start, WINDOW_END)
    assert section.window_days == 2
    (row,) = section.rows
    assert (row.count, row.last_excerpt) == (2, "last in")


def test_summary_counts_only_hits_inside_the_window(
    db_session: Session, test_user: User
) -> None:
    """The CRUD bound is applied before grouping, not after."""
    _hit_at(db_session, test_user, WINDOW_END - timedelta(days=3), excerpt="inside")
    _hit_at(db_session, test_user, WINDOW_END, excerpt="at end")

    rows = crud_policy_notice_hit.summarize_by_rule(
        db_session,
        account_id=test_user.account_id,
        since=WINDOW_END - timedelta(days=7),
        until=WINDOW_END,
    )

    (row,) = rows
    assert (row.count, row.last_excerpt) == (1, "inside")


def test_the_same_window_in_another_offset_reads_the_same(
    db_session: Session, test_user: User
) -> None:
    """A window written in local time covers the same hits as one in UTC."""
    _hit_at(db_session, test_user, WINDOW_END - timedelta(hours=1), excerpt="inside")
    _hit_at(db_session, test_user, WINDOW_END + timedelta(hours=1), excerpt="future")
    start = WINDOW_END - timedelta(hours=6)
    offset = timezone(timedelta(hours=-5))

    utc_section = build_policy_notice_digest_section(
        db_session, test_user.account_id, start=start, end=WINDOW_END
    )
    shifted = build_policy_notice_digest_section(
        db_session,
        test_user.account_id,
        start=start.astimezone(offset),
        end=WINDOW_END.astimezone(offset),
    )
    naive = build_policy_notice_digest_section(
        db_session,
        test_user.account_id,
        start=start.replace(tzinfo=None),
        end=WINDOW_END.replace(tzinfo=None),
    )

    for other in (shifted, naive):
        assert (other.window_start, other.window_end) == (start, WINDOW_END)
        assert [row.last_excerpt for row in other.rows] == ["inside"]
    assert [row.last_excerpt for row in utc_section.rows] == ["inside"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"start": WINDOW_END - timedelta(days=1)},
            "start and end must be given together",
        ),
        ({"end": WINDOW_END}, "start and end must be given together"),
        (
            {"start": WINDOW_END, "end": WINDOW_END - timedelta(days=1)},
            "end must be after start",
        ),
        ({"start": WINDOW_END, "end": WINDOW_END}, "end must be after start"),
        (
            {
                "start": WINDOW_END - timedelta(days=1),
                "end": WINDOW_END,
                "now": WINDOW_END,
            },
            "now cannot be combined",
        ),
    ],
)
def test_a_window_that_cannot_be_reported_is_refused(
    db_session: Session, test_user: User, kwargs: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_policy_notice_digest_section(db_session, test_user.account_id, **kwargs)


def test_a_refused_window_is_refused_before_any_query(
    db_engine: Any, db_session: Session, test_user: User
) -> None:
    """Nothing is read for a window that was never going to be rendered."""
    with _captured_sql(db_engine) as statements:
        with pytest.raises(ValueError, match="end must be after start"):
            build_policy_notice_digest_section(
                db_session,
                test_user.account_id,
                start=WINDOW_END,
                end=WINDOW_END - timedelta(days=1),
            )

    assert statements == []


def test_two_accounts_sharing_a_rule_id_and_display_name_stay_isolated(
    db_session: Session, test_user: User
) -> None:
    """Only the account decides which hits and which names are in a section."""
    other_account = _other_account(db_session)
    other_user = _named_user(
        db_session, "otheruser", test_user.full_name, other_account.id
    )
    _hit_at(db_session, other_user, WINDOW_END - timedelta(hours=2), excerpt="theirs")
    _hit_at(db_session, test_user, WINDOW_END - timedelta(hours=1), excerpt="mine")

    mine = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )
    theirs = build_policy_notice_digest_section(
        db_session, other_account.id, now=WINDOW_END
    )

    assert (mine.rows[0].count, mine.rows[0].last_excerpt) == (1, "mine")
    assert (mine.rows[0].last_username, mine.rows[0].last_user_id) == (
        "testuser",
        test_user.id,
    )
    assert (theirs.rows[0].count, theirs.rows[0].last_excerpt) == (1, "theirs")
    assert (theirs.rows[0].last_username, theirs.rows[0].last_user_id) == (
        "otheruser",
        other_user.id,
    )


def test_a_hit_pointing_at_another_accounts_user_names_nobody(
    db_session: Session, test_user: User
) -> None:
    """A user id of another account resolves to no name, not to their name."""
    other_account = _other_account(db_session)
    foreign = _named_user(db_session, "foreignuser", "Jane Doe", other_account.id)
    _hit_at(db_session, test_user, WINDOW_END - timedelta(hours=1), user_id=foreign.id)

    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    (row,) = section.rows
    assert row.count == 1
    assert row.last_username is None
    assert "Last user: unknown" in section.render_text()
    assert "foreignuser" not in section.render_html()
    assert "Jane Doe" not in section.render_html()


def test_a_bounded_window_renders_only_the_stored_excerpt(
    db_session: Session, test_user: User
) -> None:
    """Markup is escaped, and a hit whose redaction failed says so."""
    _hit_at(
        db_session,
        test_user,
        WINDOW_END - timedelta(days=1),
        rule_id="notify-markup",
        excerpt="<b>project-x</b> plan",
    )
    _hit_at(
        db_session,
        test_user,
        WINDOW_END - timedelta(hours=1),
        rule_id="notify-unredacted",
        excerpt=None,
    )

    section = build_policy_notice_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=2),
        end=WINDOW_END,
    )

    text = section.render_text()
    assert "notify-markup: 1 hit" in text
    assert "Last excerpt: (not available)" in text
    rendered = section.render_html()
    assert "&lt;b&gt;project-x&lt;/b&gt;" in rendered
    assert "<b>project-x</b>" not in rendered


def test_a_section_never_reads_captured_request_content(
    db_engine: Any, db_session: Session, test_user: User
) -> None:
    """The section reads stored hits, never the model request or response."""
    _hit_at(db_session, test_user, WINDOW_END - timedelta(hours=1))
    with _captured_sql(db_engine) as statements:
        section = build_policy_notice_digest_section(
            db_session, test_user.account_id, now=WINDOW_END
        )

    assert section.rows
    assert statements
    for statement in statements:
        assert "gateway_usage_search_document" not in statement
        assert "searchable_text" not in statement


def test_a_partial_day_window_is_described_as_it_is(
    db_session: Session, test_user: User
) -> None:
    """A window that is not a whole number of days is not called one."""
    section = build_policy_notice_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1, hours=3),
        end=WINDOW_END,
    )

    assert section.window_days == 1
    assert section.window_label == "1 day, 3 hours"
    text = section.render_text()
    assert "No notify rule matched in the reporting window." in text
    assert "in the last" not in text
    assert "1 day, 3 hours" in text
    rendered = section.render_html()
    assert "in the last" not in rendered
    assert "1 day, 3 hours" in rendered


def test_a_window_that_is_not_the_last_n_days_names_its_period(
    db_session: Session, test_user: User
) -> None:
    """A window that ended in the past is the range it covers, not "the last"."""
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 8, 9, 0, tzinfo=timezone.utc)
    period = "2026-01-01 09:00:00 UTC to 2026-01-08 09:00:00 UTC (7 days)"

    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, start=start, end=end
    )

    assert section.window_summary == period
    text = section.render_text()
    assert "No notify rule matched in the reporting window." in text
    assert f"Window: {period}." in text
    assert "in the last" not in text
    rendered = section.render_html()
    assert "No notify rule matched in the reporting window." in rendered
    assert f"Window: {period}." in rendered
    assert "in the last" not in rendered

    _hit_at(db_session, test_user, start + timedelta(hours=1), excerpt="inside")
    filled = build_policy_notice_digest_section(
        db_session, test_user.account_id, start=start, end=end
    )
    filled_text = filled.render_text()
    assert "notify-codename: 1 hit" in filled_text
    assert f"Window: {period}." in filled_text
    assert "in the last" not in filled_text
    assert f"Window: {period}." in filled.render_html()


def test_the_default_window_is_still_seven_days(
    db_session: Session, test_user: User
) -> None:
    section = build_policy_notice_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    assert section.window_days == 7
    assert section.window_label == "7 days"
    text = section.render_text()
    assert "No notify rule matched in the reporting window." in text
    assert "7 days" in text
    assert "in the last" not in text


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


def _account_user(db_session: Session, account_id, name: str) -> User:
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "email": f"{name}@example.com",
            "username": name,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def test_policy_owners_are_found_in_sql_not_in_a_capped_scan(
    db_session: Session, test_user: User
) -> None:
    from preloop.models.crud import (
        crud_role,
        crud_team,
        crud_team_role,
        crud_user_role,
    )
    from preloop.models.models.team import TeamMembership

    account_id = test_user.account_id
    # Plain members sort before the owners by username, so a capped scan
    # followed by a filter would return none of the owners below.
    viewer_role = crud_role.get_by_name(db_session, name="viewer")
    editor_role = crud_role.get_by_name(db_session, name="editor")
    assert viewer_role is not None and editor_role is not None
    for index in range(3):
        member = _account_user(db_session, account_id, f"aa-member-{index}")
        crud_user_role.create(
            db_session, obj_in={"user_id": member.id, "role_id": viewer_role.id}
        )
    editor = _account_user(db_session, account_id, "zz-editor")
    crud_user_role.create(
        db_session, obj_in={"user_id": editor.id, "role_id": editor_role.id}
    )
    team_editor = _account_user(db_session, account_id, "zz-team-editor")
    team = crud_team.create(
        db_session, obj_in={"account_id": account_id, "name": "Policy team"}
    )
    db_session.add(TeamMembership(team_id=team.id, user_id=team_editor.id))
    crud_team_role.create(
        db_session, obj_in={"team_id": team.id, "role_id": editor_role.id}
    )
    inactive = _account_user(db_session, account_id, "zz-inactive-editor")
    inactive.is_active = False
    crud_user_role.create(
        db_session, obj_in={"user_id": inactive.id, "role_id": editor_role.id}
    )
    db_session.flush()

    owners = delivery.policy_owners(db_session, account_id, limit=3)

    assert {owner.username for owner in owners} == {
        test_user.username,
        "zz-editor",
        "zz-team-editor",
    }

    # Same answer as the per-user permission walk used elsewhere.
    from preloop.utils.permissions import user_holds_permission

    everyone = crud_user.get_active_by_account(
        db_session, account_id=account_id, limit=100
    )
    expected = {
        user.id
        for user in everyone
        if user.is_superuser
        or user_holds_permission(db_session, user, "manage_policies")
    }
    uncapped = delivery.policy_owners(db_session, account_id)
    assert {owner.id for owner in uncapped} == expected | {test_user.id}


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
    assert "This notify rule did not block the call." in body_text
    # A denied call can also produce a notice, so never claim the call went
    # through: only this rule's effect is known.
    for body in (body_text, body_html):
        assert "The call was not blocked" not in body
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


def test_run_async_uses_a_helper_thread_inside_a_running_loop() -> None:
    import asyncio

    from preloop.services.policy_notice_delivery import _run_async

    async def value() -> str:
        return "sent"

    async def boom() -> str:
        raise ValueError("transport down")

    async def caller() -> None:
        # A loop is running here, so the helper thread path is taken.
        assert _run_async(value) == "sent"
        with pytest.raises(ValueError, match="transport down"):
            _run_async(boom)

    asyncio.run(caller())
    # No running loop: the direct asyncio.run path.
    assert _run_async(value) == "sent"


def test_workflow_without_url_persists_endpoint_deactivation(
    db_session: Session, test_user: User
) -> None:
    from preloop.models.crud import crud_approval_workflow

    workflow = crud_approval_workflow.create(
        db_session,
        account_id=str(test_user.account_id),
        obj_in={
            "name": "chat-slack",
            "approval_type": "slack",
            "approval_config": {"webhook_url": "https://hooks.example.test/notice"},
            "is_default": True,
        },
    )
    db_session.flush()
    hit = _hit(db_session, test_user)
    assert delivery.send_webhook_notice(db_session, hit) is True

    workflow.approval_config = {}
    db_session.flush()
    with patch.object(db_session, "commit", wraps=db_session.commit) as commit:
        assert delivery.send_webhook_notice(db_session, hit) is False

    commit.assert_called_once()
