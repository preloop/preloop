"""Add resource sharing, resource tags, tag key policies and access rules.

Revision ID: 20260928_share_tag_rule
Revises: 20260928_person_membership
Create Date: 2026-09-28

Last of four revisions for the account hierarchy (#986). Tables only, no
rows. ``resource_share_recipient`` is the materialized table hot paths read,
through ``(recipient_account_id, resource_type)``; two triggers keep it free
of revoked shares. Tags are separate from
system metadata such as ``managed_agent.tags`` and runner ``labels``.
Idempotent: each table is created only if it is missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "20260928_share_tag_rule"
down_revision = "20260928_person_membership"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

# Spelled out rather than imported from the models, so this revision does not
# change when the models do.
_RESOURCE_TYPES = (
    "resource_type IN ('ai_model', 'mcp_server', 'managed_agent', 'flow',"
    " 'runner_pool', 'policy_baseline')"
)
_TAG_KEY = "key ~ '^[a-z0-9._/-]{1,63}$'"
_ACTIONS = (
    "cardinality(actions) >= 1 AND actions <@ ARRAY['model:invoke', 'tool:call',"
    " 'flow:run', 'runner:accept', 'resource:view', 'resource:share']::text[]"
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def _account_fk(name: str, comment: str | None = None) -> sa.Column:
    return sa.Column(
        name,
        UUID(as_uuid=True),
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        comment=comment,
    )


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        UUID(as_uuid=True),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )


def _create_access_rule() -> None:
    op.create_table(
        "access_rule",
        *_base_columns(),
        _account_fk("account_id"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("effect", sa.String(16), nullable=False, comment="permit | forbid"),
        sa.Column("actions", ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "subject_selector",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "resource_type",
            sa.String(32),
            nullable=True,
            comment="One of the shareable resource types; NULL matches every type",
        ),
        sa.Column(
            "resource_selector",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "conditions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "scope",
            sa.String(32),
            nullable=False,
            server_default="self",
            comment="self | subaccounts | self_and_subaccounts",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _user_fk("created_by"),
        sa.CheckConstraint(
            "effect IN ('permit', 'forbid')", name="ck_access_rule_effect"
        ),
        sa.CheckConstraint(
            "scope IN ('self', 'subaccounts', 'self_and_subaccounts')",
            name="ck_access_rule_scope",
        ),
        sa.CheckConstraint(
            "resource_type IS NULL OR " + _RESOURCE_TYPES,
            name="ck_access_rule_resource_type",
        ),
        sa.CheckConstraint(_ACTIONS, name="ck_access_rule_actions"),
    )
    op.create_index("ix_access_rule_id", "access_rule", ["id"])
    op.create_index(
        "ix_access_rule_account_enabled", "access_rule", ["account_id", "is_enabled"]
    )


def _create_resource_share() -> None:
    op.create_table(
        "resource_share",
        *_base_columns(),
        _account_fk("owner_account_id", "Account that owns the shared resource"),
        sa.Column(
            "resource_type",
            sa.String(32),
            nullable=False,
            comment=(
                "ai_model | mcp_server | managed_agent | flow | runner_pool | "
                "policy_baseline"
            ),
        ),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "target_mode",
            sa.String(16),
            nullable=False,
            comment="all | selected | rule",
        ),
        sa.Column(
            "access_rule_id",
            UUID(as_uuid=True),
            sa.ForeignKey("access_rule.id", ondelete="RESTRICT"),
            nullable=True,
            comment="Rule selecting recipients; set exactly when target_mode is rule",
        ),
        _user_fk("created_by"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("revoked_by"),
        sa.CheckConstraint(_RESOURCE_TYPES, name="ck_resource_share_resource_type"),
        sa.CheckConstraint(
            "target_mode IN ('all', 'selected', 'rule')",
            name="ck_resource_share_target_mode",
        ),
        sa.CheckConstraint(
            "(target_mode = 'rule') = (access_rule_id IS NOT NULL)",
            name="ck_resource_share_rule_mode",
        ),
    )
    op.create_index("ix_resource_share_id", "resource_share", ["id"])
    op.create_index(
        "ix_resource_share_access_rule_id", "resource_share", ["access_rule_id"]
    )
    op.create_index(
        "ix_resource_share_owner_resource",
        "resource_share",
        ["owner_account_id", "resource_type", "resource_id"],
    )


def _create_resource_share_recipient() -> None:
    op.create_table(
        "resource_share_recipient",
        *_base_columns(),
        sa.Column(
            "share_id",
            UUID(as_uuid=True),
            sa.ForeignKey("resource_share.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _account_fk("recipient_account_id"),
        _account_fk(
            "owner_account_id",
            "Copied from the share so the hot-path join needs no second table",
        ),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint(
            "recipient_account_id",
            "resource_type",
            "resource_id",
            "share_id",
            name="uq_resource_share_recipient",
        ),
        sa.CheckConstraint(
            _RESOURCE_TYPES, name="ck_resource_share_recipient_resource_type"
        ),
    )
    op.create_index(
        "ix_resource_share_recipient_id", "resource_share_recipient", ["id"]
    )
    op.create_index(
        "ix_resource_share_recipient_share_id",
        "resource_share_recipient",
        ["share_id"],
    )
    op.create_index(
        "ix_resource_share_recipient_type",
        "resource_share_recipient",
        ["recipient_account_id", "resource_type"],
    )


def _create_resource_tag() -> None:
    op.create_table(
        "resource_tag",
        *_base_columns(),
        _account_fk("account_id", "Account that owns the tagged resource"),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "key",
            sa.Text(),
            nullable=False,
            comment="Lowercase [a-z0-9._/-], 1 to 63 characters",
        ),
        sa.Column("value", sa.String(128), nullable=False),
        _user_fk("created_by"),
        sa.CheckConstraint(_TAG_KEY, name="ck_resource_tag_key"),
        sa.CheckConstraint(_RESOURCE_TYPES, name="ck_resource_tag_resource_type"),
        sa.UniqueConstraint(
            "resource_type", "resource_id", "key", name="uq_resource_tag_key"
        ),
    )
    op.create_index("ix_resource_tag_id", "resource_tag", ["id"])
    op.create_index(
        "ix_resource_tag_account_lookup",
        "resource_tag",
        ["account_id", "resource_type", "key", "value"],
    )


def _create_tag_key_policy() -> None:
    op.create_table(
        "tag_key_policy",
        *_base_columns(),
        _account_fk("account_id"),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "governed_by", sa.String(16), nullable=False, comment="owner | parent"
        ),
        sa.Column(
            "allowed_values",
            ARRAY(sa.Text()),
            nullable=True,
            comment="NULL means any value",
        ),
        sa.CheckConstraint(_TAG_KEY, name="ck_tag_key_policy_key"),
        sa.CheckConstraint(
            "governed_by IN ('owner', 'parent')",
            name="ck_tag_key_policy_governed_by",
        ),
        sa.UniqueConstraint("account_id", "key", name="uq_tag_key_policy_account_key"),
    )
    op.create_index("ix_tag_key_policy_id", "tag_key_policy", ["id"])


# Creation order: resource_share references access_rule, the recipient table
# references resource_share.
_TABLES = (
    ("access_rule", _create_access_rule),
    ("resource_share", _create_resource_share),
    ("resource_share_recipient", _create_resource_share_recipient),
    ("resource_tag", _create_resource_tag),
    ("tag_key_policy", _create_tag_key_policy),
)


# Hot paths read resource_share_recipient alone, so a recipient row may exist
# only while its share is live. Revoking a share deletes its recipient rows,
# and a revoked share cannot gain new ones. The insert guard locks the share
# row FOR SHARE, which conflicts with the revoking UPDATE: a concurrent
# materializer either waits and then sees the revocation, or commits first and
# has its rows deleted by the revoke trigger (which reads a fresh snapshot).
_REVOKE_FUNCTION = """
CREATE OR REPLACE FUNCTION preloop_resource_share_revoked() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM resource_share_recipient WHERE share_id = NEW.id;
    RETURN NULL;
