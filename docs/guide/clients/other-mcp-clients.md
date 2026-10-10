# Cursor, Claude Desktop & Other MCP Clients

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After this page you know exactly what Preloop can and cannot govern for each MCP-capable client the CLI discovers, and how to wire the ones that need manual steps.

Any client that speaks MCP over streamable HTTP can point at Preloop's endpoint with an API key:

```text
URL:    https://YOUR_PRELOOP_URL/mcp/v1
Header: Authorization: Bearer YOUR_API_KEY
```

That gives you the MCP firewall (access rules, approvals, audit) for every tool call the client makes through Preloop. What differs per client is **model traffic**: only some clients let Preloop rewrite their model configuration so spend flows through the [gateway](../concepts/model-gateway.md).

## Support matrix

| Client | Tool governance (MCP) | Model routing through the gateway |
|--------|----------------------|-----------------------------------|
| [Claude Code](claude-code.md) | Automatic | Automatic |
| [Codex CLI](../codex-cli.md) | Automatic | Automatic |
| [Gemini CLI](gemini-cli.md) | Automatic | Automatic |
| [OpenCode](opencode.md) | Automatic | Automatic |
| [Hermes](../hermes.md) | Automatic | Automatic |
| [OpenClaw](../integrations/openclaw.md) | Automatic | Automatic (OpenAI-compatible gateway) |
| Cursor | Automatic | **Manual BYOK**: AI panel incl. Agent mode (see below) |
| [Claude Desktop](claude-desktop.md) | Automatic | **Managed config**: `--model-route direct` or `apps-gateway` prints the MDM configuration |
| Windsurf | Automatic | Not available |
| VS Code / Copilot | Automatic | **Manual BYOK**: Chat: Manage Language Models -> Add Models -> Custom Endpoint (see below) |
| Copilot CLI | Automatic (`~/.copilot/mcp-config.json`) | Not rewritten on onboard. `preloop copilot` sets the gateway env vars |
| Antigravity | Automatic | Not available: locked to Google-hosted models, no custom base URL |
| Devin | Automatic | Not available: inference runs in Cognition's cloud |

"Automatic" means `preloop agents discover` / `preloop agents onboard <agent>` handles it. Clients with no model routing keep using their own provider credentials: tool calls are governed and audited, and their model spend is not metered live, so model budgets do not apply to them. Manual BYOK meters the calls aimed at the gateway and leaves the client's own hosted models alone. Spend that is not metered live can still be brought into Cost analytics after the fact by importing it, see [Importing usage from Cursor](../cost/importing-cursor-usage.md).

## Cursor

Onboarding configures the MCP firewall automatically. Model routing is a manual BYOK step because Cursor only accepts a custom model base URL through its in-app **Settings → Models** (global, no config-file hook), so Preloop cannot rewrite it for you. Once set, the override covers more than chat: Cursor's AI panel routes third-party model calls through the gateway in Ask/Plan mode **and** in Agent mode.

Cursor's **bundled models** (Composer, Auto) are a separate case: they are billed and served by Cursor and cannot be pointed at any custom endpoint, so no configuration will ever route them through the gateway. That spend is not invisible, though. Export it from the Cursor dashboard and import it:

```bash
preloop agents onboard cursor        # once, so imports have an agent to attribute to
preloop usage import cursor-usage.csv
```

Imported records show up in Cost analytics as their own block, labeled as imported and kept out of gateway budgets, so metered and imported spend stay separately auditable. Re-importing the same export is safe. See [Importing usage from Cursor](../cost/importing-cursor-usage.md) for the full walkthrough, including the API endpoints and custom column mapping.

Cursor is also one of the clients that supports the native tool-approval hook: `preloop agents onboard cursor --approvals` routes tool calls that would have prompted you locally to Preloop approvals instead.

### Route Cursor model traffic through Preloop

Cursor's AI panel, in Ask/Plan mode and Agent mode, can send its model calls through the Preloop gateway. Tab autocomplete, inline edit (Cmd/Ctrl+K), and Cursor's own bundled models (Composer, Grok, Auto) always use Cursor's backend and cannot be routed through any external gateway, ours or anyone else's.

Prerequisites:

- A Preloop API key. Mint one scoped to Cursor: subject-scoped allowed-model lists apply to gateway calls made with it.
- Your Preloop gateway URL must be reachable over public HTTPS. Cursor relays BYOK requests from its own servers, so a localhost or LAN-only Preloop cannot receive them; Cursor blocks localhost base URLs outright.

Steps:

1. Open Cursor Settings, then go to **Models**.
2. In the **OpenAI API Key** section, paste your Preloop API key and enable the key toggle.
3. Enable **Override OpenAI Base URL** and enter `https://YOUR_PRELOOP_URL/openai/v1`.
4. Pick a third-party model in the model picker (for example `gpt-5.2` or `claude-sonnet-4-5`), or use **Add Custom Model** with the exact model alias configured on your Preloop AI models.
5. Test: open the AI panel (Cmd/Ctrl+L), send a prompt, then check **Cost** and **Audit > Sessions** in the Preloop console for the call.

