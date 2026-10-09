# Claude Desktop

Editions: OSS, Cloud, Enterprise. Everything on this page ships in OSS.

After this page you can route Claude Desktop model traffic (Chat, Cowork and Code tabs) through the Preloop model gateway, either directly or behind a Claude apps gateway, and you know what Preloop sees and does not see on each route.

Claude Desktop in third-party mode reads its inference settings from the operating system's managed configuration. Preloop generates that configuration for you; your MDM (Jamf, Intune, Kandji, Fleet, or a root-owned file on Linux) deploys it. The CLI never writes managed configuration itself: those locations are admin-owned, and a wrong file there can disable Desktop's local settings entirely.

Tool governance is separate and unchanged: `preloop agents onboard "Claude Desktop"` (without `--model-route`) adds the managed MCP bridge, or you add Preloop as a custom connector. See [Cursor, Claude Desktop and other MCP clients](other-mcp-clients.md).

## Pick a route

| Route | Use it when | Identity at Preloop | What you run |
|-------|-------------|---------------------|--------------|
| **Direct** (`--model-route direct`) | Any Desktop fleet, including Preloop Cloud with no gateway of your own | The signed-in user's Preloop API key, minted by the CLI credential helper | Nothing beyond MDM |
| **Apps gateway** (`--model-route apps-gateway`) | You already run, or want, the Claude apps gateway for SSO, RBAC and policy | The developer's IdP identity, forwarded by the gateway on a trusted upstream key | `claude gateway` on your private network |

## Direct route

```bash
preloop agents onboard "Claude Desktop" --model-route direct            # prints macOS, Windows and Linux config
preloop agents onboard "Claude Desktop" --model-route direct --os macos --out ./desktop-config
```

The generated configuration sets:

| Key | Value |
|-----|-------|
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `https://YOUR_PRELOOP_URL/anthropic` |
| `inferenceGatewayAuthScheme` | `x-api-key` |
| `inferenceCustomHeaders` | `{"X-Preloop-Client":"claude-desktop"}` (attribution only, no credentials) |
| `inferenceCredentialKind` | `helper-script` |
| `inferenceCredentialHelper` | absolute path of the `preloop` executable on the device |
| `inferenceCredentialHelperArgs` | `["auth","gateway-credential","--client","claude-desktop"]` |
| `chatTabEnabled` | `true`, only with `--chat-tab` |

Per OS:

- **macOS**: a `com.anthropic.claudefordesktop.plist` and a `.mobileconfig` payload snippet (`PayloadType` `com.anthropic.claudefordesktop`). Your MDM installs it at `/Library/Managed Preferences/<user>/com.anthropic.claudefordesktop.plist`. Give the payload a fresh `PayloadUUID`.
- **Windows**: a `.reg` file with `REG_SZ` values directly under `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude` (Desktop never reads subkeys). Machine policy wins over `HKCU` when both exist.
- **Linux**: `managed-settings.json` for `/etc/claude-desktop/managed-settings.json`. The file and the directory must be owned by root and not group or world writable, or Desktop rejects the whole file.

Object and array values (`inferenceCustomHeaders`, `inferenceCredentialHelperArgs`) are JSON strings in the plist and the registry, and native JSON in the Linux file, as Desktop expects.

Pass `--helper-path` when `preloop` is installed somewhere other than the default on managed devices (the current executable on this OS, `/usr/local/bin/preloop` elsewhere, `C:\Program Files\Preloop\preloop.exe` on Windows).

### The credential helper

```bash
preloop auth gateway-credential --client claude-desktop
```

Desktop runs this command, reads stdout and sends the result as `x-api-key` to Preloop. It prints one bare token and nothing else; diagnostics go to stderr. On first use it mints a Preloop API key for the signed-in CLI user and caches it in `~/.preloop/gateway-credentials/claude-desktop.json` (mode 0600). It checks that the cached key still exists on each run and mints a new one when the key was revoked. When the user is not signed in it exits non-zero with an empty stdout, so Desktop shows its credential error instead of sending a bad key. Each user runs `preloop login` once on the device.

