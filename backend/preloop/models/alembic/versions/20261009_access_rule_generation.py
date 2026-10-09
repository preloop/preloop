"""Account-access generation counters and committed cross-replica invalidation.

Revision ID: 20261009_access_rule_generation
Revises: 20261009_mcp_tool_shadow_prefix
"""

from alembic import op
import sqlalchemy as sa

revision = "20261009_access_rule_generation"
down_revision = "20261009_mcp_tool_shadow_prefix"
branch_labels = None
depends_on = None

# Changes to selectors, membership and resource identities invalidate account
# bundles. PostgreSQL delivers NOTIFY only at commit, never for rolled-back rules.
TABLES = {
    "access_rule": None,
    "resource_tag": None,
    "tag_key_policy": None,
    "user_role": None,
    "team_role": None,
    "team_membership": None,
    "team": "account_id",
    "role": "account_id",
    "account_access_grant": None,
    "user": "account_id, person_id, membership_kind, access_grant_id",
    "person": "primary_user_id",
    "api_key": "account_id, user_id, context_data",
    "managed_agent_credential": None,
    "runtime_session": "account_id, session_source_type, session_source_id",
    "managed_agent": "account_id, session_source_type, session_source_id",
    "ai_model": "account_id",
    "mcp_server": "account_id",
    "tool_configuration": "account_id",
    "flow": "account_id",
    "flow_runner": "account_id, labels",
    "account": "meta_data, hierarchy_path",
}


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column(
            "access_rule_generation", sa.Integer(), server_default="0", nullable=False
        ),
    )
    for table in ("resource_tag", "access_rule"):
        op.drop_constraint(f"ck_{table}_resource_type", table, type_="check")
    kinds = "'ai_model','mcp_server','managed_agent','flow','runner_pool','policy_baseline','account','mcp_tool','runner','policy','tracker','api_key'"
    op.create_check_constraint(
        "ck_resource_tag_resource_type", "resource_tag", f"resource_type IN ({kinds})"
    )
    op.create_check_constraint(
        "ck_access_rule_resource_type",
        "access_rule",
        f"resource_type IS NULL OR resource_type IN ({kinds})",
    )
    op.execute("""
    CREATE FUNCTION preloop_bump_access_generation() RETURNS trigger AS $$
    DECLARE data jsonb; payloads jsonb[]; owner uuid; affected record;
    BEGIN
      IF TG_OP = 'UPDATE' THEN payloads := ARRAY[to_jsonb(OLD), to_jsonb(NEW)];
      ELSIF TG_OP = 'DELETE' THEN payloads := ARRAY[to_jsonb(OLD)];
      ELSE payloads := ARRAY[to_jsonb(NEW)]; END IF;
      FOREACH data IN ARRAY payloads LOOP
      owner := NULLIF(data->>'account_id', '')::uuid;
      IF TG_TABLE_NAME = 'account' THEN owner := (data->>'id')::uuid;
      ELSIF TG_TABLE_NAME = 'account_access_grant' THEN owner := (data->>'parent_account_id')::uuid;
      ELSIF TG_TABLE_NAME = 'user_role' THEN
        SELECT account_id INTO owner FROM "user" WHERE id=(data->>'user_id')::uuid;
      ELSIF TG_TABLE_NAME IN ('team_role','team_membership') THEN
        SELECT account_id INTO owner FROM team WHERE id=(data->>'team_id')::uuid;
      ELSIF TG_TABLE_NAME = 'person' THEN
        SELECT account_id INTO owner FROM "user" WHERE id=(data->>'primary_user_id')::uuid;
      ELSIF TG_TABLE_NAME = 'managed_agent_credential' THEN
        SELECT account_id INTO owner FROM managed_agent WHERE id=(data->>'managed_agent_id')::uuid;
      END IF;
      IF owner IS NOT NULL THEN
        FOR affected IN
          UPDATE account SET access_rule_generation=access_rule_generation+1
          WHERE id=owner OR hierarchy_path @> ARRAY[owner]::uuid[] OR id IN (
            SELECT membership.account_id FROM "user" membership
            JOIN person p ON p.id=membership.person_id
            JOIN "user" home ON home.id=p.primary_user_id
            WHERE home.account_id=owner
          ) RETURNING id, access_rule_generation
        LOOP
          PERFORM pg_notify('preloop_access', json_build_object('account_id', affected.id,
                                     'generation', affected.access_rule_generation)::text);
        END LOOP;
      END IF;
      END LOOP;
      IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END; $$ LANGUAGE plpgsql;
    """)
    for table, columns in TABLES.items():
        update = "UPDATE" + (f" OF {columns}" if columns else "")
        op.execute(
            f'CREATE TRIGGER trg_access_generation AFTER INSERT OR DELETE OR {update} ON "{table}" FOR EACH ROW EXECUTE FUNCTION preloop_bump_access_generation()'
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP TRIGGER IF EXISTS trg_access_generation ON "{table}"')
    op.execute("DROP FUNCTION preloop_bump_access_generation()")
    # Refuse rollback while the newly taggable key kind is still in use.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM resource_tag WHERE resource_type='api_key') OR
         EXISTS (SELECT 1 FROM access_rule WHERE resource_type='api_key') THEN
        RAISE EXCEPTION 'Remove api_key tags/access rules before downgrading';
      END IF;
    END $$;""")
    kinds = "'ai_model','mcp_server','managed_agent','flow','runner_pool','policy_baseline','account','mcp_tool','runner','policy','tracker'"
    for table in ("resource_tag", "access_rule"):
        op.drop_constraint(f"ck_{table}_resource_type", table, type_="check")
    op.create_check_constraint(
        "ck_resource_tag_resource_type", "resource_tag", f"resource_type IN ({kinds})"
    )
    op.create_check_constraint(
        "ck_access_rule_resource_type",
        "access_rule",
        f"resource_type IS NULL OR resource_type IN ({kinds})",
    )
    op.drop_column("account", "access_rule_generation")
