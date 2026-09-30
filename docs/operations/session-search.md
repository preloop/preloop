# Session search

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Searching session content is two mechanisms with one entry point. Keyword
search reads a corpus of text chunks, `session_search_document`, and is the
half every deployment gets. Semantic search reads vectors over the same
chunks, and an account has to opt in before any exist. The endpoint is
`POST /api/v1/runtime-sessions/search`; the console box at the top of the
sessions view calls it in `keyword` mode.

## What is in the corpus, and when it got there

Chunks are written on the request path, as the content is stored. A gateway
interaction becomes `gateway_interaction` chunks, a tool call becomes a
`tool_call` chunk, imported transcripts become `transcript_message`, an
operator note `operator_note`, a flow log `flow_log`, and a session's title
and summary a `session_summary` chunk. Text is split at about 1200
characters with 200 of overlap, capped at 64 chunks per source, and the
`search_vector` column is generated as `to_tsvector('simple', content)`.
`simple` means no stemming: `patch` does not match `patched`, and
`websearch_to_tsquery` syntax (quoted phrases, `or`, `-`) is what a query is
parsed as.

Two switches decide whether anything is written at all:

| Variable | Default | Role |
| --- | --- | --- |
| `SESSION_SEARCH_INDEX_ENABLED` | true | Whether any chunk is written as content is stored |
| `MODEL_GATEWAY_CAPTURE_CONTENT` | true | Whether prompt and response bodies are kept at all. Off means gateway chunks carry metadata only, and a search for a word inside a prompt cannot match. A deployment that turns it off for privacy gets title and metadata search and nothing more |

The consequence operators hit first: **indexing on write covers new sessions
only**. On the day search is deployed, nothing older than the deploy is in
the corpus. Sessions from before it can still appear in results, because the
sessions list generates missing AI titles lazily and a title write produces a
`session_summary` chunk. That is why a fresh deployment looks like "search
only works on titles, not on content": for pre-deploy sessions the title
chunk is the only thing indexed. Every search answer now publishes both ends
of the window it covered, `indexed_from` and `indexed_through`, plus
`backfill_complete` and `backfill_state`, and the console shows a notice
naming the date the corpus reaches back to.

## The history backfill

`SESSION_SEARCH_BACKFILL_ENABLED` (default **false**) starts a sweeper on the
API role that walks each account's runtime sessions newest first and derives
chunks from sources already on disk: gateway interaction documents joined
through usage rows, and tool call activity rows. It captures nothing new and
changes no chunk shape; the same writers that run on the request path run
against older rows. Each account carries a watermark, so a pass that stops on
a budget resumes rather than restarts.

| Variable | Default | Role |
| --- | --- | --- |
| `SESSION_SEARCH_BACKFILL_ENABLED` | false | Run the sweeper at all |
| `SESSION_SEARCH_BACKFILL_INTERVAL_SECONDS` | 900 | Seconds between passes |
| `SESSION_SEARCH_BACKFILL_MAX_ROWS_PER_PASS` | 2000 | Corpus rows one pass may write in total |
| `SESSION_SEARCH_BACKFILL_MAX_ROWS_PER_ACCOUNT` | 500 | Rows one pass may write for a single account, so one large account cannot take the whole budget |
| `SESSION_SEARCH_BACKFILL_MAX_SECONDS` | 120 | Wall clock budget for one pass |
| `SESSION_SEARCH_BACKFILL_MAX_AGE_DAYS` | 183 | How far back the walk goes. 0 means no age bound. Matches the retention floor |

Estimating how long a backfill takes: count the sessions with no chunks and
multiply by the chunks a session of yours produces (read it off a recently
indexed account: total chunks divided by sessions indexed). Divide by the
rows a pass writes, and multiply by the interval. With the shipped bounds a
pass writes at most 2000 rows every 900 seconds, so 8000 rows an hour across
the deployment, and at most 500 rows per account per pass, so 2000 rows an
hour for any one account. A backlog concentrated in a single account is
therefore bounded by the per-account number, not the per-pass one.

Progress is visible per account without reading the sweeper's logs: the
search response reports `backfill_state` as `not_started`, `in_progress` or
`complete`, and `indexed_from` moves backwards as the walk proceeds. The pass
itself logs a summary line (`Session search backfill pass: ...`) whenever it
writes rows or an account fails.

Turning the sweeper off leaves everything it wrote in place. Turning it back
on resumes from the watermark.

## Who may search

The core gates the endpoint with `view_runtime_sessions`, the permission
every read role already has. Enterprise deployments require a second
permission, `search_session_content`, because reading a session you were
pointed at and asking every transcript in the account a question are
different acts. The enterprise role table is in `plugins/rbac/README.md` of
the enterprise repository; as seeded, owner, admin and analyst carry it, and
editor, executor, tracker manager, viewer and every custom role do not. A
caller without it is refused before the query runs, so no `ts_headline`
snippet is ever generated for them.

