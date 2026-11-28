# Flow state

A flow run starts from a clean workspace. Flow state lets it keep small JSON
documents across runs: a ledger of failures it has seen, a cursor recording
where the last run stopped, a baseline to compare against. The state belongs
to the flow, not to a run, and is deleted together with the flow.

## From a run: `get_flow_state` and `put_flow_state`

Both tools are off by default. A flow opts in by listing them in
`allowed_mcp_tools`:

```yaml
allowed_mcp_tools:
  - name: get_flow_state
  - name: put_flow_state
```

The flow is never an argument. It comes from the run's credential, so a run
can only read and write the state of its own flow.

| Call | Result |
| --- | --- |
| `get_flow_state({})` | The flow's keys with their version and size, without values. |
| `get_flow_state({"key": "cursor"})` | `{"key", "value", "version", "updated_at", "updated_by_execution_id"}`. A missing key returns `value: null`, `version: 0`. |
| `put_flow_state({"key": "cursor", "value": {...}})` | Creates or replaces the value; returns the new `version`. |
| `put_flow_state({..., "expected_version": 3})` | Writes only if the stored version is still 3 (`0` means "only if new"). Otherwise the call fails with `flow_state_version_conflict`. |

Read the value, change it and write it back with the version you read. If a
concurrent run (for example a manual run next to a scheduled one) wrote in
between, the write fails. Read again and merge, instead of losing that
run's update.

### Limits

| Limit | Value |
| --- | --- |
| Key | 1-128 characters of `a-z 0-9 . - _`, starting with a letter or digit |
| Value | Any JSON, at most 256 KB encoded |
| Keys per flow | 32 |

Keep state compact: counts, ids, timestamps and short summaries. Logs and
reports belong in the run's evidence or artifacts.

### Reserved keys

Keys that start with `preloop.` are written by platform features. One
example is `preloop.ci_health`, the failure ledger the
[CI health collector](ci-health.md) maintains. A run can read them, but
`put_flow_state` refuses them.

## From the API and the console

| Route | Permission | Notes |
| --- | --- | --- |
| `GET /api/v1/flows/{flow_id}/state` | `view_flows` | List keys. |
| `GET /api/v1/flows/{flow_id}/state/{key}` | `view_flows` | Read one value. |
| `PUT /api/v1/flows/{flow_id}/state/{key}` | `edit_flows` | Body `{"value": ..., "expected_version": N}`; 409 on a version conflict, 403 for a reserved key. |
| `DELETE /api/v1/flows/{flow_id}/state/{key}` | `edit_flows` | Reset a key, reserved keys included. The owning feature starts over on its next run. |

State is data the flow wrote. A prompt that reads it should treat it as
input, not instructions, like any other tracker or repository content.
