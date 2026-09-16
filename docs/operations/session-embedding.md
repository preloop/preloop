# Session embedding worker

Keyword indexing of session content is separate from vectors. Turning
embedding off does not stop the corpus from taking writes.

## Kill switch vs keyword indexing

`SESSION_EMBEDDING_ENABLED` (default true) is the deployment kill switch for
vectors only. `SESSION_SEARCH_INDEX_ENABLED` gates keyword chunks. Accounts
must still opt in through `session_embedding_setting`; the shipped default
embeds nothing.

## What gets embedded

`session_embedding_setting.scope` decides how much of a session becomes
vectors. It is `summaries_only` by default, for new accounts and for every
row that existed before the column did.

| Scope | Chunks embedded | Rough cost for 10k sessions |
| --- | --- | --- |
| `summaries_only` (default) | the session's own title and summary chunk, about one per session | about 60 MB of vectors |
| `full` | every chunk, transcripts included, about 40 per session | about 2.4 GB of vectors, plus the HNSW index |

The arithmetic: a 1536 wide vector of 4 byte floats is about 6 KB, so a
session of about 40 chunks is about 240 KB of vectors, and 10k of those
sessions are roughly 2.4 GB before an index is built. A title and summary
are one short chunk and carry the meaning semantic search is good at, which
is most of the value at about a fortieth of the storage and the provider
spend.

Changing the scope changes nothing that already exists. Narrowing to
`summaries_only` keeps the vectors an account produced under `full` and
stops new transcript chunks being claimed from the next worker pass;
widening to `full` hands the untouched backlog back to the worker, still
under the daily cap. Keyword search reads the whole corpus in both cases,
so nothing becomes unfindable, and a semantic hit from a summaries-only
account names its source in the result so it reads as a summary match.

The setting is read and written at `GET` and `PUT
/api/v1/runtime-sessions/settings/embedding`. Reading it takes
`view_runtime_sessions`; changing the scope takes `manage_budgets`, because
widening it is a spending decision. An unknown scope is a 422.

## Daily cap

`SESSION_EMBEDDING_DAILY_CAP_USD` (default 2.0) is the per-account money
ceiling for purpose-tagged `session_embedding` usage. An account may set its
own cap on the setting row. Reaching the cap is a degraded state, not an
error: chunks stay pending for the next day's run. An OpenAI-compatible model
missing from the price catalogue is refused before the provider call
(`unpriced_model`) so an unmetered name cannot bypass the cap.

## Shared API key

`SESSION_EMBEDDING_API_KEY` is a deployment credential. It is attached as a
Bearer token only when the account's `base_url` is listed in
`SESSION_EMBEDDING_API_KEY_BASE_URLS` (comma-separated exact https URLs,
trailing slash ignored). An empty allow-list, the shipped default, means the
key is never sent. Store the key in the same secret handling as other
provider credentials. `enable()` (and the worker, again at request time)
refuses a non-https URL, a private/loopback/link-local IP literal, and a
hostname that resolves to loopback, link-local, multicast, unspecified,
or reserved addresses, so `169.254.169.254.nip.io` cannot reach instance
metadata. A hostname that resolves to RFC1918 or unique-local is accepted:
that is the self-hosted OpenAI-compatible path. DNS can still change
between the resolve check and the HTTP connect. Restrict the embedding
worker's egress (deny link-local and metadata ranges; allow only the
operator endpoints you intend) rather than treating the hostname check as
a firewall.

Transcript import writes search chunks with `commit=False` and then
calls `request_embedding` after the host transaction commits. Other
`commit=False` writers still wait for a later committing write or a
sweeper.

## Other knobs

| Variable | Default | Role |
| --- | --- | --- |
| `SESSION_EMBEDDING_BATCH_SIZE` | 32 | Chunks per provider call and usage row |
| `SESSION_EMBEDDING_QUEUE_MAX_PENDING` | 128 | Accounts waiting before a submit is dropped |
| `SESSION_EMBEDDING_QUEUE_WORKER_ENABLED` | true | Background thread; `TESTING=true` disables it |
| `SESSION_EMBEDDING_MAX_ATTEMPTS` | 3 | Retries before a chunk is retired as failed |
| `SESSION_EMBEDDING_TIMEOUT_SECONDS` | 30 | One embeddings HTTP call |
| `SESSION_SEARCH_QUERY_EMBEDDING_TTL_SECONDS` | 300 | How long a search query's vector stays in the process cache. Zero disables the cache. A cached vector can serve paging for up to the TTL after the daily cap is reached. Consent is checked before the cache is read, so opt-out cannot start a semantic search from a leftover entry. |
| `SESSION_SEARCH_QUERY_EMBEDDING_CACHE_SIZE` | 256 | Query vectors one process may cache. The entry closest to expiry is evicted when the cache is full. |

Semantic search also raises `hnsw.ef_search` to at least 200 (`VECTOR_CANDIDATE_CHUNKS`) for each ANN statement. The HNSW index cannot carry account / model / redaction filters, so that depth is approximate under selective filtering rather than "the closest N".

Helm documents the same names next to the gateway search-index queue comments
in `helm/preloop/values.yaml`.