To give a person search, assign a role that carries the permission (analyst
is the seeded non-administrative holder), or add `search_session_content` to
a role in `scripts/init_system_roles.py` and re-run the seed. Either change
takes effect on the next request.

Agents are separate. The `search_sessions` tool searches the calling agent's
own sessions by default; a call with `scope: "account"` is refused by name
unless the account governance store carries the `session_search.account_scope`
grant for that caller. The core ships the read half of that grant only; the
enterprise repository ships the write half at
`PUT /api/v1/session-search/grants`.

Every search, answered or refused, is audited with the actor and the source
(`console` or `mcp`), so the trail separates a person's search from an
agent's.

## Turning on semantic search for an account

Semantic search needs vectors, and vectors cost provider spend, so an account
opts in explicitly. In the console, open **Sessions**, then **Semantic search
settings** above the list (a search that came back with
`semantic_not_enabled` also offers **Turn on semantic search** in its
notice). The card turns embedding on, names the model and endpoint, picks
the scope, sets the daily cap, and shows how much of the corpus is embedded
and why the last run stopped short, if it did. A saved change re-runs the
search on screen.

The same thing over the API:

```bash
# Read the current setting, with corpus progress
curl "$PRELOOP_URL/api/v1/runtime-sessions/settings/embedding" \
  -H "Authorization: Bearer $TOKEN"

# Opt in, embedding titles and summaries only
curl -X PUT "$PRELOOP_URL/api/v1/runtime-sessions/settings/embedding" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "scope": "summaries_only",
       "provider": "openai_compatible",
       "model_identifier": "text-embedding-3-small",
       "base_url": "https://embeddings.example.com/v1"}'

# Turn it off; the provider details stay for a later {"enabled": true}
curl -X PUT "$PRELOOP_URL/api/v1/runtime-sessions/settings/embedding" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Every field of the `PUT` is optional and a field left out is left alone, as
is a field sent as `null`, except `daily_cap_usd`.
Enabling needs a model, either in the body or already on the setting from an
earlier opt in; a refusal (no model, a non-https or loopback endpoint) is a
422 whose `detail.code` names it. Provider details are only accepted when the
save leaves embedding on. `daily_cap_usd: null` clears the account cap back
to the deployment default.

Reading takes `view_runtime_sessions`; writing takes `manage_budgets`,
because widening the scope is a spending decision. `scope` is
`summaries_only` (the default, about one chunk per session) or `full` (every
chunk, transcripts included, roughly forty times the storage and spend). An
unknown scope is a 422. The deployment kill switch is
`SESSION_EMBEDDING_ENABLED`; with it off no account embeds, and keyword
indexing is unaffected. `SESSION_EMBEDDING_DAILY_CAP_USD` (default 2.0 USD)
is the per-account daily ceiling on purpose-tagged `session_embedding`
spend, and an account may set its own on the setting row. Reaching the cap is
a degraded state, not an error: chunks stay pending for the next day's run.
[`session-embedding.md`](session-embedding.md) covers the worker, the scope
arithmetic and the shared API key allow-list.

## Degraded reasons the answer can carry

A search never errors because half of it could not run. It returns what it
could and names what it could not, in `degraded.reasons`.

| Reason | What happened | What an operator does |
| --- | --- | --- |
| `semantic_not_enabled` | The account has not opted in | Sessions, Semantic search settings, or the `PUT` above |
| `semantic_disabled_by_deployment` | `SESSION_EMBEDDING_ENABLED` is off | Deployment decision |
| `semantic_daily_cap_reached` | Today's embedding spend hit the cap | Raise the cap or wait for the next day |
| `semantic_provider_error` | The embeddings provider could not answer | Check the provider and the worker logs |
| `semantic_provider_misconfigured` | The setting names no usable provider or model | Fix the setting |
| `semantic_model_mismatch` | The corpus holds vectors from a different model than the one that embedded the query | Re-embed under one model, or keep the query model aligned |
| `semantic_no_vectors` | The account has no vectors yet | Wait for the worker |
| `semantic_backfill_incomplete` | Some chunks are still waiting for a vector, so the semantic half searched less than the keyword half | Wait for the worker |
| `fusion_candidates_truncated` | A candidate list filled its documented depth, so a fused page cannot see past it | Narrow the query or the date range |

Keyword results are never degraded by any of these: the keyword half of a
keyword search reads the whole corpus.

## Reading an empty answer

Three different things look identical to a user and are distinguishable from
one response:

1. `backfill_state` is `not_started` or `in_progress` and `indexed_from` is
   later than the window searched: the sessions that could have matched are
   not indexed yet. This is a coverage gap, not an absence.
2. `MODEL_GATEWAY_CAPTURE_CONTENT` is off for the period searched: the
   bodies were never stored, so no word inside them can match. Only metadata
   and titles can.
3. `backfill_state` is `complete`, the window is covered, and the query still
   returns nothing: no session did that. Check the query for stemming
   (`simple` does none) before concluding it.
