# Flow to flow delegation: the object shapes

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

When one flow runs another, three records describe the call: the **request**
the parent sends, the **task** record the child execution is represented by,
and the **artifact** the child's result becomes. This page freezes those
shapes. Everything built on flow to flow delegation implements against this
page rather than inventing its own field names.

This is internal delegation in A2A shapes. It is **not A2A support**. There is
**no JSON-RPC endpoint**, **no Agent Card**, no external counterparty and no
streaming event in Preloop today, and nothing on this page adds one. The
shapes are borrowed so that a future serving or governing surface inherits a
data model instead of migrating one.

## What this maps to

The shapes implement A2A protocol version 1.0, as frozen by specification
release v1.0.1, and are written for the `JSONRPC` binding.

| | |
| --- | --- |
| Specification | A2A Protocol Specification v1.0.1 |
| Protocol version | 1.0 (A2A names a version by the `Major.Minor` of its specification release) |
| Binding shaped for | `JSONRPC`, JSON-RPC 2.0 over HTTP(S), the binding in section 9 of that specification |
| JSON serialisation | ProtoJSON, as A2A's ADR-001 makes normative |

ProtoJSON is why the records look the way they do:

- member names are camelCase (`messageId`, `contextId`, `artifactId`)
- enum values are `SCREAMING_SNAKE_CASE` (`ROLE_USER`, `TASK_STATE_WORKING`)
- a union member is identified by **the member name itself**, so a text part
  is `{"text": "..."}` and a data part is `{"data": {...}}`. The `kind`
  discriminator of A2A 0.2.x is not part of the protocol any more and is not
  emitted here.

Operator notes (`docs/guide/operator-notes.md`) store an A2A shaped message in
the older 0.2.x style, with a `kind` discriminator. That predates this page.
Delegation does not copy it, and converting the note envelope is its own
change, not a side effect of this one.

## The three records

### 1. The request: a message

What a parent sends when it asks another flow to run. An A2A `Message` with
`role: ROLE_USER`, because A2A reserves the user role for the client side of a
task and the parent is the client here. The instruction goes in a text part,
structured inputs go in a data part, and either may be omitted as long as one
part is present.

<!-- validates: delegation_request -->
```json
{
  "messageId": "1b9c2f3d4e5a6b7c8d9e0f1a2b3c4d5e",
  "contextId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
  "role": "ROLE_USER",
  "parts": [
    {"text": "Review the repository and report findings."},
    {"data": {"repository": "example/widget", "since": "2026-09-01"}, "mediaType": "application/json"}
  ],
  "metadata": {
    "preloop.ai/kind": "delegation_request",
    "preloop.ai/parentExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/rootExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/flowId": "9a8b7c6d-5e4f-4031-9283-1a2b3c4d5e6f",
    "preloop.ai/flowName": "Repository review",
    "preloop.ai/depth": 1
  }
}
```

`contextId` is the delegation tree: the root execution id, which is what makes
a whole tree of calls one context rather than a pile of unrelated messages.
A request never carries `preloop.ai/executionId`; at request time the child
does not exist.

### 2. The child execution: a task

How a parent sees a child. An A2A `Task` with `id`, `contextId`, `status` and
`artifacts`. The id is the child execution id, so the record and the row it
describes are the same thing addressed two ways.

<!-- validates: delegation_task -->
```json
{
  "id": "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f",
  "contextId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-09-15T10:04:11Z"},
  "artifacts": [
    {
      "artifactId": "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f/result",
      "name": "result",
      "parts": [{"data": {"verdict": "pass", "findings": []}, "mediaType": "application/json"}]
    }
  ],
  "metadata": {
    "preloop.ai/kind": "delegation_task",
    "preloop.ai/executionId": "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f",
    "preloop.ai/parentExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/rootExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/flowId": "9a8b7c6d-5e4f-4031-9283-1a2b3c4d5e6f",
    "preloop.ai/flowName": "Repository review",
    "preloop.ai/depth": 1,
    "preloop.ai/status": "SUCCEEDED",
    "preloop.ai/cost": 0.42,
    "preloop.ai/tokens": 18422,
    "preloop.ai/consoleUrl": "/console/flows/executions/5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f"
  }
}
```

