# Transcript evaluation preset

**Transcript evaluation** (`backend/presets/021-transcript-evaluation.yaml`,
slug `transcript-evaluation`) is a scheduled flow that reads the transcripts
deposited since its last run and turns what they say into suggestions for a
person and approved actions. Every run that finds transcripts leaves one
report artifact listing them.

It ships disabled. Open **Flows > New flow**, pick **Transcript evaluation**
under *Scheduled*, review the options below and enable it.

## What one run does

1. `search_artifacts` with `kind: [transcript]`, the run's time window
   (`since` = `trigger_event.payload.window.from`, `until` =
   `trigger_event.payload.window.to`, see
   [the run's time window](flow-triggers.md#the-runs-time-window)), the
   `site` label and the scope.
2. **Empty window:** nothing found, nothing deposited, nothing asked. The run
   succeeds with `transcripts: 0`.
3. `get_artifact` for each transcript (the first 64 KiB; a cut transcript is
   marked in the report).
4. Each finding is classified as **info**, **suggestion** or **action**.
5. **Suggestions** go to people as **one** `ask_user` question per run, with
   one row per suggestion to accept or dismiss. It appears in **Attention**.
6. **Actions** (a mutating tool such as `create_task` or
   `propose_workflow_change` from your own MCP server) are preceded by
   `request_approval`, with the transcript excerpt and the artifact link in
   the request. The call is made only when the approval is granted; a denied
   or expired approval is recorded, not retried.
7. Optionally `send_note` to the session that produced a transcript.
   `send_note` reaches only the runs this run started. Noting another
   agent's session needs a tool access rule on `send_note` that grants
   the `account` scope; without it the note is refused and recorded, and
   the run continues.
8. One `document` artifact is deposited with labels
   `{report: transcript-evaluation, window_from, window_to, site}`. It links
   every evaluated transcript by its `resource_link` URI.

## Options (`schedule_config.payload`)

| Key | Default | Meaning |
|-----|---------|---------|
| `scope` | `own` | `own`: transcripts from sessions of this flow's own runs. `account`: every agent's transcripts. |
| `labels.site` | `""` | Only transcripts with this `site` label. Empty matches every site. |
| `kinds` | `[transcript]` | Artifact kinds to evaluate. |
| `answers` | `{}` | Filled by the platform when a parked run resumes; leave it empty. |

### Questions are asked once

`ask_user` and `request_approval` park the run until a person decides; the
run then resumes. A run that parks twice (the question, then an approval)
resumes the second time with a prompt that names only the latest decision,
and a harness that cannot continue its session starts again from step 1.
The platform keeps every decision of the run in
`trigger_event.payload.answers`, and the prompt shows that map, so the run
uses the earlier answer instead of asking the person again.

The schedule is hourly (`0 * * * *`, UTC). Change it on the flow like any
other schedule; the window follows.

## Same-agent and cross-agent mode

- **Same-agent mode** (`scope: own`) works in every edition. The run sees
  the artifacts of sessions run by this flow, across all its runs, because
  `search_artifacts` scopes to the caller's own agent identity by default.
  Use it when the same flow also captures the transcripts, or extend its
  prompt to do so.
- **Cross-agent mode** (`scope: account`) evaluates transcripts deposited by
  any agent of the account. It needs the `artifact_search.account_scope`
  grant for this flow, which is written through the Enterprise Edition
  governance settings. Without the grant `search_artifacts` refuses the call
  with `account_scope_not_granted`; the run ends with status `error` and that
  reason instead of quietly evaluating a narrower set.

## Action tools

The preset allows only `search_artifacts`, `get_artifact`,
`deposit_artifact`, `ask_user`, `request_approval` and `send_note`. Add the
action tools you want the evaluator to use (for example `create_task` from a
ticketing MCP server) to the flow's tool list. When an action tool is not on
the list the finding becomes a suggestion. To make the approval binding
rather than instructed, add a tool access rule that requires approval for
those tools as well. `send_note` still reaches only the runs this run
started unless a tool access rule on `send_note` grants the `account` scope.

## Result

`/workspace/result.json`:

```json
{"status": "success", "window": {"from": "...", "to": "..."},
 "transcripts": 3, "suggestions": 2, "actions_approved": 1,
 "actions_denied": 0, "report_artifact_id": "..."}
```
