"""Persist selected sharing intent and trusted consuming-account command attribution.

Revision ID: 20261009_resource_sharing_intent
Revises: 20261009_discovery_observation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "20261009_resource_sharing_intent"
down_revision = "20261009_discovery_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resource_share",
        sa.Column(
            "selected_account_ids",
            ARRAY(UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "resource_share",
        sa.Column("is_automatic", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "resource_share",
        sa.Column(
            "require_approval", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.execute(
        """UPDATE resource_share s SET selected_account_ids=(SELECT COALESCE(array_agg(r.recipient_account_id), '{}'::uuid[]) FROM resource_share_recipient r WHERE r.share_id=s.id) WHERE s.target_mode='selected'"""
    )
    op.drop_constraint(
        "resource_share_access_rule_id_fkey", "resource_share", type_="foreignkey"
    )
    op.create_foreign_key(
        "resource_share_access_rule_id_fkey",
        "resource_share",
        "access_rule",
        ["access_rule_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "agent_control_command",
        sa.Column("consuming_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_control_consuming_account",
        "agent_control_command",
        "account",
        ["consuming_account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_control_consuming_account",
        "agent_control_command",
        ["consuming_account_id", "managed_agent_id", "command_id"],
    )

    op.execute("""
    CREATE FUNCTION preloop_share_generation() RETURNS trigger AS $$
    DECLARE data jsonb; payloads jsonb[]; affected record;
    BEGIN
      IF TG_OP = 'UPDATE' THEN payloads := ARRAY[to_jsonb(OLD), to_jsonb(NEW)];
      ELSIF TG_OP = 'DELETE' THEN payloads := ARRAY[to_jsonb(OLD)];
      ELSE payloads := ARRAY[to_jsonb(NEW)]; END IF;
      FOREACH data IN ARRAY payloads LOOP
        FOR affected IN UPDATE account SET access_rule_generation=access_rule_generation+1
          WHERE id IN ((data->>'recipient_account_id')::uuid, (data->>'owner_account_id')::uuid)
          RETURNING id, access_rule_generation
        LOOP
          PERFORM pg_notify('preloop_access', json_build_object('account_id', affected.id,
                             'generation', affected.access_rule_generation)::text);
        END LOOP;
      END LOOP;
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER trg_share_generation AFTER INSERT OR DELETE OR UPDATE
      ON resource_share_recipient FOR EACH ROW EXECUTE FUNCTION preloop_share_generation();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_share_generation ON resource_share_recipient"
    )
    op.execute("DROP FUNCTION IF EXISTS preloop_share_generation()")
    op.drop_index(
        "ix_agent_control_consuming_account", table_name="agent_control_command"
    )
    op.drop_constraint(
        "fk_agent_control_consuming_account",
        "agent_control_command",
        type_="foreignkey",
    )
    op.drop_column("agent_control_command", "consuming_account_id")
    op.drop_constraint(
        "resource_share_access_rule_id_fkey", "resource_share", type_="foreignkey"
    )
    op.create_foreign_key(
        "resource_share_access_rule_id_fkey",
        "resource_share",
        "access_rule",
        ["access_rule_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for name in ("selected_account_ids", "is_automatic", "require_approval"):
        op.drop_column("resource_share", name)
