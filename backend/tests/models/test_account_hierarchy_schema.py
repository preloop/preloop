"""Account hierarchy schema (#986): constraints, helpers and row defaults."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.models.hierarchy import (
    ancestors,
    child_position,
    descendants,
    place_under,
)


def _account(db_session, name: str) -> models.Account:
    account = models.Account(organization_name=name)
    db_session.add(account)
    db_session.flush()
    return account


def _user(db_session, account, email: str, *, verified: bool) -> models.User:
    user = models.User(
        account_id=account.id,
        username=f"u-{uuid.uuid4().hex[:12]}",
        email=email,
        email_verified=verified,
        hashed_password="hash",
        user_source="local",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _rejected_by(db_session, constraint: str, account: models.Account) -> None:
    savepoint = db_session.begin_nested()
    db_session.add(account)
    with pytest.raises(IntegrityError) as caught:
        db_session.flush()
    savepoint.rollback()
    assert constraint in str(caught.value)


def _relax_depth_limit(db_session) -> None:
    """Lift the launch depth limit for this test's transaction only."""
    db_session.execute(
        text("ALTER TABLE account DROP CONSTRAINT ck_account_hierarchy_depth_max")
    )


def _tree(db_session, depth: int) -> list[models.Account]:
    """A chain root -> ... of ``depth + 1`` accounts."""
    chain = [_account(db_session, "level-0")]
    for level in range(1, depth + 1):
        child = place_under(
            models.Account(organization_name=f"level-{level}"), chain[-1]
        )
        db_session.add(child)
        db_session.flush()
        chain.append(child)
    return chain


# --- defaults for rows created by existing code -----------------------------


def test_new_account_is_a_root(db_session):
    account = _account(db_session, "root")

    assert account.parent_account_id is None
    assert account.root_account_id == account.id
    assert account.hierarchy_path == [account.id]
    assert account.hierarchy_depth == 0


def test_subaccount_without_position_is_refused(db_session):
    parent = _account(db_session, "parent")
    savepoint = db_session.begin_nested()
    db_session.add(
        models.Account(organization_name="child", parent_account_id=parent.id)
    )

    with pytest.raises(ValueError, match="place_under"):
        db_session.flush()
    savepoint.rollback()


def test_new_user_gets_its_own_direct_person(db_session):
    account = _account(db_session, "acme")
    user = _user(db_session, account, " Alice@Example.com ", verified=True)

    assert user.membership_kind == "direct"
    assert user.access_grant_id is None
    person = db_session.get(models.Person, user.person_id)
    assert person.email_normalized == "alice@example.com"
    assert person.email_verified_at is not None
    assert person.primary_user_id == user.id
    assert person.last_active_user_id == user.id


def test_new_rows_are_never_merged_into_an_existing_person(db_session):
    """Creating a row links nothing: a second verified row stays provisional."""
    first = _user(
        db_session, _account(db_session, "a"), "bob@example.com", verified=True
    )
    second = _user(
        db_session, _account(db_session, "b"), "BOB@example.com", verified=True
    )
    unverified = _user(
        db_session, _account(db_session, "c"), "bob@example.com", verified=False
    )

    persons = {first.person_id, second.person_id, unverified.person_id}
    assert len(persons) == 3
    assert db_session.get(models.Person, second.person_id).email_verified_at is None
    assert db_session.get(models.Person, unverified.person_id).email_verified_at is None


def test_two_verified_rows_in_one_flush_do_not_collide(db_session):
    a, b = _account(db_session, "a"), _account(db_session, "b")
    for account in (a, b):
        db_session.add(
            models.User(
                account_id=account.id,
                username=f"u-{uuid.uuid4().hex[:12]}",
                email="carol@example.com",
                email_verified=True,
                user_source="local",
            )
        )
    db_session.flush()

    verified = db_session.scalars(
        select(models.Person).where(
            models.Person.email_normalized == "carol@example.com",
            models.Person.email_verified_at.is_not(None),
        )
    ).all()
    assert len(verified) == 1


# --- CHECK constraints -------------------------------------------------------


