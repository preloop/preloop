# Copilot coverage

What Preloop governs and meters on each GitHub Copilot surface. The
longer guides stay the setup steps. This page is the matrix.

Rows are surfaces. Columns are:

- **MCP tool calls governed:** policies, approvals, and audit for tool
  calls that go through Preloop.
- **Model calls metered:** tokens, cost, and session replay for the
  model request itself.
- **Sessions recorded:** a runtime session from a Copilot hook.
- **Spend visible:** gateway usage, or the premium-request import.
- **Status today:** shipped, planned (with an issue), or not possible
  (with the reason).

Session replay is the console timeline of gateway `ApiUsage` rows
(`docs/architecture/gateway.md`, Current Explorer Surface). A session
with no captured gateway requests stays replay-ineligible. Hook ingest
can still open a runtime session without that timeline.

GitHub-hosted model traffic never passes through the Preloop gateway.
There is no proxy for those models. The [premium-request import](copilot-usage-import.md)
stores what GitHub reports (seats, daily premium-request `netAmount`,
usage-metrics counters). It is not per request, and it is not a
transcript.

## Matrix

| Surface | MCP tool calls governed | Model calls metered | Sessions recorded (hooks) | Spend visible | Status today |
| --- | --- | --- | --- | --- | --- |
| VS Code Copilot Chat, GitHub-hosted models | Yes, for MCP servers Preloop writes | Not possible: no proxy for GitHub-hosted models | Not possible: no hook surface in VS Code Chat | Premium-request import | MCP shipped. Metering and hooks not possible |
| VS Code Copilot Chat, BYOK (custom endpoint) | Same MCP path as the row above | Planned ([#787](https://github.com/preloop/preloop/issues/787)) | Not possible: no hook surface in VS Code Chat | Gateway, only if #787 lands. Import stays GitHub-reported | Planned ([#787](https://github.com/preloop/preloop/issues/787)) |
| Copilot CLI, GitHub-hosted, interactive on a laptop | Yes, after onboard. Native tools need `--approvals` | Not possible: no proxy for GitHub-hosted models | Yes, lifecycle only | Premium-request import | Shipped |
| Copilot CLI, GitHub-hosted, flow on a private-runner host profile | Only the runner user's own MCP file. Flow MCP settings do not apply | Not possible: no proxy for GitHub-hosted models | Yes. The runner installs usage hooks before the run | Execution: not gateway metered, plus a premium-request count. Dollars: import | Shipped ([#956](https://github.com/preloop/preloop/issues/956)) |
| Copilot CLI BYOK through the gateway (`preloop copilot`) | Same CLI MCP and approval hooks | Yes: tokens, cost, and session replay | Yes, when the CLI hooks are installed | Gateway. Not the premium-request import | Shipped |
| Copilot cloud coding agent on GitHub.com | Yes. Preloop policy on `/mcp/v1` | Not possible: no proxy for GitHub-hosted models | Not installed. Preloop does not write `.github/hooks` | Import is daily, not a session. Usage metrics exclude Copilot Chat on GitHub.com | MCP shipped. Hooks not installed |
| Copilot inline completions | Not applicable. A completion is not an MCP tool call | Not possible: no proxy for GitHub-hosted models | Not possible: no hook surface for completions | Premium-request import, as daily aggregates only | Not possible for live governance or metering |

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

Spend for this row is the [premium-request import](copilot-usage-import.md):
daily, marked not metered by the gateway
(`backend/preloop/services/copilot_usage_import.py`).

### VS Code Copilot Chat, BYOK (custom endpoint)

MCP governance is the same user file as the GitHub-hosted row. Onboarding
still does not rewrite a model endpoint.

[#787](https://github.com/preloop/preloop/issues/787) is open. It asks
whether Copilot Chat's custom OpenAI-compatible endpoint can point at
the gateway, and which plans expose that control. This page does not
claim that path works, and it does not name a plan. If it lands, those
calls are gateway usage (tokens, cost, session replay). Until then,
model metering is planned (#787).

The hook cell is the same as the GitHub-hosted row: no hook surface in
VS Code Chat.

Gateway spend and the import are different ledgers. The import stores
GitHub's reported figures only. A call that never reached GitHub is not
in that import. [#788](https://github.com/preloop/preloop/issues/788)
says not to double-count BYOK traffic that does go through the gateway.

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
the hook.

### Copilot CLI, GitHub-hosted models, flow on a private runner

A flow can run `copilot` on a private runner under the OS user that
runs `preloop runner fg`. Setup, profile fields, and named errors:
[Copilot CLI](copilot-cli.md#run-copilot-cli-from-flows-private-runner-host-profile)
and [host execution profiles](runners/quickstart-linux.md#copilot-cli-profiles).
Issue [#956](https://github.com/preloop/preloop/issues/956) is closed.
The runner strips `COPILOT_PROVIDER_*` so the run cannot silently become
BYOK (`cli/internal/cmd/runner_host_exec_copilot.go`).

MCP: the run uses the runner user's `~/.copilot/mcp-config.json`. Flow
MCP settings do not apply. `--additional-mcp-config` is a flag the
runner refuses. If that user has onboarded Copilot CLI, those MCP tool
calls are governed. If not, Preloop does not write an MCP file for the
job. Profile `allow_tools` and `deny_tools` are Copilot permission
rules. `allow_all_tools` is refused unless
`preloop agents onboard "Copilot CLI" --approvals` has installed the
approval hook.

Model calls are not gateway metered. The server forces
`gateway_metered: false`
(`backend/preloop/services/host_exec.py`). The execution page shows
"Not gateway metered". The result can include the Copilot session id,
the model Copilot reported, and `premium_requests`. That count is not
a token ledger and not a dollar amount.

Sessions: before each run the runner upserts the Preloop usage hooks
in `~/.copilot/hooks/preloop.json` (same lifecycle events as onboard).
Other hook files are left alone.

Dollars still come from the premium-request import, per user and day,
not from the execution row.

Copilot plan terms govern how a seat may be used. A developer running
flows on their own machine with their own seat is ordinary use. Check
the organization's Copilot terms before sharing one seat across
automated flows for several people. The launcher guide states that
limit; this page does not restate the terms.

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
import.

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

### Copilot inline completions

Inline completions do not call MCP tools, so tool governance does not
apply. They use GitHub-hosted models. There is no proxy, and there is
no hook surface for a completion, so tokens, cost, session replay, and
hook sessions are not possible.

What remains is the premium-request import: daily aggregates and
usage-metrics counters (acceptance and code generation among them), not
one completion.

## Recommended setups

1. **Keep Copilot.** Leave developers on GitHub-hosted models. Add
   Preloop for tool governance (`preloop agents onboard` for
   "VSCode / Copilot" and "Copilot CLI", with `--approvals` on the CLI
   when native shell and file tools should hit policy). Connect the
   [premium-request import](copilot-usage-import.md) so seats and
   premium-request spend show on the Cost page, marked not metered by
   the gateway. Run flows on Copilot CLI with a private-runner host
   profile when the work should use that runner user's seat. This setup
   does not meter model calls live.

2. **BYOK overflow through the gateway.** When the team has provider
   keys and wants tokens, cost, and session replay, start Copilot CLI
   with `preloop copilot` and a gateway model alias. Model traffic is
   gateway usage. MCP and approval hooks are still the onboard step.
   Do not read those calls out of the premium-request import. VS Code
   Chat BYOK is not this setup. It is [#787](https://github.com/preloop/preloop/issues/787)
   and is not shipped.

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

The cloud agent MCP click path, including the GitHub how-to URL, is in
[Copilot cloud agent](copilot-cloud-agent.md).

## Related

- [Copilot CLI through the gateway, and private-runner host profiles](copilot-cli.md)
- [Copilot cloud agent MCP](copilot-cloud-agent.md)
- [Usage hooks](usage-hooks.md) (Copilot CLI section)
- [Premium-request import](copilot-usage-import.md)