END
$$
"""
_LIVE_FUNCTION = """
CREATE OR REPLACE FUNCTION preloop_resource_share_recipient_live() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    share_revoked_at TIMESTAMPTZ;
BEGIN
    SELECT revoked_at INTO share_revoked_at
    FROM resource_share WHERE id = NEW.share_id
    FOR SHARE;
    IF share_revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'resource share % is revoked', NEW.share_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""
_TRIGGERS = (
    (
        "trg_resource_share_revoked",
        "resource_share",
        "AFTER UPDATE OF revoked_at ON resource_share FOR EACH ROW"
        " WHEN (NEW.revoked_at IS NOT NULL)"
        " EXECUTE FUNCTION preloop_resource_share_revoked()",
    ),
    (
        "trg_resource_share_recipient_live",
        "resource_share_recipient",
        "BEFORE INSERT OR UPDATE OF share_id ON resource_share_recipient"
        " FOR EACH ROW EXECUTE FUNCTION preloop_resource_share_recipient_live()",
    ),
)


def upgrade() -> None:
    """Create the missing tables, then (re)create the share triggers."""
    for name, create in _TABLES:
        if not _has_table(name):
            create()
    op.execute(_REVOKE_FUNCTION)
    op.execute(_LIVE_FUNCTION)
    for trigger, table, definition in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"CREATE TRIGGER {trigger} {definition}")


def downgrade() -> None:
    """Drop the sharing, tag and rule tables and the share triggers."""
    for name, _ in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")
    op.execute("DROP FUNCTION IF EXISTS preloop_resource_share_recipient_live()")
    op.execute("DROP FUNCTION IF EXISTS preloop_resource_share_revoked()")