What to expect:

- Requests arrive at `POST /openai/v1/chat/completions` with your Preloop key as a bearer token. Budgets, allowed-model lists, usage recording, and replay all apply.
- Claude and Gemini models work through this same path. Cursor's Anthropic key slot has no base-URL override (it always targets Anthropic directly), but Preloop's OpenAI-compatible endpoint resolves any gateway-enabled account model by alias, so add your Claude alias as a custom model and serve it through `/openai/v1`.
- Selecting Composer, Grok, or another Cursor-billed bundled model while the override is enabled fails with "This model does not support custom API keys". Switching to Auto works but silently bypasses the override; bundled-model spend never reaches Preloop, for any gateway vendor.
- Privacy note: Cursor still relays these requests through its own backend for prompt assembly, and Cursor's Zero Data Retention does not apply to custom-key traffic.
- On Cursor Teams and Enterprise plans, BYOK requests additionally consume Cursor's Token Rate ($0.25 per million tokens) on the Cursor side; that residue does not appear in Preloop Cost analytics. Enterprise admins can also restrict personal API keys entirely, which blocks this override pattern on managed teams.

## VS Code / Copilot Chat

Onboarding writes `~/.vscode/mcp.json` and leaves model routing alone. Copilot Chat's Custom Endpoint is the manual BYOK path, the same class as Cursor's Settings -> Models override. There is no config file `preloop agents onboard` can rewrite: `github.copilot.chat.customOAIModels` is deprecated.

Click path: **Chat: Manage Language Models** -> **Add Models** -> **Custom Endpoint** (`vendor` `customendpoint`), then `chatLanguageModels.json`.

Set `toolCalling` to `true`. Point each model's `url` at the full gateway path:

- Chat Completions: `https://YOUR_PRELOOP_URL/openai/v1/chat/completions`
- Responses: `https://YOUR_PRELOOP_URL/openai/v1/responses`
- Messages: `https://YOUR_PRELOOP_URL/anthropic/v1/messages`

The `apiKey` is the Preloop agent credential. A URL with no API path gets `/v1/<api>` appended, so use the full path. Chat and Agent mode (and utility tasks you point at the gateway) are the metered surfaces. Inline completions, semantic search, and embeddings stay on GitHub.

Plan entitlement, checked 2026-10-10 against the [VS Code language models doc](https://code.visualstudio.com/docs/agent-customization/language-models), the [2026-06-18 BYOK post](https://code.visualstudio.com/blogs/2026/06/18/byok-vscode), [GitHub's bring-your-own-key page](https://docs.github.com/en/copilot/concepts/models/bring-your-own-key), and [Changing the AI model for GitHub Copilot Chat](https://docs.github.com/en/copilot/how-tos/use-ai-models/change-the-chat-model): BYOK works with no Copilot plan. Those pages do not name Copilot Free, Pro, and Pro+ as separate rows. Copilot Business and Copilot Enterprise need an admin policy (**Bring Your Own Language Model Key in VS Code** on the VS Code page, **Bring Your Own Language Model Key in Select IDEs** on the GitHub chat-model page). A licensed Business or Enterprise seat was not checked. The sanitized file, the six-step founder checklist, and the console Cost and Models checks are in [VS Code Copilot Chat](vscode-copilot.md).

## Copilot CLI

`preloop agents onboard "Copilot CLI"` backs up `~/.copilot/mcp-config.json` and adds the Preloop MCP server (`/mcp/v1`). The CLI does not read VS Code's `.vscode/mcp.json`. Onboarding does not rewrite model traffic. `preloop copilot` starts the `copilot` binary with `COPILOT_PROVIDER_BASE_URL` pointed at the Preloop gateway. IDE Copilot chat and inline completions are a different client (VS Code / Copilot, above) and are not covered by that launcher.

## Claude Desktop

Discovered and onboarded for MCP governance. Model routing uses Claude Desktop's third-party gateway mode: `preloop agents onboard "Claude Desktop" --model-route direct` (or `--model-route apps-gateway` behind a Claude apps gateway) prints the managed configuration for macOS, Windows and Linux, which your MDM deploys. See [Claude Desktop](claude-desktop.md).

## Everything else

If your client is MCP-capable but not in the table, add the endpoint manually (URL + bearer header as above) and it gets the same tool governance. Check `preloop agents discover --json` first: the discovery list grows release by release.

## Related

- [Importing usage from Cursor](../cost/importing-cursor-usage.md): get bundled-model spend into Cost analytics
- [VS Code Copilot Chat](vscode-copilot.md): Custom Endpoint click path and founder checklist
- [CLI Reference: support levels](../cli.md#support-levels)
- [Connect Your MCP Client](../getting-started/connect-mcp-client.md): generic setup steps
- [Subject-Scoped Governance](../concepts/subject-scoped-governance.md): scoping tools/models per client key
