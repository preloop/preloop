"""Store agent usage totals with transactional updates and resumable backfill.

Revision ID: 20261109_agent_usage_totals
Revises: 20261108_control_reachable
"""

from alembic import op
import sqlalchemy as sa

revision = "20261109_agent_usage_totals"
down_revision = "20261108_control_reachable"
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
-- A batch writes each counter once. Row-by-row upserts of a shared scope
-- repeatedly rewrite its tuple and become quadratic for bulk imports.
CREATE OR REPLACE FUNCTION agent_usage_track_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 WITH claimed AS (
  INSERT INTO agent_usage_counted(usage_id)
  SELECT u.id FROM inserted_usage u
  WHERE u.action_type='model_gateway' AND u.account_id IS NOT NULL
  AND EXISTS(SELECT 1 FROM account WHERE id=u.account_id)
  ON CONFLICT DO NOTHING RETURNING usage_id
 )
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
  SELECT u.account_id,
       u.runtime_principal_type,
       u.runtime_principal_id,
       u.runtime_session_id,
       count(*),
       SUM(CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END),
       SUM(COALESCE(u.prompt_tokens,0)),
       SUM(COALESCE(u.completion_tokens,0)),
       SUM(COALESCE(u.total_tokens,0)),
       SUM(COALESCE(u.cache_read_tokens,0)),
       SUM(COALESCE(u.cache_creation_tokens,0)),
       SUM(CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       SUM(COALESCE(u.estimated_cost,0)::numeric),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END),
       max(u.timestamp),
       (array_agg(u.id ORDER BY u.timestamp DESC,u.id DESC))[1],
       (array_agg(u.model_alias ORDER BY u.timestamp DESC,u.id DESC))[1],
       (array_agg(u.provider_name ORDER BY u.timestamp DESC,u.id DESC))[1]
  FROM inserted_usage u JOIN claimed c ON c.usage_id=u.id
  GROUP BY u.account_id,u.runtime_principal_type,u.runtime_principal_id,u.runtime_session_id
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
 RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER agent_usage_track_insert AFTER INSERT ON api_usage
 REFERENCING NEW TABLE AS inserted_usage FOR EACH STATEMENT
 EXECUTE FUNCTION agent_usage_track_insert();