A2A's `Task` also has a `history` of messages. Delegation does not populate it:
the execution log already holds the turns, and copying them into a second
place would be a second source of truth. The schema does not allow the field,
so nobody half fills it.

### 3. The result: an artifact with a data part

A child's structured result (`FlowExecution.result`, the parsed
`/workspace/result.json` served by `GET /flows/executions/{execution_id}/result`)
becomes one artifact on the task, named `result`, carrying a single data part
whose `data` is the result document unchanged. A child that reported no
structured result has no artifact, not an empty one.

The artifact id is the child execution id with a `/result` suffix, which keeps
it unique within the task without inventing a second identifier to store.

## Status mapping

Preloop execution statuses map to A2A task states as follows. Every status the
executor writes has a row; an unmapped status raises rather than defaulting,
because a new status is a decision.

| Preloop status | A2A task state | Why |
| --- | --- | --- |
| `PENDING` | `TASK_STATE_SUBMITTED` | Accepted, queued, nothing running |
| `INITIALIZING` | `TASK_STATE_SUBMITTED` | Container coming up; no model turn has run |
| `STARTING` | `TASK_STATE_SUBMITTED` | Runtime coming up; no model turn has run |
| `RUNNING` | `TASK_STATE_WORKING` | The agent is working |
| `RESUMING` | `TASK_STATE_WORKING` | A parked run being restarted is working again |
| `WAITING_FOR_HUMAN` | `TASK_STATE_INPUT_REQUIRED` | Alive, holding no runtime, waiting for an answer. Interrupted, not terminal |
| `WAITING_FOR_CHILDREN` | `TASK_STATE_WORKING` | Alive, holding no runtime, waiting for the flows it started. Nobody outside has to act |
| `SUCCEEDED` | `TASK_STATE_COMPLETED` | Terminal, the work was done |
| `FAILED` | `TASK_STATE_FAILED` | Terminal, the work was attempted and ended badly |
| `TIMEOUT` | `TASK_STATE_FAILED` | Nobody asked for the clock to run out |
| `TIMED_OUT` | `TASK_STATE_FAILED` | Same case, the other spelling the executor writes |
| `ABORTED` | `TASK_STATE_FAILED` | Infrastructure killed it; no one requested that |
| `STOPPED` | `TASK_STATE_CANCELED` | Someone asked for the stop |
| `CANCELLED` | `TASK_STATE_CANCELED` | Same case, the other spelling the executor writes |
| `CANCELED` | `TASK_STATE_CANCELED` | Same case, the single-l spelling |
| `REFUSED` | `TASK_STATE_REJECTED` | Policy declined the call; no child was created |

### Refusal is not failure

`REFUSED` is the one status that is never stored on an execution row. A
refused delegation creates no child: the parent asked, a rule said no, nothing
ran and nothing was charged. A2A has exactly this state, `TASK_STATE_REJECTED`,
described as the agent deciding not to perform the task, and it is separate
from `TASK_STATE_FAILED` for the same reason we keep them separate: retrying a
failure can work, retrying a refusal without changing the policy cannot.

A refused task record carries no artifacts, no `preloop.ai/executionId` (there
is no row to point at) and a mandatory `preloop.ai/refusalReason` from this
list:

| Reason | Meaning |
| --- | --- |
| `flow_not_found` | No flow with that id in the caller's account |
| `flow_not_callable` | The target flow does not list the caller in its callable flows allowlist |
| `tool_not_allowed` | The calling flow is not allowed the delegation tool at all |
| `firewall_denied` | The tool policy firewall denied this specific call |
| `depth_exceeded` | The child would sit deeper than the delegation depth cap |
| `cycle_detected` | The target already appears in this call's ancestry |
| `fanout_exceeded` | The parent has already started as many children as it may |
| `budget_exceeded` | The remaining budget cannot cover another child |
| `execution_not_found` | The execution asked for is not the caller's own or one of its descendants. The one reason a read is refused with: a target in another account, an unrelated one in the same account and one that never existed are all answered in these words, so a refusal cannot be used to learn what exists |

