# Auditing session content search

Opening one session somebody linked reads one session. A content search reads
every captured prompt, response and tool call the account holds, ranks them,
and hands back the fragments that matched. It is the broadest read the product
offers, so every search writes one audit row.

The row is what makes the feature answerable. When somebody asks whether
anyone grepped the transcripts, "no" is only a useful answer if there is a
place a "yes" would have shown up.

## What is recorded

One row per call, written through the same audit path every other
security-relevant action uses, so it is queryable with the rows already there:

| Field | Value |
| --- | --- |
| `action` | `query` |
| `resource_type` | `session_search` |
| `resource_id` | the query hash, so repeats of one search group together |
| `user_id` | the person who searched, or null for an agent |
| `status` | `success`, `denied` or `failure` |

`details` carries the metadata of the read and nothing of its content:

```json
{
  "actor_type": "user",
  "source": "api",
  "actor_user_id": "0f2c6e4f-5a2b-4c1e-9a0f-2b8d7c4e1a33",
  "mode": "hybrid",
  "effective_mode": "keyword",
  "filters": {"source_kind": "transcript_message", "start_date": "2026-09-01T00:00:00+00:00"},
  "result_count": 3,
  "total_matched": 11,
  "limit": 20,
  "offset": 0,
  "query_hash": "sha256:6b7f...",
  "query_chars": 18,
  "query_text_stored": false
}
```

Snippet text is never recorded, under any setting. The row says that a search
happened and what it was allowed to see, not what it saw.

## Why a hash rather than the query

A search string is frequently the secret the searcher is hunting for, typed
verbatim. An audit trail that keeps every query is a second copy of exactly
the thing the first copy was supposed to protect, and it is a copy that
outlives the session it came from.

The stored digest is `sha256` over the normalised, lowercased query, so the
same search typed twice hashes the same way and two different searches do not.
That answers the questions a review asks, "was this searched for again", "who
else searched for it", without holding the string. It is deliberately
unsalted: a salt would also break recognising the same search across rows and
exports.

An account that needs the text sets `session_search_audit_store_query_text` to
`true` in its account metadata. It is off until it is set, and the name says
what it turns on. With it on the row also carries `query_text`; with it off,
no field in the row contains the query.

## Who searched

`actor_type` and `source` separate an agent's search from a person's:

- A console or API search is `actor_type: "user"` with `source: "api"`, and
  the user id is on the row itself.
- A search through the `search_sessions` tool is `actor_type:
  "managed_agent"` with `source: "mcp"`. `user_id` stays null and the agent
  names itself in the details (`actor_managed_agent_id`,
  `actor_runtime_principal_id`, `actor_api_key_id`): the audit table has no
  agent column, and borrowing a person's identity for an agent's act would be
  the wrong record.

## Refusals and failures

A refused search is still a search somebody tried to run, and it is usually
the more interesting row. An agent call refused for lack of the account wide
grant writes a `denied` row naming the rule that declined it; a search that
breaks writes a `failure` row naming the exception type.

## Auditing never breaks a search

If the row cannot be written, the failure is logged and swallowed and the
search answers exactly as it would have. This is the discipline the indexing
path already keeps. A search that fails because its audit row could not be
written teaches operators to turn auditing off, which is the opposite of what
the row is for; a warning in the log with no row in the table is the honest
signal that something is wrong with the audit path itself.

Retention, export, signing and a console view of these rows follow the audit
log's own rules and are not specific to search.
