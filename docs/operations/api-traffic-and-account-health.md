# API traffic records and account health

Preloop records `/api/v1` requests, whatever credential they carried
(login session, account API key, run-scoped runtime key or CI identity) and
whatever status they returned. Health probes (`/api/v1/health*`), the public
billing routes (`/api/v1/billing/plans*`, `/api/v1/billing/create-checkout-session*`,
`/api/v1/billing/webhooks*`) and `/api/v1/ai-models/providers/*` are not
recorded. Platform operators can read one account's
failures and anomalies over a time window from these records.

## What is recorded

| Table | Content | Retention |
| --- | --- | --- |
| `api_traffic_rollup` | Request count, total and maximum duration per UTC minute, account, principal kind, method, route template and status class (2xx to 5xx). | `API_TRAFFIC_ROLLUP_RETENTION_DAYS`, default 90 |
| `api_traffic_event` | One row per 4xx or 5xx response, and per list response that came back empty right after a create in its scope. It holds the principal kind and id, route template, status, duration and, where the handler set one, a short reason code. | `API_TRAFFIC_EVENT_RETENTION_DAYS`, default 30, never less than 7 |

Only route templates such as `/api/v1/flows/{flow_id}` are stored, never
the raw path, query values, headers or bodies. Requests that match no route
are stored as `<unmatched>`.

The recording is outside the restricted CI middleware, so CI requests that
the middleware rejects are counted too. Writes happen in memory on the
request path and are flushed every `API_TRAFFIC_FLUSH_SECONDS` (default 10)
in one batch. Memory is bounded. If the database is unavailable, the data
is kept for the next flush within that bound, and anything over the bound is
counted as dropped. `API_USAGE_LOGGING_ENABLED=false` turns the recording
off together with the older per-request usage rows.

### Expected volume

A client that polls one route 27 times a minute produces at most one rollup
row per minute for that series: 1,440 rows a day instead of 38,880 request
rows, or at most 43,200 rows in 30 days instead of about a million. Each
additional combination of principal kind, method, route and status class
adds at most one row per minute while it is active. Errors add one row each.

## Account health

`GET /api/v1/operator/accounts/{account_id}/health?since=&until=` and
`preloop accounts health <account-id> [--since 24h] [--json]` return, for
one account and a window of at most 31 days (default 24 hours):

- failed flow executions per failure category, with the first execution of
  each;
- successful executions with zero tool calls, and with zero model requests
  (a model request is any gateway call linked to the execution, failed or
  not; older runs may undercount native tool activity);
- API routes that answered 4xx or 5xx at least twice, and the individual
  error responses grouped by status, principal kind and reason code;
- list responses that came back empty right after a create in the same
  scope;
- gateway upstream errors (`network` and `upstream_*` classes) per provider
  and error class. Client cancels, budget and hosted-credit gating are not
  counted as upstream errors.

Only categories, counts, ids, route templates and first-party codes are
returned, never prompts, outputs, raw error text or provider error bodies.
Access is limited to installation superusers. Keys bound to a flow
execution, runtime session or managed agent are refused even when their
owner is a superuser, and restricted CI keys never reach the route.

## Run-scoped key maintenance

Flow executions and runtime sessions mint their own API keys. The normal
end-of-run paths revoke them, but a run that ended some other way (a crash
or a recovery sweep, for example) can leave its key active after it
expires. A maintenance pass deactivates active keys that are bound in
their `context_data` to a flow execution or runtime session that has
ended, or that have expired. It never touches keys that a person created,
which carry neither binding, or durable managed-agent credentials. Key
names are never used as evidence. A key keeps working for the post-run
flush: the pass skips executions that ended less than five minutes ago and
runtime sessions that ended less than an hour ago.

The pass runs hourly as the `deactivate_ended_run_keys` task. To run it by
hand:

```bash
python -m preloop.services.run_key_maintenance                 # dry run: counts only
python -m preloop.services.run_key_maintenance --apply         # deactivate
python -m preloop.services.run_key_maintenance --account-id <uuid> --batch-size 500 --max-batches 20
```

Each applied batch writes one `run_keys_deactivated` audit row per account
with the key ids and the counts per reason (`expired`, `execution_ended`,
`session_ended`). The output prints counts only.

## Unverified sign-up cleanup

Registration creates an account before its address is verified. The same
hourly `deactivate_ended_run_keys` task then looks for self-registered
accounts that were never used and, when enabled, deletes them. An account
qualifies only when all of these hold:

- it was created more than `UNVERIFIED_SIGNUP_MAX_AGE_DAYS` days ago
  (default `7`, minimum `1`);
- its single member's address was never verified and that member never
  signed in (failed sign-ins do not count as use);
- it has no billing customer, no parent or child accounts, and is not any of
  the accounts listed in `OPERATOR_ACCOUNT_IDS`;
- nothing else in the database points at the account, its member or the
  member's identity: no API key, agent, tracker, flow, session, passkey,
  single sign-on link or any other row besides the defaults registration
  writes. Foreign keys are read from the live database, so tables added by
  plugins count as well.

| Variable | Default | Meaning |
|---|---|---|
| `UNVERIFIED_SIGNUP_CLEANUP` | `dry-run` | `dry-run` logs the counts and changes nothing; `apply` deletes; `off` skips the pass |
| `UNVERIFIED_SIGNUP_MAX_AGE_DAYS` | `7` | minimum account age in days |

`apply` needs an account to hold its audit trail: each batch writes one
`unverified_signups_deleted` audit row, with the deleted account ids, in the
first account in `OPERATOR_ACCOUNT_IDS`. Without one the pass runs as a dry
run and logs why. Before deleting a batch, the pass checks each account again with its rows
locked, so one verified or signed in to since the scan is kept. A pass reads
at most two batches of 100 accounts. To run it
by hand:

```bash
python -m preloop.services.unverified_signup_cleanup                 # dry run
python -m preloop.services.unverified_signup_cleanup --apply
python -m preloop.services.unverified_signup_cleanup --max-age-days 14 --batch-size 100 --max-batches 5
```

Self-hosted installs keep the default dry run, which deletes nothing.

## Flow delete refusals

A refused `DELETE /api/v1/flows/{id}` sends an `X-Preloop-Delete-Blocker`
header (`built_in_preset` on the 403 for a built-in preset; `active_executions`,
`references` or `reference_race` on a 409) and writes a
`flow_delete_refused` audit row whose `details.reason_code` holds the same
code, with ids and counts but no names. The traffic event for the 409 holds
the code too.
