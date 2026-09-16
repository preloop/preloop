# Saved session searches

A saved session search is a named question about what the agents did: the
query text, the ranking mode, the filters and the snippet preferences, stored
under a name so the same question can be asked again in one click. It is not a
bookmark of results. Nothing about a past answer is kept, which is what makes
a re-run next month honest: it searches the corpus as it is then, including
whatever has since been redacted, retained out or deleted.

The searches worth saving are the ones people repeat: "what did the agents do
about billing last month", "every session that touched the migration graph",
"runs where an agent hit a provider error". Retyping the query and rebuilding
four filters each time is the difference between a search box people use and
one they try once.

This document is the spec asked for in issue #673 and the description of what
shipped with it.

## What is stored

| Stored | Why |
| --- | --- |
| `name` | The label in the saved list. Unique per author inside an account. |
| `query` | Normalised exactly as the search endpoint normalises it. |
| `mode` | `keyword`, `semantic` or `hybrid`, saved as asked. |
| `filters` | The validated filter object, serialised. |
| `max_snippets_per_session`, `include_snippet_text` | The snippet preferences a run uses. |
| `visibility`, `shared_at` | Private, or shared with the account, and when. |
| `filters_version` | The filter schema the payload was validated against. |
| `ranking_identity` | The ranking constants in force when it was saved or last edited. |
| `last_run_at`, `run_count` | Whether anybody actually re-runs it. |

Not stored: results, result counts, session ids, or anything else that would
turn a saved question into a stale report.

Paging is deliberately absent. `limit` and `offset` are properties of looking
at an answer, not of the question, so they are passed per run.

## The routes

All of them take the `view_runtime_sessions` permission, the same one the
search endpoint takes: saving a query grants no access, because a saved search
can only ever return what the person running it could have found by typing the
query themselves.

| Route | What it does |
| --- | --- |
| `POST /api/v1/runtime-sessions/search/saved` | Save a search. Returns 409 when the author already used that name. |
| `GET /api/v1/runtime-sessions/search/saved` | The caller's saved searches plus the ones shared with the account, most recently run first. |
| `GET /api/v1/runtime-sessions/search/saved/{id}` | One saved search the caller may see. |
| `PATCH /api/v1/runtime-sessions/search/saved/{id}` | Rename, change the question, share or unshare. Author only. |
| `DELETE /api/v1/runtime-sessions/search/saved/{id}` | Delete the question. Sessions are untouched. |
| `POST /api/v1/runtime-sessions/search/saved/{id}/run` | Re-run it. Body carries paging only, and is optional. |

They sit under the search path rather than beside it
(`/runtime-sessions/saved-searches`) because the account router already owns
`/runtime-sessions/{runtime_session_id}`: a two segment sibling would be
matched as a session id before it reached this module.

## Visibility

A saved search is private to its author until the author sets `visibility` to
`account`, which is a separate edit and is reversible. Anyone in the account
who may read sessions can then see it and run it; only the author can rename
it, change the question, share it again or delete it. A private search
belonging to somebody else answers 404, because its existence and the name its
author chose are not other people's business; a shared one the caller did not
write answers 403 on an edit, because pretending it does not exist would be a
worse answer than saying it is not theirs to change.

Finer grained sharing (per team, per role, an explicit recipient list) belongs
to the enterprise layer and is tracked there.

A saved search is deleted with the user who wrote it, shared or not. An
account that wants to keep somebody's question can save its own copy while it
is still shared.

## What a re-run says that a search does not

A run returns the search endpoint's answer under `search`, unchanged and
including its degraded block, plus two things about the saved question
itself.

**`unresolved_filters`** names every saved filter that no longer points at
anything in the account: a deleted flow, a rotated api key, a corpus source
kind this build stopped writing, or a key the filter schema no longer defines.
Each entry says whether the run still applied it.

* A filter naming a row that is gone is still applied, and the run returns
  nothing. Corpus rows keep the flow id and api key id they were written with,
  so the filter still means something; widening the search instead would
  return sessions the caller never asked for and give them no way to tell.
* A key the current schema cannot accept is the one case that is left out of
  the run, because there is no way to apply it. It is reported by name rather
  than dropped in silence.

**`ranking_changed`** says the ranking constants have moved since the search
was saved, so this ordering is not the ordering it was saved under. The
constants are still tunable and unvalidated, so nothing is pinned per saved
search; freezing a guess would be worse than reporting that the guess changed.

A saved mode that cannot run today is not an error. A saved `hybrid` search on
an account that has not opted in to embedding comes back as keyword results
with `semantic_not_enabled` in the degraded block, exactly as the search
endpoint would answer the same body.

## The open decisions, and what they were decided as

Issue #673 left four decisions to the spec.

1. **Visibility.** Private by default, with an explicit share step to the
   whole account, and author-only editing. Both halves, because a saved search
   that could only be private makes the repeated team questions unshareable,
   and one that shared by default publishes the author's query text without
   being asked. The multi-user refinements belong to the enterprise layer.
2. **Notifications.** Out. A saved search is a manual re-run. A digest of new
   matches is a subscription feature with its own cost, retention and delivery
   questions, and it is a separate issue.
3. **Filters.** Stored as the validated filter object, not as an opaque
   payload, so an unrunnable search cannot be saved. The stored payload carries
   the schema version it was validated against, and a run reports key by key
   what the current schema can no longer accept.
4. **Ranking.** Not pinned. A run always uses the current constants and reports
   whether they moved since the search was saved. Pinning ranking constants
   per saved search would freeze numbers that are documented as unmeasured,
   and would quietly give two people different orderings for the same question.

## Not in this slice

* The console surface. The sessions view has no search box yet: nothing in the
  console calls the session search endpoint, so there is nowhere for a "run
  this saved search" button to live. The contract above is what that view will
  call when it lands.
* Notifications and digests (decision 2).
* Sharing rules beyond private and account wide.
* Any measurement of whether the ranking constants deserve to be pinned.
