# Copilot cloud agent: Preloop MCP on GitHub.com

Repository admins can give the Copilot cloud agent on GitHub.com access
to Preloop tools by pasting an MCP config into the repository's Copilot
MCP settings. There is no local config for `preloop agents discover` to
rewrite. This page covers that GitHub.com path only. It does not cover
inline completions or IDE Copilot chat.

Configure MCP servers under **Settings → Copilot → MCP servers**. Official
steps:
https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers

## MCP configuration

Store a Preloop API token as an Agents secret named
`COPILOT_MCP_PRELOOP_TOKEN` (names must start with `COPILOT_MCP_`). Do not
put the token in the JSON. GitHub substitutes `$COPILOT_MCP_*` references
in header values.

Replace the URL host with your Preloop instance (`PRELOOP_URL`):

```json
{
  "mcpServers": {
    "preloop": {
      "type": "http",
      "url": "https://preloop.example.com/mcp/v1",
      "headers": {
        "Authorization": "Bearer $COPILOT_MCP_PRELOOP_TOKEN"
      },
      "tools": ["*"]
    }
  }
}
```

Prefer a tighter `tools` allowlist once you know which Preloop tools the
agent should call. Copilot calls the tools you list without asking for
approval on GitHub. Preloop still applies its own tool policy on
`/mcp/v1`.

The same repository MCP configuration, including the Agents secret, is
shared with Copilot code review. Code review only calls tools whose
`tools/list` entries set `annotations.readOnlyHint` to true. Turn that
off under **Settings → Copilot → Code review** if review sessions should
not see these tools.

## Firewall allow list

The cloud agent sandbox only reaches hosts on the Copilot firewall allow
list. Add the Preloop hostname (the host in `PRELOOP_URL`) or the agent
cannot open `{PRELOOP_URL}/mcp/v1`.

## Model traffic

Model calls from the cloud coding agent stay on GitHub-hosted models.
This page does not route them through the Preloop gateway, and it does
not install a local Copilot CLI.

## Hooks

`.github/hooks/*.json` can emit session lifecycle events from the cloud
agent (see GitHub's hooks reference). Preloop does not install those
hooks. An `http` hook back to Preloop also needs the Preloop host on the
firewall allow list. Files a hook writes inside the sandbox are discarded
when the job ends.