<!-- validates: delegation_task -->
```json
{
  "id": "7d8e9f0a-1b2c-4d3e-8f40-5a6b7c8d9e0f",
  "contextId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": {"state": "TASK_STATE_REJECTED", "timestamp": "2026-09-15T10:04:11Z"},
  "metadata": {
    "preloop.ai/kind": "delegation_task",
    "preloop.ai/parentExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/rootExecutionId": "3f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "preloop.ai/flowId": "9a8b7c6d-5e4f-4031-9283-1a2b3c4d5e6f",
    "preloop.ai/depth": 2,
    "preloop.ai/status": "REFUSED",
    "preloop.ai/refusalReason": "depth_exceeded"
  }
}
```

Which rules exist, and in what order they run, belongs to the issue that
implements the call. This page fixes only the vocabulary a refusal is reported
in.

## Metadata keys

A2A leaves `metadata` open. Preloop's keys are namespaced under `preloop.ai/`,
the convention operator notes already set, and here the namespace is closed:
the schemas allow these keys and no others, so adding one is an edit to the
schema, this page and a test in the same change.

| Key | Meaning | Where |
| --- | --- | --- |
| `preloop.ai/kind` | Which record this is: `delegation_request` or `delegation_task` | both |
| `preloop.ai/executionId` | The child execution this task stands for | task |
| `preloop.ai/parentExecutionId` | The execution that made the call (`flow_execution.parent_execution_id`) | both |
| `preloop.ai/rootExecutionId` | First execution of the tree (`flow_execution.root_execution_id`); null when the caller is the root | both |
| `preloop.ai/flowId` | The flow asked to run, or that ran | both |
| `preloop.ai/flowName` | That flow's name at call time, for reading | both |
| `preloop.ai/depth` | Distance from the root (`flow_execution.delegation_depth`): 0 is the root, 1 a direct child | both |
| `preloop.ai/status` | The Preloop status the task state was mapped from, so the mapping stays inspectable | task |
| `preloop.ai/cost` | Estimated cost of the child so far, in USD | task |
| `preloop.ai/tokens` | Total tokens the child has spent so far | task |
| `preloop.ai/consoleUrl` | Console link to the execution | both |
| `preloop.ai/refusalReason` | Which rule declined the call; present only on a refusal | task |

Required on a request: `preloop.ai/kind`, `preloop.ai/parentExecutionId`,
`preloop.ai/flowId`, `preloop.ai/depth`. Required on a task record:
`preloop.ai/kind`, `preloop.ai/flowId`, `preloop.ai/depth`,
`preloop.ai/status`, plus `preloop.ai/executionId` unless the state is
`TASK_STATE_REJECTED`, in which case `preloop.ai/refusalReason` is required
instead.

## Schemas and the validator

| Thing | Where |
| --- | --- |
| Request schema | `backend/preloop/a2a/schemas/delegation_request.schema.json` |
| Task schema | `backend/preloop/a2a/schemas/delegation_task.schema.json` |
| Helper and status table | `backend/preloop/a2a/delegation.py` |
| Contract tests | `backend/tests/a2a/test_delegation_shapes.py` |

```python
from preloop.a2a.delegation import (
    DelegationShapeError,
    task_state_for_status,
    validate_delegation_request,
    validate_delegation_task,
)

validate_delegation_request(request)  # raises DelegationShapeError
state = task_state_for_status(execution.status)  # "TASK_STATE_WORKING"
```

Both schemas are JSON Schema draft 2020-12 and both are closed
(`additionalProperties: false`) everywhere a Preloop field vocabulary appears.
That is the point of freezing them: an unknown key is a mistake caught in a
test, not a field half the codebase later has to support.

`validate_delegation_request` and `validate_delegation_task` import
`jsonschema` lazily, because validation is a build and test time guard; no
request path validates a record today, since no request path produces one yet.

## Deliberately not implemented

- No JSON-RPC endpoint, no `SendMessage` or `GetTask` method, no HTTP surface
  of any kind.
- No Agent Card, signed or otherwise, and no discovery.
- No external counterparty: nothing is sent to or accepted from another
  vendor's agent.
- No streaming events: no `TaskStatusUpdateEvent`, no
  `TaskArtifactUpdateEvent`, no Server-Sent Events.
- No delegation tool, no lineage writes, no parking on children, no budgets,
  no console tree. Those are separate changes that will use these shapes.
- No migration and no behaviour change: this page and the files beside it add
  a vocabulary, not a code path.
