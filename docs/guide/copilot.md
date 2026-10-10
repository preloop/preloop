# Copilot coverage

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

What Preloop governs and meters on each GitHub Copilot surface. The
longer guides stay the setup steps. This page is the matrix. It describes
the current release line: check it against the version you have deployed
before quoting it.

Rows are surfaces. Columns are:

- **MCP governed:** policies, approvals, and audit for MCP tool calls
  that go through Preloop.
- **Models metered:** tokens, cost, and session replay for the model
  request itself.
- **Hook sessions:** a runtime session from a Copilot hook.
- **Spend visible:** gateway usage, or the premium-request import.
- **Dollars per ticket:** whether a run's money can be attributed to a
  ticket, an execution or a session. A count, a daily report or a seat
  estimate is not ticket-level dollars, and an unknown amount is not a
  zero.
- **Publication and feedback:** whether the surface pushes branches,
  opens pull requests, or continues a run from a review comment.
- **Status today:** shipped, planned (with an issue), or not possible
  (with the reason).

Those are separate answers, and a surface can have one without the
others. The private-runner host flow below governs MCP and records hook
sessions while it meters no model traffic and prices no ticket. It
publishes only through the opt-in managed path for one Bitbucket Cloud
repository.

Session replay is the console timeline of gateway `ApiUsage` rows
(`docs/architecture/gateway.md`, Current Explorer Surface). A session
with no captured gateway requests stays replay-ineligible. Hook ingest
can still open a runtime session without that timeline.

GitHub-hosted model traffic never passes through the Preloop gateway.
There is no proxy for those models. The [premium-request import](copilot-usage-import.md)
stores what GitHub reports (seats, daily premium-request `netAmount`,
usage-metrics counters). It is not per request, it is not matched to a
ticket, and it is not a transcript.

## Matrix