def test_child_with_depth_zero_is_rejected(db_session):
    parent = _account(db_session, "parent")
    child_id = uuid.uuid4()
    child = models.Account(
        id=child_id,
        organization_name="child",
        parent_account_id=parent.id,
        root_account_id=child_id,
        hierarchy_path=[child_id],
        hierarchy_depth=0,
    )
    _rejected_by(db_session, "ck_account_parent_iff_nonroot", child)


def test_root_with_depth_one_is_rejected(db_session):
    other = _account(db_session, "other")
    root_id = uuid.uuid4()
    root = models.Account(
        id=root_id,
        organization_name="root",
        parent_account_id=None,
        root_account_id=other.id,
        hierarchy_path=[other.id, root_id],
        hierarchy_depth=1,
    )
    _rejected_by(db_session, "ck_account_parent_iff_nonroot", root)


def test_depth_two_is_rejected_at_launch(db_session):
    root = _account(db_session, "root")
    middle = place_under(models.Account(organization_name="middle"), root)
    db_session.add(middle)
    db_session.flush()
    assert middle.hierarchy_depth == 1

    grandchild = place_under(models.Account(organization_name="grandchild"), middle)
    assert grandchild.hierarchy_depth == 2
    _rejected_by(db_session, "ck_account_hierarchy_depth_max", grandchild)


def test_path_must_end_at_self_and_start_at_root(db_session):
    root = _account(db_session, "root")
    child_id = uuid.uuid4()
    child = models.Account(
        id=child_id,
        organization_name="child",
        parent_account_id=root.id,
        root_account_id=root.id,
        hierarchy_path=[root.id, uuid.uuid4()],
        hierarchy_depth=1,
    )
    _rejected_by(db_session, "ck_account_hierarchy_path_shape", child)


def test_parent_with_children_cannot_be_deleted(db_session):
    root = _account(db_session, "root")
    child = place_under(models.Account(organization_name="child"), root)
    db_session.add(child)
    db_session.flush()

    savepoint = db_session.begin_nested()
    with pytest.raises(IntegrityError, match="fk_account_parent"):
        db_session.execute(text("DELETE FROM account WHERE id = :id"), {"id": root.id})
    savepoint.rollback()


# --- ancestors / descendants -------------------------------------------------


def test_helpers_on_a_depth_one_tree(db_session):
    root = _account(db_session, "root")
    children = []
    for name in ("east", "west"):
        child = place_under(models.Account(organization_name=name), root)
        db_session.add(child)
        children.append(child)
    unrelated = _account(db_session, "unrelated")
    db_session.flush()

    assert ancestors(db_session, root) == []
    assert {a.id for a in descendants(db_session, root)} == {c.id for c in children}
    for child in children:
        assert ancestors(db_session, child) == [root]
        assert descendants(db_session, child) == []
        assert child.root_account_id == root.id
    assert descendants(db_session, unrelated) == []


def test_helpers_on_a_depth_three_tree(db_session):
    """Depth is configuration: with the CHECK relaxed, nothing else changes."""
    _relax_depth_limit(db_session)
    chain = _tree(db_session, depth=3)
    sibling = place_under(models.Account(organization_name="sibling"), chain[1])
    db_session.add(sibling)
    db_session.flush()

    leaf = chain[3]
    assert leaf.hierarchy_depth == 3
    assert leaf.hierarchy_path == [a.id for a in chain]
    assert ancestors(db_session, leaf) == chain[:3]
    assert ancestors(db_session, chain[1]) == [chain[0]]
    assert [a.id for a in descendants(db_session, chain[0])][:1] == [chain[1].id]
    assert {a.id for a in descendants(db_session, chain[0])} == {
        chain[1].id,
        chain[2].id,
        chain[3].id,
        sibling.id,
    }
    assert {a.id for a in descendants(db_session, chain[1])} == {
        chain[2].id,
        chain[3].id,
        sibling.id,
    }
    assert [a.id for a in descendants(db_session, chain[2])] == [chain[3].id]
    assert descendants(db_session, leaf) == []


def test_child_position_needs_a_placed_parent():
    parent = models.Account(id=uuid.uuid4())
    with pytest.raises(ValueError):
        child_position(parent, uuid.uuid4())


# --- tags, grants, shares, rules ---------------------------------------------