CREATE OR REPLACE FUNCTION agent_usage_track_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 WITH claimed AS (
  DELETE FROM agent_usage_counted c USING deleted_usage u
  WHERE c.usage_id=u.id RETURNING c.usage_id
 ), removed AS MATERIALIZED (
  SELECT u.account_id AS account_id,
       u.runtime_principal_type AS principal_type,
       u.runtime_principal_id AS principal_id,
       u.runtime_session_id AS session_id,
       count(*) AS request_count,
       SUM(CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
       SUM(COALESCE(u.prompt_tokens,0)) AS prompt_tokens,
       SUM(COALESCE(u.completion_tokens,0)) AS completion_tokens,
       SUM(COALESCE(u.total_tokens,0)) AS total_tokens,
       SUM(COALESCE(u.cache_read_tokens,0)) AS cache_read_tokens,
       SUM(COALESCE(u.cache_creation_tokens,0)) AS cache_write_tokens,
       SUM(CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END) AS covered_prompt_tokens,
       SUM(COALESCE(u.estimated_cost,0)::numeric) AS estimated_cost,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END) AS sub_requests,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END) AS sub_prompt,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END) AS sub_completion,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END) AS sub_total,
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END) AS sub_covered,
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END) AS sub_equivalent
  FROM deleted_usage u JOIN claimed c ON c.usage_id=u.id
  GROUP BY u.account_id,u.runtime_principal_type,u.runtime_principal_id,u.runtime_session_id
 )
 UPDATE agent_usage_total t SET
 request_count=t.request_count-r.request_count,
 success_count=t.success_count-r.success_count,
 error_count=t.error_count-r.error_count,
 prompt_tokens=t.prompt_tokens-r.prompt_tokens,
 completion_tokens=t.completion_tokens-r.completion_tokens,
 total_tokens=t.total_tokens-r.total_tokens,
 cache_read_tokens=t.cache_read_tokens-r.cache_read_tokens,
 cache_write_tokens=t.cache_write_tokens-r.cache_write_tokens,
 covered_prompt_tokens=t.covered_prompt_tokens-r.covered_prompt_tokens,
 estimated_cost=t.estimated_cost-r.estimated_cost,
 sub_requests=t.sub_requests-r.sub_requests,
 sub_prompt=t.sub_prompt-r.sub_prompt,
 sub_completion=t.sub_completion-r.sub_completion,
 sub_total=t.sub_total-r.sub_total,
 sub_covered=t.sub_covered-r.sub_covered,
 sub_equivalent=t.sub_equivalent-r.sub_equivalent
 FROM removed r WHERE t.account_id=r.account_id
 AND t.principal_type IS NOT DISTINCT FROM r.principal_type
 AND t.principal_id IS NOT DISTINCT FROM r.principal_id
 AND t.session_id IS NOT DISTINCT FROM r.session_id;
 DELETE FROM agent_usage_total t USING deleted_usage d
 WHERE t.request_count=0 AND t.account_id=d.account_id
 AND t.principal_type IS NOT DISTINCT FROM d.runtime_principal_type
 AND t.principal_id IS NOT DISTINCT FROM d.runtime_principal_id
 AND t.session_id IS NOT DISTINCT FROM d.runtime_session_id;
 -- Only scopes whose latest request was removed need a metadata repair.
 WITH affected AS MATERIALIZED (
  SELECT t.id,t.account_id,t.principal_type,t.principal_id,t.session_id
  FROM agent_usage_total t JOIN deleted_usage d
   ON t.account_id=d.account_id AND d.id=t.last_usage_id
 ), latest AS (
  SELECT a.id AS total_id, u.timestamp,u.id,u.model_alias,u.provider_name
  FROM affected a CROSS JOIN LATERAL (
   SELECT u.timestamp,u.id,u.model_alias,u.provider_name FROM api_usage u
   WHERE u.account_id=a.account_id AND u.action_type='model_gateway'
   AND u.runtime_principal_type IS NOT DISTINCT FROM a.principal_type
   AND u.runtime_principal_id IS NOT DISTINCT FROM a.principal_id
   AND u.runtime_session_id IS NOT DISTINCT FROM a.session_id
   ORDER BY u.timestamp DESC,u.id DESC LIMIT 1
  ) u
 )
 UPDATE agent_usage_total t SET last_request_at=l.timestamp,last_usage_id=l.id,
 latest_model_alias=l.model_alias,latest_provider_name=l.provider_name
 FROM latest l WHERE t.id=l.total_id;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION agent_usage_track_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 WITH claimed AS (
  DELETE FROM agent_usage_counted c USING deleted_usage u
  WHERE c.usage_id=u.id RETURNING c.usage_id
 ), removed AS MATERIALIZED (
  SELECT u.account_id AS account_id,
       u.runtime_principal_type AS principal_type,
       u.runtime_principal_id AS principal_id,
       u.runtime_session_id AS session_id,
       count(*) AS request_count,
       SUM(CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
       SUM(COALESCE(u.prompt_tokens,0)) AS prompt_tokens,
       SUM(COALESCE(u.completion_tokens,0)) AS completion_tokens,
       SUM(COALESCE(u.total_tokens,0)) AS total_tokens,
       SUM(COALESCE(u.cache_read_tokens,0)) AS cache_read_tokens,
       SUM(COALESCE(u.cache_creation_tokens,0)) AS cache_write_tokens,
       SUM(CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END) AS covered_prompt_tokens,
       SUM(COALESCE(u.estimated_cost,0)::numeric) AS estimated_cost,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END) AS sub_requests,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END) AS sub_prompt,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END) AS sub_completion,
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END) AS sub_total,
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END) AS sub_covered,
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END) AS sub_equivalent
  FROM deleted_usage u JOIN claimed c ON c.usage_id=u.id
  GROUP BY u.account_id,u.runtime_principal_type,u.runtime_principal_id,u.runtime_session_id
 )
 UPDATE agent_usage_total t SET
 request_count=t.request_count-r.request_count,
 success_count=t.success_count-r.success_count,
 error_count=t.error_count-r.error_count,
 prompt_tokens=t.prompt_tokens-r.prompt_tokens,
 completion_tokens=t.completion_tokens-r.completion_tokens,
 total_tokens=t.total_tokens-r.total_tokens,
 cache_read_tokens=t.cache_read_tokens-r.cache_read_tokens,
 cache_write_tokens=t.cache_write_tokens-r.cache_write_tokens,
 covered_prompt_tokens=t.covered_prompt_tokens-r.covered_prompt_tokens,
 estimated_cost=t.estimated_cost-r.estimated_cost,
 sub_requests=t.sub_requests-r.sub_requests,
 sub_prompt=t.sub_prompt-r.sub_prompt,
 sub_completion=t.sub_completion-r.sub_completion,
 sub_total=t.sub_total-r.sub_total,
 sub_covered=t.sub_covered-r.sub_covered,
 sub_equivalent=t.sub_equivalent-r.sub_equivalent
 FROM removed r WHERE t.account_id=r.account_id
 AND t.principal_type IS NOT DISTINCT FROM r.principal_type
 AND t.principal_id IS NOT DISTINCT FROM r.principal_id
 AND t.session_id IS NOT DISTINCT FROM r.session_id;
 DELETE FROM agent_usage_total t USING deleted_usage d
 WHERE t.request_count=0 AND t.account_id=d.account_id
 AND t.principal_type IS NOT DISTINCT FROM d.runtime_principal_type
 AND t.principal_id IS NOT DISTINCT FROM d.runtime_principal_id
 AND t.session_id IS NOT DISTINCT FROM d.runtime_session_id;
 -- Only scopes whose latest request was removed need a metadata repair.
 WITH affected AS MATERIALIZED (
  SELECT t.id,t.account_id,t.principal_type,t.principal_id,t.session_id
  FROM agent_usage_total t JOIN deleted_usage d
   ON t.account_id=d.account_id AND d.id=t.last_usage_id
 ), latest AS (
  SELECT a.id AS total_id, u.timestamp,u.id,u.model_alias,u.provider_name
  FROM affected a CROSS JOIN LATERAL (
   SELECT u.timestamp,u.id,u.model_alias,u.provider_name FROM api_usage u
   WHERE u.account_id=a.account_id AND u.action_type='model_gateway'
   AND u.runtime_principal_type IS NOT DISTINCT FROM a.principal_type
   AND u.runtime_principal_id IS NOT DISTINCT FROM a.principal_id
   AND u.runtime_session_id IS NOT DISTINCT FROM a.session_id
   ORDER BY u.timestamp DESC,u.id DESC LIMIT 1
  ) u
 )
 UPDATE agent_usage_total t SET last_request_at=l.timestamp,last_usage_id=l.id,
 latest_model_alias=l.model_alias,latest_provider_name=l.provider_name
 FROM latest l WHERE t.id=l.total_id;
 WITH claimed AS (
  INSERT INTO agent_usage_counted(usage_id)
  SELECT u.id FROM inserted_usage u
  WHERE u.action_type='model_gateway' AND u.account_id IS NOT NULL
  AND EXISTS(SELECT 1 FROM account WHERE id=u.account_id)
  ON CONFLICT DO NOTHING RETURNING usage_id
 )
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
  SELECT u.account_id,
       u.runtime_principal_type,
       u.runtime_principal_id,
       u.runtime_session_id,
       count(*),
       SUM(CASE WHEN u.status_code < 400 THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.status_code >= 400 THEN 1 ELSE 0 END),
       SUM(COALESCE(u.prompt_tokens,0)),
       SUM(COALESCE(u.completion_tokens,0)),
       SUM(COALESCE(u.total_tokens,0)),
       SUM(COALESCE(u.cache_read_tokens,0)),
       SUM(COALESCE(u.cache_creation_tokens,0)),
       SUM(CASE WHEN u.cache_read_tokens IS NOT NULL OR u.cache_creation_tokens IS NOT NULL THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       SUM(COALESCE(u.estimated_cost,0)::numeric),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.prompt_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.completion_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' THEN COALESCE(u.total_tokens,0) ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN 1 ELSE 0 END),
       SUM(CASE WHEN u.cost_source = 'subscription' AND jsonb_typeof(u.meta_data->'api_equivalent_cost') = 'number' THEN (u.meta_data->>'api_equivalent_cost')::numeric ELSE 0 END),
       max(u.timestamp),
       (array_agg(u.id ORDER BY u.timestamp DESC,u.id DESC))[1],
       (array_agg(u.model_alias ORDER BY u.timestamp DESC,u.id DESC))[1],
       (array_agg(u.provider_name ORDER BY u.timestamp DESC,u.id DESC))[1]
  FROM inserted_usage u JOIN claimed c ON c.usage_id=u.id
  GROUP BY u.account_id,u.runtime_principal_type,u.runtime_principal_id,u.runtime_session_id
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
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS agent_usage_track ON api_usage;
CREATE OR REPLACE TRIGGER agent_usage_track_delete AFTER DELETE ON api_usage
 REFERENCING OLD TABLE AS deleted_usage FOR EACH STATEMENT
 EXECUTE FUNCTION agent_usage_track_delete();
CREATE OR REPLACE TRIGGER agent_usage_track_update AFTER UPDATE ON api_usage
 REFERENCING OLD TABLE AS deleted_usage NEW TABLE AS inserted_usage FOR EACH STATEMENT
 EXECUTE FUNCTION agent_usage_track_update();
"""


def upgrade() -> None:
    """Install capture and register the history backfill; do not run it.

    The triggers count every new request from this commit on. Historical
    requests are counted after the deploy by ``preloop.models.backfills``,
    which advances the ``agents`` registry row in committed, resumable batches.
    Reads stay on the existing path until that row is ``ready``. A database
    whose backfill already completed keeps ``ready=true``: the registry
    insert is ``ON CONFLICT DO NOTHING`` and nothing here scans ``api_usage``.
    """
    op.execute(sa.text(DDL))


def downgrade() -> None:
    """Remove derived totals; retain all source usage records."""
    op.execute("DROP TRIGGER IF EXISTS agent_usage_track_insert ON api_usage")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_track_insert()")
    op.execute("DROP TRIGGER IF EXISTS agent_usage_track_update ON api_usage")
    op.execute("DROP TRIGGER IF EXISTS agent_usage_track_delete ON api_usage")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_track_update()")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_track_delete()")
    op.execute("DROP TRIGGER IF EXISTS agent_usage_track ON api_usage")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_track()")
    op.execute("DROP FUNCTION IF EXISTS agent_usage_apply(api_usage, integer, boolean)")
    op.drop_table("agent_usage_counted")
    op.drop_table("agent_usage_backfill")
    op.drop_table("agent_usage_total")
