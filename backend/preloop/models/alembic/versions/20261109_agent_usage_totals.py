"""Store agent usage totals with transactional updates and resumable backfill.

Revision ID: 20261109_agent_usage_totals
Revises: 20261107_session_turn_state
"""

from alembic import op
import sqlalchemy as sa

revision = "20261109_agent_usage_totals"
down_revision = "20261107_session_turn_state"
branch_labels = None
depends_on = None

DDL = r"""CREATE TABLE IF NOT EXISTS agent_usage_total (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
 account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 principal_type varchar(64), principal_id varchar(255), session_id uuid,
 request_count bigint NOT NULL DEFAULT 0,
success_count bigint NOT NULL DEFAULT 0,
error_count bigint NOT NULL DEFAULT 0,
prompt_tokens bigint NOT NULL DEFAULT 0,
completion_tokens bigint NOT NULL DEFAULT 0,
total_tokens bigint NOT NULL DEFAULT 0,
cache_read_tokens bigint NOT NULL DEFAULT 0,
cache_write_tokens bigint NOT NULL DEFAULT 0,
covered_prompt_tokens bigint NOT NULL DEFAULT 0,
estimated_cost numeric NOT NULL DEFAULT 0,
sub_requests bigint NOT NULL DEFAULT 0,
sub_prompt bigint NOT NULL DEFAULT 0,
sub_completion bigint NOT NULL DEFAULT 0,
sub_total bigint NOT NULL DEFAULT 0,
sub_covered bigint NOT NULL DEFAULT 0,
sub_equivalent numeric NOT NULL DEFAULT 0,
 last_request_at timestamp NOT NULL, last_usage_id uuid NOT NULL,
 latest_model_alias varchar(255), latest_provider_name varchar(255),
 CONSTRAINT uq_agent_usage_total_scope UNIQUE NULLS NOT DISTINCT (account_id, principal_type, principal_id, session_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_usage_total_session ON agent_usage_total(account_id,session_id);
CREATE TABLE IF NOT EXISTS agent_usage_counted (usage_id uuid PRIMARY KEY, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS agent_usage_backfill (name varchar(32) PRIMARY KEY, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(), last_usage_id uuid, ready boolean NOT NULL DEFAULT false);
INSERT INTO agent_usage_backfill(name) VALUES ('agents') ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION agent_usage_apply(u api_usage, delta integer, replacing boolean DEFAULT false) RETURNS void LANGUAGE plpgsql AS $$
DECLARE latest api_usage; total_id uuid; removed_latest boolean;
BEGIN
 IF delta = 1 THEN
  INSERT INTO agent_usage_total(account_id,principal_type,principal_id,session_id,request_count,
       success_count,
       error_count,
       prompt_tokens,
       completion_tokens,
       total_tokens,
       cache_read_tokens,
       cache_write_tokens,
       covered_prompt_tokens,
       estimated_cost,
       sub_requests,
       sub_prompt,
       sub_completion,
       sub_total,
       sub_covered,
       sub_equivalent,last_request_at,last_usage_id,latest_model_alias,latest_provider_name)
  VALUES(u.account_id,u.runtime_principal_type,u.runtime_principal_id,u.runtime_session_id,1,
       CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END,
       CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END,
       COALESCE(u.prompt_tokens,0),
       COALESCE(u.completion_tokens,0),
       COALESCE(u.total_tokens,0),
       COALESCE(u.cache_read_tokens,0),
       COALESCE(u.cache_creation_tokens,0),
       CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END,
       COALESCE(u.estimated_cost,0)::numeric,
       CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END,
       CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END,
       CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END,
       CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END,
       CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END,
       CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END,u.timestamp,u.id,u.model_alias,u.provider_name)
  ON CONFLICT ON CONSTRAINT uq_agent_usage_total_scope DO UPDATE SET
  request_count=agent_usage_total.request_count+excluded.request_count,
       success_count=agent_usage_total.success_count+excluded.success_count,
       error_count=agent_usage_total.error_count+excluded.error_count,
       prompt_tokens=agent_usage_total.prompt_tokens+excluded.prompt_tokens,
       completion_tokens=agent_usage_total.completion_tokens+excluded.completion_tokens,
       total_tokens=agent_usage_total.total_tokens+excluded.total_tokens,
       cache_read_tokens=agent_usage_total.cache_read_tokens+excluded.cache_read_tokens,
       cache_write_tokens=agent_usage_total.cache_write_tokens+excluded.cache_write_tokens,
       covered_prompt_tokens=agent_usage_total.covered_prompt_tokens+excluded.covered_prompt_tokens,
       estimated_cost=agent_usage_total.estimated_cost+excluded.estimated_cost,
       sub_requests=agent_usage_total.sub_requests+excluded.sub_requests,
       sub_prompt=agent_usage_total.sub_prompt+excluded.sub_prompt,
       sub_completion=agent_usage_total.sub_completion+excluded.sub_completion,
       sub_total=agent_usage_total.sub_total+excluded.sub_total,
       sub_covered=agent_usage_total.sub_covered+excluded.sub_covered,
       sub_equivalent=agent_usage_total.sub_equivalent+excluded.sub_equivalent,
  last_request_at=CASE WHEN (excluded.last_request_at,excluded.last_usage_id) > (agent_usage_total.last_request_at,agent_usage_total.last_usage_id) THEN excluded.last_request_at ELSE agent_usage_total.last_request_at END,
  last_usage_id=CASE WHEN (excluded.last_request_at,excluded.last_usage_id) > (agent_usage_total.last_request_at,agent_usage_total.last_usage_id) THEN excluded.last_usage_id ELSE agent_usage_total.last_usage_id END,
  latest_model_alias=CASE WHEN (excluded.last_request_at,excluded.last_usage_id) >= (agent_usage_total.last_request_at,agent_usage_total.last_usage_id) THEN excluded.latest_model_alias ELSE agent_usage_total.latest_model_alias END,
  latest_provider_name=CASE WHEN (excluded.last_request_at,excluded.last_usage_id) >= (agent_usage_total.last_request_at,agent_usage_total.last_usage_id) THEN excluded.latest_provider_name ELSE agent_usage_total.latest_provider_name END;
 ELSE
  UPDATE agent_usage_total SET request_count=request_count-(1),
       success_count=success_count-(CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END),
       error_count=error_count-(CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END),
       prompt_tokens=prompt_tokens-(COALESCE(u.prompt_tokens,0)),
       completion_tokens=completion_tokens-(COALESCE(u.completion_tokens,0)),
       total_tokens=total_tokens-(COALESCE(u.total_tokens,0)),
       cache_read_tokens=cache_read_tokens-(COALESCE(u.cache_read_tokens,0)),
       cache_write_tokens=cache_write_tokens-(COALESCE(u.cache_creation_tokens,0)),
       covered_prompt_tokens=covered_prompt_tokens-(CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       estimated_cost=estimated_cost-(COALESCE(u.estimated_cost,0)::numeric),
       sub_requests=sub_requests-(CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END),
       sub_prompt=sub_prompt-(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       sub_completion=sub_completion-(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END),
       sub_total=sub_total-(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END),
       sub_covered=sub_covered-(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END),
       sub_equivalent=sub_equivalent-(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END)
  WHERE account_id=u.account_id AND principal_type IS NOT DISTINCT FROM u.runtime_principal_type AND principal_id IS NOT DISTINCT FROM u.runtime_principal_id AND session_id IS NOT DISTINCT FROM u.runtime_session_id RETURNING id, last_usage_id=u.id INTO total_id, removed_latest;
  IF total_id IS NULL THEN RETURN; END IF;
  DELETE FROM agent_usage_total WHERE id=total_id AND request_count=0;
  IF removed_latest AND FOUND THEN RETURN; END IF;
  IF removed_latest AND NOT replacing THEN
   -- Preserve index bounds when a latest row is deleted or moves scope.
   IF u.runtime_session_id IS NOT NULL THEN
    SELECT * INTO latest FROM api_usage WHERE account_id=u.account_id AND runtime_principal_type IS NOT DISTINCT FROM u.runtime_principal_type AND runtime_principal_id IS NOT DISTINCT FROM u.runtime_principal_id AND runtime_session_id=u.runtime_session_id AND action_type='model_gateway' AND id<>u.id ORDER BY timestamp DESC,id DESC LIMIT 1;
   ELSIF u.runtime_principal_type IS NOT NULL AND u.runtime_principal_id IS NOT NULL THEN
    SELECT * INTO latest FROM api_usage WHERE account_id=u.account_id AND runtime_principal_type=u.runtime_principal_type AND runtime_principal_id=u.runtime_principal_id AND runtime_session_id IS NULL AND action_type='model_gateway' AND id<>u.id ORDER BY timestamp DESC,id DESC LIMIT 1;
   ELSE
    SELECT * INTO latest FROM api_usage WHERE account_id=u.account_id AND runtime_principal_type IS NOT DISTINCT FROM u.runtime_principal_type AND runtime_principal_id IS NOT DISTINCT FROM u.runtime_principal_id AND runtime_session_id IS NOT DISTINCT FROM u.runtime_session_id AND action_type='model_gateway' AND id<>u.id ORDER BY timestamp DESC,id DESC LIMIT 1;
   END IF;
   IF latest.id IS NOT NULL THEN
    UPDATE agent_usage_total SET last_request_at=latest.timestamp,last_usage_id=latest.id,latest_model_alias=latest.model_alias,latest_provider_name=latest.provider_name WHERE id=total_id;
   END IF;
  END IF;
 END IF;
END $$;
CREATE OR REPLACE FUNCTION agent_usage_track() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE counted uuid;
BEGIN
 IF TG_OP <> 'INSERT' THEN
  DELETE FROM agent_usage_counted WHERE usage_id=OLD.id RETURNING usage_id INTO counted;
  IF counted IS NOT NULL THEN
   -- Same-key price/token corrections retain their latest-row position.
   -- Avoid rescanning history on the ingestion path for these updates.
   PERFORM agent_usage_apply(OLD,-1,
    TG_OP='UPDATE' AND COALESCE(NEW.action_type='model_gateway',false) AND OLD.account_id IS NOT DISTINCT FROM NEW.account_id
    AND OLD.runtime_principal_type IS NOT DISTINCT FROM NEW.runtime_principal_type
    AND OLD.runtime_principal_id IS NOT DISTINCT FROM NEW.runtime_principal_id
    AND OLD.runtime_session_id IS NOT DISTINCT FROM NEW.runtime_session_id
    AND OLD.timestamp IS NOT DISTINCT FROM NEW.timestamp);
  END IF;
 END IF;
 IF TG_OP <> 'DELETE' AND NEW.action_type='model_gateway' AND NEW.account_id IS NOT NULL
    AND EXISTS(SELECT 1 FROM account WHERE id=NEW.account_id) THEN
  INSERT INTO agent_usage_counted(usage_id) VALUES(NEW.id) ON CONFLICT DO NOTHING RETURNING usage_id INTO counted;
  IF counted IS NOT NULL THEN PERFORM agent_usage_apply(NEW,1); END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER agent_usage_track AFTER INSERT OR UPDATE OR DELETE ON api_usage FOR EACH ROW EXECUTE FUNCTION agent_usage_track();
"""