def _tag(account, key: str) -> models.ResourceTag:
    return models.ResourceTag(
        account_id=account.id,
        resource_type="flow",
        resource_id=uuid.uuid4(),
        key=key,
        value="prod",
    )


def test_tag_key_accepts_the_documented_alphabet(db_session):
    account = _account(db_session, "tags")
    db_session.add(_tag(account, "team/platform.env_x-1"))
    db_session.add(_tag(account, "k" * 63))
    db_session.flush()


@pytest.mark.parametrize("key", ["Env", "ENV", "k" * 64, "", "has space"])
def test_tag_key_check_rejects(db_session, key):
    account = _account(db_session, "tags")
    savepoint = db_session.begin_nested()
    db_session.add(_tag(account, key))
    with pytest.raises(IntegrityError, match="ck_resource_tag_key"):
        db_session.flush()
    savepoint.rollback()


def test_tag_key_policy_uses_the_same_key_check(db_session):
    account = _account(db_session, "tags")
    savepoint = db_session.begin_nested()
    db_session.add(
        models.TagKeyPolicy(account_id=account.id, key="Env", governed_by="owner")
    )
    with pytest.raises(IntegrityError, match="ck_tag_key_policy_key"):
        db_session.flush()
    savepoint.rollback()


def test_inherited_membership_requires_a_grant(db_session):
    account = _account(db_session, "acme")
    user = _user(db_session, account, "dan@example.com", verified=True)

    savepoint = db_session.begin_nested()
    with pytest.raises(IntegrityError, match="ck_user_inherited_has_grant"):
        db_session.execute(
            text("UPDATE \"user\" SET membership_kind = 'inherited' WHERE id = :id"),
            {"id": user.id},
        )
    savepoint.rollback()


def test_one_person_holds_one_row_per_account(db_session):
    account = _account(db_session, "acme")
    first = _user(db_session, account, "erin@example.com", verified=True)
    second = _user(db_session, account, "erin+2@example.com", verified=True)

    savepoint = db_session.begin_nested()
    with pytest.raises(IntegrityError, match="uq_user_person_account"):
        db_session.execute(
            text('UPDATE "user" SET person_id = :person WHERE id = :id'),
            {"person": first.person_id, "id": second.id},
        )
    savepoint.rollback()


def test_grant_share_tag_and_rule_rows_round_trip(db_session):
    parent = _account(db_session, "parent")
    child = place_under(models.Account(organization_name="child"), parent)
    db_session.add(child)
    admin = _user(db_session, parent, "frank@example.com", verified=True)

    grant = models.AccountAccessGrant(
        parent_account_id=parent.id,
        subject_type="user",
        subject_id=admin.id,
        access_level="operate",
        target_mode="selected",
        created_by=admin.id,
    )
    db_session.add(grant)
    db_session.flush()
    db_session.execute(
        models.account_access_grant_target.insert().values(
            grant_id=grant.id, subaccount_id=child.id
        )
    )

    rule = models.AccessRule(
        account_id=parent.id,
        name="prod models only",
        effect="permit",
        actions=["model:invoke"],
        resource_type="ai_model",
        resource_selector={"env": "prod"},
        scope="subaccounts",
    )
    db_session.add(rule)
    db_session.flush()

    agent_id = uuid.uuid4()
    share = models.ResourceShare(
        owner_account_id=parent.id,
        resource_type="managed_agent",
        resource_id=agent_id,
        target_mode="rule",
        access_rule_id=rule.id,
    )
    db_session.add(share)
    db_session.flush()
    db_session.add(
        models.ResourceShareRecipient(
            share_id=share.id,
            recipient_account_id=child.id,
            owner_account_id=parent.id,
            resource_type="managed_agent",
            resource_id=agent_id,
        )
    )
    db_session.add(
        models.TagKeyPolicy(
            account_id=parent.id,
            key="env",
            governed_by="parent",
            allowed_values=["prod"],
        )
    )
    db_session.flush()

    shared = db_session.scalars(
        select(models.ResourceShareRecipient.resource_id).where(
            models.ResourceShareRecipient.recipient_account_id == child.id,
            models.ResourceShareRecipient.resource_type == "managed_agent",
        )
    ).all()
    assert shared == [agent_id]
    assert rule.is_enabled is True and rule.version == 1 and rule.priority == 0


