# Where an approval raised inside a child execution goes

A child execution that asks a human must not park a subtree forever. This page
records what happens today, decides the three open questions, and states what
a parent sees when a child ends up waiting for a person, so the parking child
of the delegation PRD can be implemented without guessing.

It is a design note, not a feature. Nothing in this page changes behaviour.
The object shapes it refers to are frozen in
`docs/guide/flow-delegation-shapes.md`.

Verified on main `5b592ece`.

## Short version

Routing needs no change. A child's question already reaches the same humans a
parent's question reaches, because both run as the same account principal and
resolve the same account scoped approval workflow, and because an approval
always carries an `expires_at` bounded by the account cap, an unanswered
question always ends. A child that asks is therefore an interrupted child, not
a terminal one, and a parent may keep waiting for it until the parent's own
deadline.

Two things still have to be got right, and neither is approval routing:

1. A parent must read a parked child's outcome through the execution that
   continued it (`flow_execution.resume_execution_id`), not off the parked row,
   which is closed with a terminal status and a null `result`.
2. The execution that continues a parked run must carry the lineage columns of
   the run it continues, or a child that parks leaves the delegation tree.
   That is the one defect this spike found; it is fixed (see the follow up
   below).

## What happens today

### The call

1. The agent inside a child calls `ask_user` or `request_approval` over MCP
   (`backend/preloop/services/initialize_mcp.py:647` for `ask_user`).
2. `ask_user` resolves the approval workflow from the account: the workflow
   named in the tool call's `approval_workflow` argument, else the account's
   default workflow (`initialize_mcp.py:683` and
   `backend/preloop/models/crud/approval_workflow.py:43` / `:91`). Nothing in
   that resolution reads the calling execution, its flow or its ancestry.
3. `require_approval` (`backend/preloop/services/approval_helper.py:308`)
   creates the request. The requester identity comes from the MCP caller's
   own context (`approval_helper.py:668`, built in
   `backend/preloop/services/approval_attribution.py`), whose
   `flow_execution_id` is read from the runtime token's `context_data`
   (`backend/preloop/services/dynamic_fastmcp.py:2014`). For a child that is
   the child's own execution id.
4. The decision window is resolved before the row exists
   (`approval_helper.py:676` calling
   `backend/preloop/services/approval_window.py:95`): the tool call's
   `timeout_seconds`, else the child's own flow's `approval_window_seconds`,
   else the workflow timeout, else the deployment default, always clamped by
   the account cap (`approval_window.py:79`, 30 days by default).
5. `create_and_notify` (`backend/preloop/services/approval_service.py:1991`)
   writes the `approval_request` row and notifies. Recipients are the
   workflow's `approver_user_ids` plus the members of its `approver_team_ids`
   (`approval_service.py:2699`), through each user's own notification
   preferences, plus whatever webhook channel the workflow configures
   (`approval_service.py:2266`). The console and the approval link page serve
   the same request to anyone in the account who may act on it.
6. If the window is longer than the short in-process wait, the child parks:
   `park_request_id` is stamped on its row, the orchestrator releases the
   container and the status becomes `WAITING_FOR_HUMAN`
   (`backend/preloop/services/approval_park.py:47`, `:108`, `:214`). The tool
   call returns the `parked_for_human` payload telling the agent to stop.
7. The decision, or the expiry sweep (`approval_park.py:488`), claims the
   parked row once and starts a **new** execution that continues the same
   agent session (`approval_park.py:394`). When that continuation finishes,
   the parked row is closed with the continuation's terminal status and
   `end_time` (`backend/preloop/models/crud/flow_execution.py:1466`). Its
   `result` stays null: the result is on the continuation.

### So where does a child's question go?

To the account's approvers, on exactly the same path as a question raised by a
parent or by a flow with no lineage at all. There is no per execution approver
identity to route by, and there does not need to be one, because there is no
per execution owner either: every flow runtime token is minted as the account's
primary user (`backend/preloop/services/flow_runtime_token.py:124-150`). The
parent's owner and the child's owner are the same person by construction.
"Route it to the parent's owner" is already what happens.

`ApprovalWorkflow` is account scoped and carries no execution reference
(`backend/preloop/models/models/tool_configuration.py:170`). The
`approval_request` row records which execution asked (`execution_id`), which
API key, which managed agent and which runtime session, and no lineage.

### Evidence, and what is missing from it

This note is written from the code, and from the park and delegation test
suites that exercise these paths (`backend/tests/test_approval_park.py`,
`backend/tests/a2a/`). It is **not** written from a live child run, because
the tool that lets one execution start another does not exist yet: on
`5b592ece` nothing writes `parent_execution_id`, so no child execution can be
produced to observe. The decisions below therefore have to be re-checked the
first time a real `run_flow` child raises a question, and the pins in
`backend/tests/a2a/test_child_approval_routing.py` are what will fail if any
of this drifts before then.

## The three decisions

### 1. Does a child inherit the parent's resolved approval workflow and requesting subject?

**No, and nothing needs building.**

There is no requesting subject to inherit: both executions authenticate as the
account's primary user. A child resolves its workflow the way every caller
does, by name or by account default, and the account default is the same
workflow the parent would have used.

Inheriting the parent's *named* workflow would be actively wrong. The
`approval_workflow` argument on `ask_user` is a per call choice made by the
agent for one question. Copying it onto a child would route the child's
unrelated question to the channel the parent happened to name for a different
decision, and would do so invisibly.

