# DORA: the AI-agent slice

Preloop holds the AI-agent slice of an ICT estate: the agents, the tools and
MCP servers they reach, the models and providers they call through the
gateway, and the hosts that run them. It does not know about your databases,
your networks, your payment rails or your third-party contracts. Two exports
turn what it does hold into files you can hand to the people who assemble the
registers and run the incident process.

Nothing here is a DORA compliance product, and neither export is a filing.
They are inputs, produced from records Preloop already keeps.

## What these exports feed

| Export | Feeds | What stays with you |
| --- | --- | --- |
| Asset register | **Art. 8** ICT asset inventory (identification and classification of ICT assets) and the agent-slice lines of the **Art. 28** register of information (ICT third-party service providers) | Criticality, business function mapping, contractual data, everything outside the agent slice |
| Incident candidates | **Art. 17** ICT-related incident management process, as a source of events to review | Classification and, where it applies, reporting under **Art. 18** and **Art. 19** |

Art. 5 and Art. 6 sit behind both: the governance arrangements and the ICT
risk management framework are yours, and these files are evidence you can put
into them, not a substitute for having them.

## Asset register

```
GET /api/v1/exports/asset-register?format=csv
GET /api/v1/exports/asset-register?format=json
```

Console: **Audit > Asset register**. CLI: `preloop export asset-register`.

One flat table with a `record_type` column, so it can be opened in a
spreadsheet and filtered. Six record types:

| `record_type` | What it is | Source |
| --- | --- | --- |
| `agent` | An enrolled agent | `managed_agent` |
| `tool` | A tool your agents may call, builtin, MCP or HTTP | `tool_configuration` |
| `mcp_server` | An MCP server the platform connects to | `mcp_server` |
| `model` | A configured model | `ai_model` |
| `provider` | A model provider, derived from the models that point at it and from gateway usage | derived |
| `runner_host` | A machine registered to run flows | `flow_runner` |

Every line carries an owner where one is recorded, `first_seen` and
`last_seen` with a `last_seen_source` column saying which record dated it, and
`attached_policies` listing the controls attached to that asset: budget
policies, tool access rules, approval workflows. `parent_asset_id` links a
tool to its MCP server and a model to its provider.

A provider is not a table in Preloop. It is the third party your models point
at, which is exactly the thing an Art. 28 register wants a line for, so it is
derived and given a stable id (`provider:openai`) that two exports agree on.

What is deliberately not a row: the discovered tool catalogue an MCP server
advertises (`mcp_tool`). It is a cache of what a server said it could do, not
an asset your firm decided to hold. The tools you configured are the register
lines; the catalogue is visible in the console.

## Incident candidates

```
GET /api/v1/exports/incident-candidates?from=2026-01-01&to=2026-04-01&format=csv
```

Console: **Audit > Incident candidates**, which sends the date filter the
timeline is already showing. CLI:
`preloop export incident-candidates --from 2026-01-01 --to 2026-04-01`.

The period is half open, `[from, to)`, so consecutive periods tile: no row
lands in two files and none falls between them. With no bounds given the
server exports the last 30 days.

| `record_type` | What it is | Source |
| --- | --- | --- |
| `execution_failure` | A flow execution that ended in a failure status | `flow_execution` |
| `kill_switch_activation` | An account halt being turned on | `audit_log` |
| `policy_deny` | A tool call refused by policy | `audit_log`, Enterprise only |
| `budget_breach` | A gateway request refused because a budget was spent | `audit_log` |
| `gateway_upstream_failure` | A gateway call that failed at, or on the way to, the provider | `api_usage` |

Each row carries `occurred_at`, a `correlation_id` with a
`correlation_source` column naming which identifier it is, and the affected
agent where the record allows Preloop to resolve one.

A request the kill switch refused is not counted as an upstream failure: it is
the halt, and the halt is already its own record type.

### Classification is yours, not ours

They are called candidates on purpose. Art. 17 to 19 make the financial entity
classify an ICT-related incident and decide whether it is major and reportable.
Preloop cannot make that call: it cannot see your clients, your critical
functions, your data losses or your economic impact.

So the file carries no severity, no major or non-major flag and no
client-impact field. It carries `platform_category`, which is Preloop's own
technical vocabulary (a flow failure category, a gateway error class, a halt
scope) under a name that says so. Do not read it as a DORA classification.

A row appearing here does not mean an incident occurred. A failed execution is
usually a bad prompt or a flaky tool. The export's job is to make sure nothing
that might matter is invisible when you review the period.

## Manifest and digest

Both exports are wrapped in the same manifest shape as the CRA evidence pack
and the retention period export, so one verifier covers all of them:

```json
{
  "schema": "preloop.dora.asset_register_manifest/v1",
  "generated_at": "2026-03-15T12:00:00Z",
  "members": [{"name": "asset-register.csv", "size_bytes": 4096, "sha256": "..."}],
  "members_digest": "...",
  "counts": {"agent": 12, "tool": 40},
  "edition": {"edition": "oss", "fields_absent": []},
  "feeds": ["DORA Art. 8 (ICT asset inventory), agent slice", "..."]
}
```

JSON exports carry the manifest in the body next to the `rows` it digests: a
verifier can canonicalise the rows and recompute `members[0].sha256`. CSV
cannot carry it in the body, so it travels in response headers:

- `X-Preloop-Export-Sha256`: the digest of the file as served
- `X-Preloop-Members-Digest`: the manifest's members digest
- `X-Preloop-Export-Manifest`: the whole manifest, base64 of its canonical JSON

The CLI recomputes the digest from what actually landed and warns if it does
not match what the server reported.

What the digest proves is narrow: the bytes you hold are the bytes Preloop
produced. It says nothing about whether the underlying records were true when
they were written, and these exports are not signed.

## Editions: an empty column, never a missing one

The columns are identical in every edition. A register whose shape changes
with the deployment cannot be diffed against last quarter's, and a column that
quietly disappears reads as "nothing to report".

Fields the running deployment cannot fill are empty, and the manifest's
`edition` block names each one with the reason:

- `last_config_change_at` and `last_config_change_by` on asset rows come from
  `configuration_change` audit records, which only the Enterprise audit plugin
  writes. On other deployments they are empty.
- `policy_deny` incident rows are only persisted by that same plugin. On other
  deployments the count is zero and the manifest says the rows are unrecorded,
  which is not the same statement as "no tool call was ever denied". Policy
  denials still happen and are still enforced; they are simply not written to
  the audit trail without the plugin.

The CLI prints these notes after every export.

## Access and audit

Both endpoints require the `view_audit_logs` permission: they are a bulk read
of the same records the audit trail holds, in a different shape. Each export
writes its own audit record naming the actor, the format, the period, the row
counts and the digest of what was served, because who took the register and
what exactly they got are the questions asked afterwards.

An account with more than 100,000 records of one type gets HTTP 413 rather
than a truncated file. Narrow the period, or read the list APIs page by page.
A compliance export missing its tail is worse than no export at all.