Gateway calls made with this key are attributed to that user: user budgets, allowed-model lists, usage, sessions and audit apply as for any other Preloop key. Usage rows record `client: claude_desktop` from the `X-Preloop-Client` header.

## Apps gateway route

```bash
preloop agents onboard "Claude Desktop" --model-route apps-gateway \
  --gateway-url https://claude-gateway.internal.example.com --out ./gateway-config
```

This creates a Preloop API key with the scope `model_gateway:trusted_upstream` (account admin only) and a random upstream secret. Preloop stores only the secret's sha256. With `--key-id <id>` it reuses an existing trusted key and generates no new secrets.

The secrets (`PRELOOP_UPSTREAM_KEY`, `PRELOOP_UPSTREAM_SECRET`) are shown once: in `preloop-upstream.env` (mode 0600) when you pass `--out`, otherwise on the terminal. Put them in the gateway's environment, not its config file.

The command also prints:

- the gateway `upstreams:` entry:

  ```yaml
  upstreams:
    - provider: anthropic
      base_url: https://YOUR_PRELOOP_URL/anthropic
      auth:
        api_key: ${PRELOOP_UPSTREAM_KEY}
      forward_user_identity: true
      headers:
        x-preloop-upstream-secret: ${PRELOOP_UPSTREAM_SECRET}
  ```

- the policy opt-in `desktop: {}` on the matching policy,
- the Desktop managed configuration with `bootstrapUrl: <gateway public_url>/user/bootstrap` (and `chatTabEnabled` with `--chat-tab`),
- the Claude Code managed settings for CLI fleets: `forceLoginMethod: gateway`, `forceLoginGatewayUrl`, `parentSettingsBehavior: merge`.

Minimum versions of Claude Code on the gateway server: v2.1.233 for `forward_user_identity`, v2.1.267 for relaying a per-user 429 instead of failing over, v2.1.277 for upstream `headers:`, v2.1.203 for the Desktop bootstrap.

Preloop honours the forwarded identity headers (`x-claude-gateway-user-id`, `x-claude-gateway-user-email`, `x-litellm-end-user-id`) only on a key with the trusted upstream scope, and only when the request carries the matching `x-preloop-upstream-secret`. A trusted key with a configured secret and a missing or wrong secret gets 401. On any other key those headers are ignored.

### Budgets and 429

Each developer becomes a gateway subject at Preloop, keyed on the IdP `sub`. When the email matches a member of your account, that user's budgets apply as well. When a budget or rate limit denies a request from a trusted upstream key that carries identity headers, Preloop answers **429** (not 403) with `retry-after` and, for budgets, `x-should-retry: false`, and an Anthropic error body of type `billing_error` (budgets) or `rate_limit_error` (rate limits). The apps gateway returns a 429 for a request that carried the developer's email to the developer as-is, so the limit holds; a 403 would make it fail over to the next upstream. A developer whose IdP token has no email is forwarded without the email headers, and the gateway treats a 429 for them as capacity and fails over. Configure your IdP to supply the email.

## Check what a device uses

```bash
preloop agents discover
preloop agents discover --json
```

Discovery reads the managed configuration (read-only) and reports Claude Desktop as `gateway-bound (direct)` when `inferenceProvider` is `gateway` and the base URL is this Preloop, `gateway-bound (apps gateway)` when `bootstrapUrl` is set, and `MCP only` otherwise. The JSON output carries `model_route`: `direct`, `apps-gateway` or `mcp-only`.

## What this does not cover

- Tool governance stays on MCP (the bridge or a custom connector). Cowork and Code built-in tools are governed by Desktop and the gateway policy, not by Preloop; Preloop sees them only as model traffic.
- Behind an apps gateway, Preloop cannot tell Claude Desktop traffic from Claude Code traffic: usage records `client: unknown`.
- Desktop single sign-on validated by Preloop itself (`inferenceIdpOidc` tokens) is not supported yet.

## Related

- [Cursor, Claude Desktop and other MCP clients](other-mcp-clients.md)
- [Model gateway](../concepts/model-gateway.md)
- [Subject-scoped governance](../concepts/subject-scoped-governance.md)
- [CLI reference: support levels](../cli.md#support-levels)