If a deployment ever wants a delegated subtree to route somewhere specific,
the knob that already exists is the child flow's own configuration: the child
flow can name its workflow, and `flow.approval_window_seconds` already gives
it its own window. That is a per flow decision an operator can see, not a per
call inheritance rule nobody can see.

### 2. Should the approval request record the parent and root execution ids?

**No. Do not add columns to `approval_request`.**

The request already records `execution_id`, and lineage already lives on
`flow_execution` (`parent_execution_id`, `root_execution_id`,
`delegation_depth`, `backend/preloop/models/models/flow_execution.py:252-259`).
A console tree row that wants to show a pending question joins the approval's
`execution_id` to the execution it is already rendering. One join, no
migration, no backfill of historical approvals, and no second copy of the tree
that can disagree with the first.

This holds on one condition, which is the follow up below: the continuation of
a parked child must carry the same lineage as the run it continues, or the
join finds an execution that is not on the tree.

### 3. Is the child's approval window bounded by the parent's remaining child wait?

**No. Do not clamp it. Bound the parent instead.**

The window is already bounded: every request gets an `expires_at`, no window
may exceed the account cap, and the sweep expires the request and resumes the
run with an explicit `expired` answer so the agent finishes gracefully
(`approval_park.py:488`). Nothing parks forever.

Clamping the child's window to the parent's remaining wait would make the same
question expire differently depending on who asked for the run, which is the
opposite of what a governance window is for: a waiver decision does not become
less serious because the caller is impatient. It would also be a silent
shortening, visible to the human only as a window that closed early.

The bound that belongs to the parent is the parent's own wait deadline. When
it passes and a child is still waiting for a person, the parent stops waiting
and reports it. The child keeps its window and keeps its human.

## What the parent's completion record says

A child that is waiting for a human is reported with the shapes already frozen
in `docs/guide/flow-delegation-shapes.md`. No new metadata key is needed and
none may be added: that vocabulary is closed.

```json
{
  "id": "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f",
  "contextId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": "2026-09-15T10:04:11Z"},
  "metadata": {
    "preloop.ai/kind": "delegation_task",
    "preloop.ai/executionId": "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f",
    "preloop.ai/parentExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/rootExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/flowId": "9a8b7c6d-5e4f-4031-9283-1a2b3c4d5e6f",
    "preloop.ai/flowName": "Repository review",
    "preloop.ai/depth": 1,
    "preloop.ai/status": "WAITING_FOR_HUMAN",
    "preloop.ai/consoleUrl": "/console/flows/executions/5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f"
  }
}
```

The rules the parking child implements, stated so it does not have to decide
them:

- **`TASK_STATE_INPUT_REQUIRED` is not terminal and not a failure.** A child in
  `WAITING_FOR_HUMAN` has not finished, has no result artifact, holds no
  runtime and is not burning budget. A parent still waiting on children must
  not count it as done.
- **The question is reached through the child, not through the parent.** The
  parent's record links to the child execution (`preloop.ai/consoleUrl`), and
  the pending question is on that execution and in the account's approvals
  queue, where it was delivered to the same humans in the first place. The
  parent does not restate the question and does not carry the approval request
  id; that would be a second copy of a record the console already renders.
- **When the parent's own wait deadline passes with a child still waiting**,
  the parent resumes and reports that child exactly as above, with
  `preloop.ai/status` `WAITING_FOR_HUMAN`. The parent's report says the
  subtree is with a person, not that it failed and not that it succeeded with
  no result. The child is not stopped: its window is its own, and stopping it
  would throw away work already paid for. This is the same question #689 asks
  about an operator initiated stop, and the answer there governs that case.
- **When the decision arrives before the parent's deadline**, the child
  continues as a *different execution row*. The parked row is closed with the
  continuation's terminal status but its `result` is null
  (`backend/preloop/models/crud/flow_execution.py:1466`). A parent reading the
  child's result must follow `flow_execution.resume_execution_id` to the end
  of the chain, which may be more than one hop if the child parks twice. Do
  not copy results backwards onto parked rows: that creates a second source of
  truth for the one field the whole delegation feature exists to deliver.

## The follow up

A continuation created for a parked run used to be created with only
`flow_id`, `status` and `trigger_event_details`, so `parent_execution_id`,
`root_execution_id` and `delegation_depth` fell back to null, null and 0. That
was invisible while nothing wrote lineage. The moment delegation writes it, a
child that parks on a question would drop out of its tree: the console tree
loses the branch, a cost rollup keyed on the root undercounts the
continuation, and a depth cap stops counting the hops it already spent.

The fix was small and is one pull request: `_start_resume_execution`
(`backend/preloop/services/approval_park.py`) now carries the parked row's
lineage unchanged onto the continuation. Unchanged, not incremented, because a
continuation is the same logical child carrying on, not a new one: the park
link is already expressed by `resume_execution_id`, and incrementing the depth
would let a question a human answered consume a delegation hop.

Filed and fixed as [#707](https://github.com/preloop/preloop/issues/707).

## Pinned behaviour

`backend/tests/a2a/test_child_approval_routing.py` pins what this note asserts,
so the recommendations cannot quietly stop being true:

- workflow and approver resolution read the account, never the caller's
  lineage, and `approval_request` carries no lineage columns
- the window resolver takes no parent argument and every resolved window is
  bounded by the account cap
- `WAITING_FOR_HUMAN` maps to `TASK_STATE_INPUT_REQUIRED`, is not terminal and
  is not in the set of statuses that close a parked parent
- a continuation is created with the parked run's trigger context and with its
  lineage carried across unchanged, including a root run that has none