| Surface | MCP governed | Models metered | Hook sessions | Spend visible | Dollars per ticket | Publication and feedback | Status today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| VS Code Copilot Chat, GitHub-hosted models | Yes, for MCP servers Preloop writes | Not possible: no proxy for GitHub-hosted models | Not wired: VS Code hooks are GitHub preview; Preloop writes no such file | Premium-request import | No. Per user and day only | Not this surface: a person drives the chat | MCP shipped. Metering not possible; hooks not wired |
| VS Code Copilot Chat, BYOK (custom endpoint) | Same MCP path as the row above | Gateway rows for the Custom Endpoint Chat Completions shape (`tools`, `stream: true`) and the Responses shape, attributed to the agent credential. Interactive VS Code is a founder checklist ([#787](https://github.com/preloop/preloop/issues/787)) | Not wired: VS Code hooks are GitHub preview; Preloop writes no such file | Gateway for calls that reach Preloop. Import stays GitHub-reported ([#788](https://github.com/preloop/preloop/issues/788)) | No. Gateway rows are per request, not per ticket | Not this surface | Documented manual BYOK. Interactive session unverified ([#787](https://github.com/preloop/preloop/issues/787)) |
| Copilot CLI, GitHub-hosted, interactive on a laptop | Yes, after onboard. Native tools need `--approvals` | Not possible: no proxy for GitHub-hosted models | Yes, lifecycle only | Premium-request import | No. Per user and day only | Not this surface: a person drives the session | Shipped |
| Copilot CLI, GitHub-hosted, flow on a private-runner host profile | Yes, from two separate sources: the runner user's own MCP file, and the flow's `preloop-flow` server, which the runner supplies per job with a short-lived, execution-scoped token | Not possible: no proxy for GitHub-hosted models | Yes. The runner installs usage hooks before the run | Execution: not gateway metered, plus a premium-request count. Dollars: import | No. The count is not tokens and not a price, and the import is per user and day. Unknown stays unknown | Opt-in managed publication for one Bitbucket Cloud repository (profile `allow_publish`, runner capability `host_publication`). Native feedback resume is rejected before the run | Shipped ([#956](https://github.com/preloop/preloop/issues/956)), with the flow MCP server. Publication: [#1069](https://github.com/preloop/preloop/issues/1069), continuation planned |
| Copilot CLI BYOK through the gateway (`preloop copilot`) | Same CLI MCP and approval hooks | Yes: tokens, cost, and session replay | Yes, when the CLI hooks are installed | Gateway. Not the premium-request import | Yes for a gateway flow: gateway rows carry the session and the execution. The local launcher attributes to the session, not to a ticket | Not this launcher. Publication belongs to the flow's harness; the local launcher pushes nothing | Shipped |
| Copilot cloud coding agent on GitHub.com | Yes. Preloop policy on `/mcp/v1` | Not possible: no proxy for GitHub-hosted models | Not installed. Preloop does not write `.github/hooks` | Import is daily, not a session. Usage metrics exclude Copilot Chat on GitHub.com | No. The import is per user and day, and the job is not matched to a row | Native on GitHub, outside Preloop. The agent's comments and pull requests are GitHub activity, not a Preloop publication record | MCP shipped. Hooks not installed |
| Copilot inline completions | Not applicable. A completion is not an MCP tool call | Not possible: no proxy for GitHub-hosted models | Not possible: no hook surface for completions | Premium-request import, as daily aggregates only | No | Not applicable | Not possible for live governance or metering |

## Where each cell comes from

### VS Code Copilot Chat, GitHub-hosted models

`preloop agents onboard "VSCode / Copilot"` writes the user MCP file
`~/.vscode/mcp.json` (`cli/internal/cmd/agents.go`, config path
`.vscode/mcp.json`). Tool calls that client makes through Preloop are
governed: policies, approvals, and audit on `/mcp/v1`. Copilot's
built-in edit and terminal tools are not MCP calls.
`permissionSourceForAgent` has no VS Code branch
(`cli/internal/cmd/agents_approval_hooks.go`), so those built-in tools
are not routed to Preloop approvals.

`supportsManagedGateway` does not include this agent
(`cli/internal/cmd/agents.go`). Discovery reports support level
`mcp-only` (`cli/internal/cmd/agents_preflight.go`). Model calls stay
on GitHub. No tokens, no gateway cost, no session replay.

Preloop does not install a hook into VS Code Chat. GitHub's hooks page
lists Copilot CLI and the Copilot cloud agent, not VS Code Chat
(read 2026-09-27). The customization cheat sheet marks VS Code hooks as
preview (read 2026-09-27). That preview is not a file Preloop writes.
`isCopilotVSCodeHookEventName` in `cli/internal/cmd/usage_hook_copilot.go`
accepts the PascalCase payload if something pipes it in. Nothing in
onboarding writes a VS Code hook config, so sessions stay unrecorded.

Spend for this row is the [premium-request import](copilot-usage-import.md):
daily, marked not metered by the gateway
(`backend/preloop/services/copilot_usage_import.py`). It is stored per
user, per day and per model, so it cannot price a ticket. A day with no
imported row is unknown, not zero. Publication and feedback are not this
surface: a person drives the chat.

### VS Code Copilot Chat, BYOK (custom endpoint)

MCP governance is the same user file as the GitHub-hosted row. Onboarding
writes that MCP file and leaves `chatLanguageModels.json` alone.

The manual path is [VS Code Copilot Chat](clients/vscode-copilot.md):
**Chat: Manage Language Models** -> **Add Models** -> **Custom Endpoint**,
with `toolCalling: true` and the full gateway URLs. The gateway regression
test `backend/tests/endpoints/test_openai_gateway_issue_787.py` replays
Chat Completions (`tools`, `stream: true`) and the Responses shape with
an agent credential and a fake upstream, and checks that the usage row
stores tokens and cost for that agent. An interactive VS Code session
was not run. That session, and a licensed Business or Enterprise seat,
are the founder checklist on that page
([#787](https://github.com/preloop/preloop/issues/787)).

Plan pages checked 2026-10-10 are cited on that page. They say BYOK works
with no Copilot plan, and that Business and Enterprise need an admin
policy. They do not name Copilot Free, Pro, and Pro+ as separate rows.

The hook cell is the same as the GitHub-hosted row: VS Code hooks are
a GitHub preview, and Preloop writes no such file.

Gateway spend and the import are different ledgers. The import stores
GitHub's reported figures only. A call that never reached GitHub is not
in that import. [#788](https://github.com/preloop/preloop/issues/788)
says not to double-count BYOK traffic that does go through the gateway.

Gateway rows for this path are per request. They are not ticket-level
dollars. GitHub-hosted model picks on this surface still show up only in
the daily per-user import. Publication and feedback stay outside this
surface either way.

### Copilot CLI, GitHub-hosted models, interactive

`preloop agents onboard "Copilot CLI"` writes `~/.copilot/mcp-config.json`
(or `$COPILOT_HOME`). That is MCP governance. It does not repoint model
traffic (`cli/internal/cmd/agents.go`: inference stays on GitHub's
backend). Native shell and file tools are governed only when onboarding
used `--approvals`, which adds `preToolUse`
([usage hooks](usage-hooks.md), [#898](https://github.com/preloop/preloop/issues/898)).

Running `copilot` directly uses the seat's GitHub-hosted models. There
is no proxy, so tokens, gateway cost, and session replay are not
possible. The same onboard writes
`~/.copilot/hooks/preloop.json` for `sessionStart`, `sessionEnd`,
`subagentStart`, `subagentStop`, and `agentStop`. Ingest source is
`copilot_cli`. Payloads carry a session id and a transcript path, not
token counts or a billed amount, so those fields are omitted
(`cli/internal/cmd/usage_hook_copilot.go`). `--store-transcript` is
Cursor only (`cli/internal/cmd/usage_hook.go`), so this session is not
a replay of the model call.

Dollars for the seat's premium requests come from the import, not from
the hook. That import is per user and day, so this row has no
ticket-level dollars, and a missing day is unknown rather than zero.
Publication and feedback are not this surface: a person drives the
session.

### Copilot CLI, GitHub-hosted models, flow on a private runner

A flow can run `copilot` on a private runner under the OS user that
runs `preloop runner fg`. Setup, profile fields, and named errors:
[Copilot CLI](copilot-cli.md#run-copilot-cli-from-flows-private-runner-host-profile)
and [host execution profiles](runners/quickstart-linux.md#copilot-cli-profiles).
Issue [#956](https://github.com/preloop/preloop/issues/956) is closed.
The runner strips `COPILOT_PROVIDER_*` so the run cannot silently become
BYOK (`cli/internal/cmd/runner_host_exec_copilot.go`).

MCP for this run comes from two separate sources, and they are governed
differently.

- **The flow's own MCP server, supplied by the runner.** When the flow
  lists allowed MCP tools or servers (`flow_uses_mcp`,
  `backend/preloop/services/host_exec_delivery.py`), the control plane
  mints a short-lived token scoped to that one execution
  (`create_flow_runtime_token`,
  `backend/preloop/services/flow_runtime_token.py`: `mcp:read` and
  `mcp:write` scopes, the flow's allowed tools and servers in the token
  context, a two-hour expiry). The lease carries the token without
  persisting it. The runner writes a per-job configuration file in the
  run directory (`.preloop/copilot-mcp-config.json`, mode `0600`),
  points Copilot at it with the managed flag
  `--additional-mcp-config=@<file>`, and adds
  `--allow-tool=preloop-flow` unless the profile set `allow_all_tools`,
  because `-p` mode cannot prompt and the server is already filtered to
  the flow's tools (`hostExecMCPConfig`,
  `cli/internal/cmd/runner_host_exec_flow.go`; `buildCopilotHostExecArgs`,
  `cli/internal/cmd/runner_host_exec_copilot.go`). Profile `deny_tools`
  rules still apply. The token never enters argv or the CLI environment.
  The runner removes the file when the job ends, and the control plane
  revokes the token when the execution completes
  (`revoke_flow_runtime_tokens`). So a flow that configures MCP does get
  flow MCP on this path, with a credential scoped to one run.
- **The runner user's own MCP file.** `~/.copilot/mcp-config.json` (or
  `$COPILOT_HOME`), written by `preloop agents onboard "Copilot CLI"`,
  is still read for the run and its servers stay available. That file is
  the operator's local configuration. Preloop governs those tool calls
  only where the servers point at Preloop's `/mcp/v1`; an unrelated local
  MCP server the operator added is their own tool, and this page does not
  promise Preloop governance for it.

Those are different behaviors, and one is not a consequence of the
other. The operator still cannot replace the managed server:
`--additional-mcp-config` is in `copilotManagedFlags`
(`cli/internal/cmd/runner_host_exec_copilot.go`), so putting it in
profile `argv` is refused when the profile is loaded, alongside
`--model`, `--resume`, `--allow-all-tools` and the other runner-owned
flags. That refusal is about operator-supplied argv. The runner supplies
the same flag itself for every job that carries a flow MCP token.

Profile `allow_tools` and `deny_tools` are Copilot permission rules for
native tools, which is a separate mechanism from MCP governance.
`allow_all_tools` is refused unless
`preloop agents onboard "Copilot CLI" --approvals` has installed the
`preToolUse` approval hook. That hook gates native tool calls; it is not
MCP governance, and MCP governance is not native-tool approval.

Model calls are not gateway metered. The server forces
`gateway_metered: false`
(`backend/preloop/services/host_exec.py`). The execution page shows
"Not gateway metered". The result can include the Copilot session id,
the model Copilot reported, and `premium_requests`. That count is not
a token ledger and not a dollar amount.

Sessions: before each run the runner upserts the Preloop usage hooks
in `~/.copilot/hooks/preloop.json` (same lifecycle events as onboard).
Other hook files are left alone. Sessions, hooks and a premium-request
count are execution bookkeeping. They are not token, cost or replay
parity with a gateway path, which captures per-request `ApiUsage` rows.

Copilot plan terms govern how a seat may be used. A developer running
flows on their own machine with their own seat is ordinary use. Check
the organization's Copilot terms before sharing one seat across
automated flows for several people. The launcher guide states that
limit; this page does not restate the terms.

#### What this path does not do today

- **Publication is opt-in and narrow.** A flow whose `git_clone_config`
  sets `create_pull_request` runs only on a Copilot profile that sets
  `allow_checkout` and `allow_publish` (the runner advertises
  `host_publication`), for exactly one repository in legacy mode. The
  runner commits and pushes the managed branch; the control plane opens
  and binds the Bitbucket Cloud pull request. See
  [Copilot CLI host profiles](copilot-cli.md#review-and-implementation-flows).
- **Other publication and feedback are refused, not queued silently.**
  Cursor publication, more than one repository, a flow in
  isolated publication mode, a flow with remote custom commands or clone
  `setup_commands`, and a flow asking for native CLI session resume are
  all rejected before the run, by `host_exec_unavailable_reason`
  (`backend/preloop/services/host_exec.py`) and
  `jobRejectedHostExecInjection`
  (`cli/internal/cmd/runner_host_exec.go`). Each refusal names the
  capability the path does not have yet. Nothing on this page describes
  them as shipped.
- **Review and checkout are supported, and that is the whole scope.**
  With `allow_checkout`, the runner clones the flow's repositories and
  the reviewer reads the diff and posts its review through the flow's
  MCP tools. Without `allow_publish` the run does not push branches or
  open pull requests.
- [#1069](https://github.com/preloop/preloop/issues/1069) tracks feedback
  continuation and publication retry for host flows. It is open.
  When it ships, that issue updates this matrix for the version, the
  capability and the opt-in that actually apply; nothing proposed there
  is claimed here.

#### Dollars on this path

There is no ticket-level dollar figure for this surface, and none is
estimated. The execution row carries a count Copilot reported and the
"Not gateway metered" marker. The
[premium-request import](copilot-usage-import.md) carries per user, per
day and per model figures GitHub billed, plus a seat count. Neither
ledger is joined to a ticket, an execution or a session, and neither
prices a run. A seat with no import connected, or a day GitHub has not
settled yet, is unknown: it is not a zero. The Cost page shows "no data
imported" for a range with nothing imported, and the execution page
shows the count and the "Not gateway metered" marker instead of an
estimate.

Implementation evidence, not a live run: `TestCopilotHostExecGetsFlowMCPServer`
and `TestHostExecRejectsInjectedPreambleMarkerAndBadMCPToken` in
`cli/internal/cmd/runner_host_exec_flow_test.go` cover the per-job file,
the managed flag, the token's absence from argv and the cleanup;
`TestNormalizeCopilotHostExecProfile` in
`cli/internal/cmd/runner_host_exec_copilot_test.go` covers the managed
flag refusal.

### Copilot CLI BYOK (`preloop copilot`)

[`preloop copilot`](copilot-cli.md) sets `COPILOT_PROVIDER_*` and
`COPILOT_MODEL` at the gateway and execs `copilot`. A missing binary,
credential, or model alias exits without launching, so the process
cannot fall through to GitHub-hosted models
(`cli/internal/cmd/copilot.go`).

Model calls are gateway usage: tokens, cost, and session replay from
captured `ApiUsage` rows. MCP onboarding is a separate step (the same
`~/.copilot/mcp-config.json` and, with `--approvals`, the same
`preToolUse` hook). Hooks still record lifecycle only. They do not
replace the gateway ledger.

Spend for this path is the gateway. It is not the premium-request
import. Gateway rows carry the session and, in a flow, the execution, so
this is the only Copilot row with per-request cost. The local launcher
still attributes to the session, not to a ticket, and it pushes nothing:
publication and feedback are the flow harness's job, not this
command's.

### Copilot cloud coding agent on GitHub.com

Repository admins paste an MCP config under the repository's Copilot
settings. Steps and the firewall allow list:
[Copilot cloud agent](copilot-cloud-agent.md). Copilot calls the listed
tools without asking on GitHub. Preloop still applies tool policy on
`/mcp/v1`. The same config is shared with Copilot code review, which
only calls tools whose `tools/list` entries set
`annotations.readOnlyHint` to true.

Model calls stay on GitHub-hosted models. This path does not install
Copilot CLI and does not route the model through the gateway. Metering
tokens, cost, and session replay is not possible: no proxy for
GitHub-hosted models.

The cloud agent does have a hook surface (`.github/hooks/*.json`,
GitHub hooks page, read 2026-09-27). Preloop does not install those
hooks. There is no open issue for writing them. An `http` hook back to
Preloop would also need the Preloop host on the firewall allow list,
and files a hook writes inside the sandbox are discarded when the job
ends ([cloud agent guide](copilot-cloud-agent.md)).

Spend is not a gateway row. The premium-request import may show
GitHub's billed `netAmount` for that user and day. It is not a session.
Usage-metrics reports do not include Copilot Chat on GitHub.com or
GitHub Mobile (GitHub usage-metrics concepts, read 2026-09-27). They do
include IDE, Copilot CLI, and agent-app telemetry. They are not a bill.

Because both are per user and day, no cloud agent job is matched to a
figure, and this row has no ticket-level dollars. The agent does post
comments and open pull requests on GitHub in its own right; that is
GitHub activity that Preloop neither performs nor records, and it is not
Preloop publication or feedback continuation.

### Copilot inline completions

Inline completions do not call MCP tools, so tool governance does not
apply. They use GitHub-hosted models. There is no proxy, and there is
no hook surface for a completion, so tokens, cost, session replay, and
hook sessions are not possible.

What remains is the premium-request import: daily aggregates and
usage-metrics counters (acceptance and code generation among them), not
one completion. Those are per user and per day, so they never price an
individual completion, and a day with no row is unknown rather than
zero. A completion has nothing to publish and nothing to continue.

## Recommended setups

1. **Keep Copilot.** Leave developers on GitHub-hosted models. Add
   Preloop for tool governance (`preloop agents onboard` for
   "VSCode / Copilot" and "Copilot CLI", with `--approvals` on the CLI
   when native shell and file tools should hit policy). Connect the
   [premium-request import](copilot-usage-import.md) so seats and
   premium-request spend show on the Cost page, marked not metered by
   the gateway. Run flows on Copilot CLI with a private-runner host
   profile when the work should use that runner user's seat; the flow's
   own MCP tools are governed there through a per-job server. This setup
   does not meter model calls live, does not attribute dollars to a
   ticket, and publishes from a host run only through the opt-in managed
   Bitbucket Cloud path.

2. **BYOK overflow through the gateway.** When the team has provider
   keys and wants tokens, cost, and session replay, start Copilot CLI
   with `preloop copilot` and a gateway model alias. Model traffic is
   gateway usage. MCP and approval hooks are still the onboard step.
   Do not read those calls out of the premium-request import. VS Code
   Chat BYOK is a separate manual path:
   [VS Code Copilot Chat](clients/vscode-copilot.md). Onboarding leaves
   `chatLanguageModels.json` alone. The gateway request shape is covered
   by a regression test. An interactive VS Code session is the founder
   checklist ([#787](https://github.com/preloop/preloop/issues/787)).

## External references

Read 2026-09-27:

- [About hooks](https://docs.github.com/en/copilot/concepts/agents/hooks):
  hooks are listed for the Copilot cloud agent and Copilot CLI.
- [Customization cheat sheet](https://docs.github.com/en/copilot/reference/customization-cheat-sheet):
  VS Code hooks are marked preview (P). Copilot CLI and GitHub.com
  hooks are marked supported.
- [Copilot usage metrics](https://docs.github.com/en/copilot/concepts/copilot-usage-metrics/copilot-metrics):
  reports cover IDE, Copilot CLI, and agent apps. They do not include
  Copilot Chat on GitHub.com or GitHub Mobile. They are not a bill.
  Data for a day is available within two full UTC days.

Reused from [#788](https://github.com/preloop/preloop/issues/788),
read 2026-09-26. The import routes and what is stored are in
[Copilot usage import](copilot-usage-import.md). This page does not
add claims beyond that guide and the concepts page above.

- [REST: Copilot usage metrics](https://docs.github.com/en/rest/copilot/copilot-usage-metrics)
- [REST: Copilot user management](https://docs.github.com/en/rest/copilot/copilot-user-management)
- [REST: billing usage](https://docs.github.com/en/rest/billing/usage)
- [REST: enterprise billing usage](https://docs.github.com/en/enterprise-cloud@latest/rest/billing/usage)

The host-exec command shape is verified in
`cli/internal/cmd/runner_host_exec_copilot.go` against Copilot CLI
1.0.88 and
[the CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
(cited from that file for [#956](https://github.com/preloop/preloop/issues/956)).
The operator steps are in the [Copilot CLI guide](copilot-cli.md).

The flow-scoped MCP server for a host run is written by
`hostExecMCPConfig`
(`cli/internal/cmd/runner_host_exec_flow.go`), minted by
`create_flow_runtime_token` and revoked by `revoke_flow_runtime_tokens`
(`backend/preloop/services/flow_runtime_token.py`), and the flags a
profile may not override are `copilotManagedFlags`
(`cli/internal/cmd/runner_host_exec_copilot.go`). The shared host rules,
including the rejected publication and resume paths, are in
[host execution profiles](runners/quickstart-linux.md#host-execution-profiles-opt-in-private-only).

The cloud agent MCP click path, including the GitHub how-to URL, is in
[Copilot cloud agent](copilot-cloud-agent.md).

## Related

- [VS Code Copilot Chat manual BYOK](clients/vscode-copilot.md)
- [Copilot CLI through the gateway, and private-runner host profiles](copilot-cli.md)
- [Copilot cloud agent MCP](copilot-cloud-agent.md)
- [Usage hooks](usage-hooks.md) (Copilot CLI section)
- [Premium-request import](copilot-usage-import.md)
- [Host execution profiles on a private runner](runners/quickstart-linux.md#host-execution-profiles-opt-in-private-only)
