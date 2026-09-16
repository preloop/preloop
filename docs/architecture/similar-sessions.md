# Sessions Similar To This One

`GET /api/v1/runtime-sessions/{id}/similar` answers one question: which other
sessions of this account went the same way as the one on screen. The issue that
asked for it (#674) left four decisions open and named none of them as
recommended. This page records what was chosen, why, and what each choice is
biased towards, so a later change is made against a stated position rather than
against silence.

The route reads vectors the indexing worker already wrote. It embeds nothing,
calls no provider and records no spend, so an account sitting at its daily
embedding cap still gets this list. Every way the comparison can come up short
is named in a `degraded` block and never raised as an error, the same contract
the search in [data-model](data-model.md#session-search-corpus) uses.

## Decision 1: sampled chunks, not a session centroid

A session is compared by a handful of its own chunks, sampled at an even stride
across it (`SIMILAR_PROBE_CHUNKS = 8`), not by one averaged vector.

Averaging every chunk of a session into a centroid is cheaper and it is what a
"session embedding" would usually mean. It also destroys the thing that makes
two sessions worth comparing: a long session is mostly setup, retries and
noise, and the mean of all of that is a vector near the middle of the account's
whole corpus. Two sessions that both spent twenty minutes fighting the same
migration look similar because of those twenty minutes, not because of their
averages.

The bias this carries: a session is judged by the passages that happened to be
sampled. The response is explicit about it rather than hiding it. `probe_chunks`
against `embedded_chunks` says how much of the session was actually compared,
and a session larger than the probe budget carries the reason code
`similar_session_sampled`.

## Decision 2: computed per request, nothing precomputed

The comparison runs when asked. There is no neighbour table, no nightly job and
no cache.

It is bounded work: one `UNION ALL` of per-probe HNSW lookups, each limited to
`SIMILAR_NEIGHBOURS_PER_PROBE = 25` rows, on an index the corpus already has.
A precomputed neighbour table would be a second store of derived facts that go
stale the moment a session gains a turn, and staleness in this feature is worse
than latency: an operator asks for neighbours while reading a session that is
often still running.

The bias: cost scales with how often the panel is opened. The console keeps the
panel collapsed and asks once per session, so a session nobody asked about
costs nothing.

## Decision 3: publish the number, show the word

The API returns `similarity` (cosine, 0 to 1) and a `band` of `close`,
`related` or `loose` (`SIMILARITY_BAND_CLOSE = 0.70`,
`SIMILARITY_BAND_RELATED = 0.50`). The console renders the band and keeps the
number in a tooltip.

A cosine similarity of 0.63 reads like a measurement an operator can trust to
two decimal places. It is not one: it is a distance in whichever vector space
the account's embedding model happens to define, and the same pair of sessions
scores differently under a different model. A word survives that change; a
number invites an operator to build a habit on a threshold nobody validated.
The number stays in the payload because hiding it from an API would be the
other kind of dishonesty.

The bias: the band cut points are unvalidated, chosen to make the orderings in
`backend/tests/services/test_session_similarity.py` hold. Treat changing them
as a product change, not a refactor.

## Decision 4: no default time window

By default the whole corpus is compared. `window_days` narrows it when a caller
asks, and a window that was applied is named in the degraded block with
`similar_window_applied`.

A default window would quietly hide the result this feature exists to find. The
session worth finding is often the old one nobody remembers, from before the
last release, which is exactly what a thirty day default would drop. Recency is
a filter an operator can apply, not a prior the platform should hold for them.

The bias: on an account with a long history the comparison reads over more of
the index. The floor (`MIN_SIMILAR_SIMILARITY = 0.35`) and the per-probe limit
bound that.

## Ranking

Chunk hits are grouped into sessions. A session's score is its best matching
passage plus a small credit for matching in more than one place:

```
score = best_similarity + SIMILAR_BREADTH_WEIGHT * log1p(matched_chunks - 1)
```

Best passage dominates on purpose: one session that solved the same problem is
worth more than one that mentions the topic repeatedly in passing. The breadth
term only breaks ties between sessions whose best passages are equally close.
`SIMILAR_BREADTH_WEIGHT = 0.02` is small enough that it cannot lift a weaker
match over a stronger one. Ties are broken on session id, so paging or
reloading never reorders the list under the reader.

## What the comparison will not do

*   **Cross model.** A session is only compared with chunks carrying the same
    `embedding_model` identity, because a distance between two models' spaces
    is not a similarity. When the account's configured model is not the one
    this session was embedded with, the session is compared in its own space
    and the answer carries `semantic_model_mismatch`.
*   **Cross account.** Every read is account-scoped, and a session id belonging
    to another account answers 404 rather than an empty list, so a probe cannot
    tell an existing session apart from one that never existed.
*   **Withheld content.** Only chunks in the `clear` redaction state are probes
    or matches, and `include_match_text=false` returns the ranking with no
    captured content read at all.

## Console

The panel sits under the replay on the sessions page, collapsed, behind the
`similarSessions` observer feature. Opening it asks once. Each entry links to
the other session (`/console/sessions?sessionId=...&replay=conversation`) and
renders the matching passage inline.

Entries open the other session, not the matching turn inside it. Deep linking
to a turn needs an anchor the corpus does not carry today: replay anchors are
`runtime_session_activity` ids, while a gateway chunk records the `api_usage`
row it came from. The matching passage is shown in the panel instead, so the
operator sees what matched without a link that lands in the wrong place.