def test_rule_share_needs_a_rule_and_rule_actions_are_closed(db_session):
    account = _account(db_session, "acme")
    savepoint = db_session.begin_nested()
    db_session.add(
        models.ResourceShare(
            owner_account_id=account.id,
            resource_type="flow",
            resource_id=uuid.uuid4(),
            target_mode="rule",
        )
    )
    with pytest.raises(IntegrityError, match="ck_resource_share_rule_mode"):
        db_session.flush()
    savepoint.rollback()

    savepoint = db_session.begin_nested()
    db_session.add(
        models.AccessRule(
            account_id=account.id,
            name="bad",
            effect="permit",
            actions=["model:delete"],
        )
    )
    with pytest.raises(IntegrityError, match="ck_access_rule_actions"):
        db_session.flush()
    savepoint.rollback()


def test_parent_must_be_the_path_element_above_self(db_session):
    """parent_account_id and hierarchy_path encode one edge; they cannot drift."""
    root = _account(db_session, "root")
    stranger = _account(db_session, "stranger")
    child_id = uuid.uuid4()
    child = models.Account(
        id=child_id,
        organization_name="child",
        parent_account_id=stranger.id,
        root_account_id=root.id,
        hierarchy_path=[root.id, child_id],
        hierarchy_depth=1,
    )
    _rejected_by(db_session, "ck_account_parent_is_path_tail", child)


def _live_share(db_session):
    owner = _account(db_session, "owner")
    recipient = place_under(models.Account(organization_name="recipient"), owner)
    db_session.add(recipient)
    share = models.ResourceShare(
        owner_account_id=owner.id,
        resource_type="ai_model",
        resource_id=uuid.uuid4(),
        target_mode="all",
    )
    db_session.add(share)
    db_session.flush()
    return share, recipient


def _recipient(share, account) -> models.ResourceShareRecipient:
    return models.ResourceShareRecipient(
        share_id=share.id,
        recipient_account_id=account.id,
        owner_account_id=share.owner_account_id,
        resource_type=share.resource_type,
        resource_id=share.resource_id,
    )


def test_revoking_a_share_removes_its_recipient_rows(db_session):
    """Hot paths read only the recipient table, so revocation must reach it."""
    share, recipient = _live_share(db_session)
    db_session.add(_recipient(share, recipient))
    db_session.flush()

    db_session.execute(
        text("UPDATE resource_share SET revoked_at = now() WHERE id = :id"),
        {"id": share.id},
    )

    remaining = db_session.execute(
        text("SELECT count(*) FROM resource_share_recipient WHERE share_id = :id"),
        {"id": share.id},
    ).scalar()
    assert remaining == 0


def test_a_revoked_share_cannot_gain_recipients(db_session):
    share, recipient = _live_share(db_session)
    db_session.execute(
        text("UPDATE resource_share SET revoked_at = now() WHERE id = :id"),
        {"id": share.id},
    )

    savepoint = db_session.begin_nested()
    db_session.add(_recipient(share, recipient))
    with pytest.raises(IntegrityError, match="revoked"):
        db_session.flush()
    savepoint.rollback()


def test_rule_and_tag_resource_types_are_a_closed_set(db_session):
    """A typo in a rule's or tag's resource type must fail, not match nothing."""
    account = _account(db_session, "typos")

    def rule(resource_type):
        return models.AccessRule(
            account_id=account.id,
            name="no models",
            effect="forbid",
            actions=["model:invoke"],
            resource_type=resource_type,
        )

    savepoint = db_session.begin_nested()
    db_session.add(rule("models"))
    with pytest.raises(IntegrityError, match="ck_access_rule_resource_type"):
        db_session.flush()
    savepoint.rollback()

    tag = _tag(account, "env")
    tag.resource_type = "models"
    savepoint = db_session.begin_nested()
    db_session.add(tag)
    with pytest.raises(IntegrityError, match="ck_resource_tag_resource_type"):
        db_session.flush()
    savepoint.rollback()

    # NULL on a rule still means every resource type.
    db_session.add_all([rule(None), rule("ai_model")])
    db_session.flush()