BACKFILL = """
WITH batch AS MATERIALIZED (
 SELECT u AS usage, u.id FROM api_usage u
 WHERE u.action_type='model_gateway' AND u.account_id IS NOT NULL
 AND u.id > COALESCE((SELECT last_usage_id FROM agent_usage_backfill WHERE name='agents'), '00000000-0000-0000-0000-000000000000'::uuid)
 ORDER BY u.id LIMIT 500 FOR UPDATE OF u
), claimed AS (
 INSERT INTO agent_usage_counted(usage_id) SELECT id FROM batch
 ON CONFLICT DO NOTHING RETURNING usage_id
), applied AS MATERIALIZED (
 SELECT agent_usage_apply(batch.usage,1) FROM batch JOIN claimed ON claimed.usage_id=batch.id
)
UPDATE agent_usage_backfill SET
 last_usage_id=COALESCE((SELECT id FROM batch ORDER BY id DESC LIMIT 1),last_usage_id),
 ready=NOT EXISTS(SELECT 1 FROM batch)
WHERE name='agents' AND (SELECT count(*) FROM applied)>=0
RETURNING ready
"""


def upgrade() -> None:
    """Install capture before scanning history; commit each bounded batch.

    The ID ledger makes each batch retryable. Source row locks serialize the
    backfill with updates/deletes; new inserts are counted by the trigger.
    Reads stay on the existing path until the complete backfill is committed.
    """
    op.execute(sa.text(DDL))
    with op.get_context().autocommit_block():
        while not op.get_bind().execute(sa.text(BACKFILL)).scalar_one():
            pass


def downgrade() -> None:
    """Remove derived totals; retain all source usage records."""
    op.execute("DROP TRIGGER IF EXISTS agent_usage_track ON api_usage")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_track()")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_apply(api_usage, integer, boolean)")
    op.drop_table("agent_usage_counted")
    op.drop_table("agent_usage_backfill")
    op.drop_table("agent_usage_total")
