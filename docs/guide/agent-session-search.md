# search_sessions: the corpus as a tool

`search_sessions` is the built-in tool an agent uses to look up what past
sessions did before repeating the work. It is the same ranked search the
console runs, asked by the agent instead of by a human: did this migration
already run, what did the last attempt conclude, what was this user already
asked last week.

It exists because a corpus only a human can query is half a corpus. The
expensive failures are the ones an agent walks into with no memory: the
migration applied twice, the answer derived again from scratch, the question
asked a second time on a Friday afternoon. One search is cheaper than any of
them.

## Scope

The default scope is the calling agent's own sessions, and there is nothing to
pass to get it. An agent reading its own history is obviously safe.

`scope: "account"` searches every session of the account. It is refused unless
an operator has granted it:

```json
{
  "refused": true,
  "reason": "account_scope_not_granted",
  "detail": "Searching every session of the account needs the 'session_search.account_scope' grant, which an operator has not given this agent. Your own sessions are searchable now: repeat the call with scope 'own', and ask an operator for the grant if the answer has to come from another agent's work.",
  "scope": "account",
  "results": []
}
```

The refusal is deliberate. Narrowing the search to the agent's own sessions
and answering anyway would be worse than saying no: the agent would read an
empty result as "this was never done" when it means "not by you". The grant
is read per subject from the account's governance store (the api key first,
then the managed agent, then the account default), so widening a scope is a
configuration change rather than a release. Who may hand the grant out, and
through which surface, is the open decision on issue #624; until it is made,
nothing writes the grant and every account wide call is refused.

The identity the scope is built from comes from the authenticated session, not
from the arguments. An agent chooses what to search, never whose sessions.

## Calling it

The tool is off by default: a flow selects it in its tool allow-list, or an
account enables it on the Tools page. It is a normal built-in from there on,
so an approval workflow or an access rule that denies `search_sessions` stops
the call like any other tool.

```json
{
  "name": "search_sessions",
  "arguments": {
    "query": "\"vacuum full\" or reindex -dry-run",
    "start_date": "2026-09-01T00:00:00Z",
    "limit": 3
  }
}
```

`query` is parsed the way a search box is: a quoted phrase stays a phrase,
`or` alternates, a leading `-` excludes. `start_date` and `end_date` are ISO
8601 and must carry a timezone offset; a naive one is refused, because the
same call must mean the same instant wherever it runs. `mode` accepts
`keyword`, `semantic` and `hybrid` today; anything but `keyword` is answered
with keyword results and a degraded marker rather than an error.

## What comes back

```json
{
  "query": "\"vacuum full\" or reindex -dry-run",
  "scope": "own",
  "mode": "keyword",
  "effective_mode": "keyword",
  "degraded": {
    "keyword": true,
    "semantic": false,
    "reasons": ["semantic_not_enabled"],
    "detail": "Semantic ranking is not enabled on this deployment; these are keyword results ranked by relevance."
  },
  "indexed_through": "2026-09-16T07:41:12+00:00",
  "total": 7,
  "returned": 3,
  "truncated": true,
  "results_omitted": 4,
  "results": [
    {
      "runtime_session_id": "4f0f2c6e-5a2b-4c1e-9a0f-2b8d7c4e1a33",
      "session_reference": "nightly-maintenance-2026-09-12",
      "occurred_at": "2026-09-12T02:14:51+00:00",
      "snippet": "ran <mark>VACUUM</mark> <mark>FULL</mark> on the reporting tables, 41s, no lock waits ... follow up ticket opened for the index bloat",
      "match_reason": "matched in 3 turns (transcript_message, tool_call)"
    }
  ]
}
```

Every part of that shape earns its place:

- **`results` is compact.** One snippet per session, trimmed, with the
  matching words in `<mark>`. It is enough to decide whether to open the
  session, which is the decision the agent is making.
- **The whole answer is capped.** A search that floods the context spends
  exactly what it was supposed to save. When the cap bites, `truncated` is
  true and `results_omitted` counts what was dropped; `total` still says how
  many sessions matched, so the agent narrows the query or the time range
  instead of paging blindly.
- **`degraded` is passed through unchanged** from the search endpoint. With
  semantic ranking off, a miss is a keyword miss. An agent that knows this
  rephrases; an agent that does not concludes the work was never done.
- **`indexed_through`** is the newest content the corpus holds for the
  account. An empty answer newer than that marker means not indexed yet, not
  absent.
- **No match is an empty `results` list**, not an error. "Nothing found" is
  the answer that lets the agent get on with the work.

## Limits

One call returns at most 20 sessions and 5 by default, and the response size
cap can return fewer. The corpus itself is what is searchable: content the
deployment never captured, or that a redaction withheld afterwards, is not
returned, and a result whose snippet was withheld says so in its
`match_reason`.
