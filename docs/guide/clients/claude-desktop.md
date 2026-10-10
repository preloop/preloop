# Claude Desktop

Editions: OSS, Cloud, Enterprise. Everything on this page ships in OSS.

After this page you can route Claude Desktop model traffic (Chat, Cowork and Code tabs) through the Preloop model gateway, either directly or behind a Claude apps gateway, and you know what Preloop sees and does not see on each route.

Claude Desktop in third-party mode reads its inference settings from the operating system's managed configuration. Preloop generates that configuration for you; your MDM (Jamf, Intune, Kandji, Fleet, or a root-owned file on Linux) deploys it. The CLI never writes managed configuration itself: those locations are admin-owned, and a wrong file there can disable Desktop's local settings entirely.

Tool governance is separate and unchanged: `preloop agents onboard "Claude Desktop"` (without `--model-route`) adds the managed MCP bridge, or you add Preloop as a custom connector. See [Cursor, Claude Desktop and other MCP clients](other-mcp-clients.md).

## Pick a route

| Route | Use it when | Identity at Preloop | What you run |
|-------|-------------|---------------------|--------------|
| **Direct** (`--model-route direct`) | Any Desktop fleet, including Preloop Cloud with no gateway of your own | The signed-in user's Preloop API key, minted by the CLI credential helper | Nothing beyond MDM |
| **Direct with your IdP** (`--model-route direct --auth idp`) | You want per-user identity without distributing Preloop keys, and your IdP speaks OpenID Connect | The user's IdP token, verified by Preloop against your issuer | Nothing beyond MDM and one admin setting |
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

Pass `--helper-path` (macOS and Linux) or `--helper-path-windows` when `preloop` is installed somewhere other than the default on managed devices: the current executable on this OS, otherwise `/usr/local/bin/preloop`, or `C:\Program Files\Preloop\preloop.exe` on Windows.

### The credential helper

```bash
preloop auth gateway-credential --client claude-desktop
```

Desktop runs this command, reads stdout and sends the result as `x-api-key` to Preloop. It prints one bare token and nothing else; diagnostics go to stderr. On first use it mints a Preloop API key for the signed-in CLI user and caches it in `~/.preloop/gateway-credentials/claude-desktop.json` (mode 0600). It checks that the cached key still exists on each run and mints a new one when the key was revoked. When the user is not signed in it exits non-zero with an empty stdout, so Desktop shows its credential error instead of sending a bad key. Each user runs `preloop login` once on the device.

Gateway calls made with this key are attributed to that user: user budgets, allowed-model lists, usage, sessions and audit apply as for any other Preloop key. Usage rows record `client: claude_desktop` from the `X-Preloop-Client` header.

## Sign in with your identity provider

Claude Desktop can sign each user in with your organization's OpenID Connect identity provider and send the resulting IdP token to Preloop as the bearer credential. Preloop verifies the token against your issuer, names the user as a gateway subject and applies the binding key's models, budgets and audit. No Preloop key is distributed and no credential helper runs on the device.

### Admin setup

1. **Register a Desktop app at your IdP**: a public client with PKCE and the loopback redirect URI `http://127.0.0.1/callback` (Okta needs the exact port: register `http://127.0.0.1:<port>/callback` and set `redirectPort`). Include the `email` claim, and `email_verified` if you restrict email domains. With Desktop's default `id_token` bearer, the token audience is this app's client ID.
2. **Create a binding API key** in Preloop (an account admin). Its account, allowed models and `per_subject_budget` apply to every IdP user. A trusted upstream key (`model_gateway:trusted_upstream`) is the way to set a `per_subject_budget`; the upstream secret is not used on this route.
3. **Register the issuer** under Settings, Gateway identity providers, or with the API (account admins only; create, update and delete are audited):

    ```bash
    curl -X POST https://YOUR_PRELOOP_URL/api/v1/account/gateway-identity-providers \
      -H "Authorization: Bearer $PRELOOP_TOKEN" -H "Content-Type: application/json" \
      -d '{"name": "Okta", "issuer": "https://YOUR_ORG.okta.com", "audiences": ["DESKTOP_CLIENT_ID"],
           "api_key_id": "BINDING_KEY_ID", "allowed_email_domains": ["corp.example"]}'
    curl -X POST https://YOUR_PRELOOP_URL/api/v1/account/gateway-identity-providers/PROVIDER_ID/test \
      -H "Authorization: Bearer $PRELOOP_TOKEN"   # fetches discovery and JWKS, lists key ids
    ```

4. **Generate and deploy the Desktop configuration**:

    ```bash
    preloop agents onboard "Claude Desktop" --model-route direct --auth idp \
      --issuer https://YOUR_ORG.okta.com --client-id DESKTOP_CLIENT_ID
    ```

    It sets `inferenceProvider: gateway`, `inferenceGatewayBaseUrl: https://YOUR_PRELOOP_URL/anthropic`, `inferenceCredentialKind: external-idp`, and `inferenceIdpOidc` (`issuer`, `clientId`, `scopes`, default `openid profile email offline_access`; change with `--scopes`). It repeats the block as `inferenceGatewayOidc` for Desktop releases that predate `inferenceIdpOidc`. `--auth key` (the default) keeps the credential helper configuration above.

Provider settings:

| Field | Default | Meaning |
|-------|---------|---------|
| `issuer` | required | Exact `iss` string, `https://` only |
| `audiences` | required | Accepted `aud` values. `(issuer, audience)` is unique across all Preloop accounts |
| `api_key_id` | required | The binding key; one provider per key |
| `allowed_email_domains` | empty (any) | Email domain allowlist; needs a verified email |
| `email_claim` | `email` | Claim holding the email |
| `require_email_verified` | `true` | Reject `email_verified: false`; without the claim the email is not used for linking or the domain check |
| `groups_claim`, `allowed_groups` | none | Group allowlist; groups are recorded on the first-seen audit event |
| `required_claims` | `{}` | Claim to exact value, for example a tenant id |
| `clock_skew_seconds` | `60` (max 300) | Leeway for `exp`, `nbf`, `iat` |
| `max_token_lifetime_seconds` | `86400` | Tokens with a longer `exp - iat` are rejected |
| `allowed_algorithms` | `RS256`, `ES256` | Asymmetric only; `none` and HMAC can never be configured |
| `allowed_jwks_hosts` | empty | Extra exact hosts `jwks_uri` may use (for example `www.googleapis.com` for Google) |
| `allow_private_network_issuer` | `false` | Fetch discovery and keys from private addresses; honoured only when the instance setting `GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS` is also on (self-hosted) |
| `enabled` | `true` | A disabled provider stops its tokens on the next request |

### What Preloop checks

A bearer is treated as an IdP token only when it is a JWT whose `iss` equals an enabled provider's issuer; everything else (Preloop API keys, Preloop session tokens, `x-api-key`) is handled exactly as before. Preloop then checks the signature against the issuer's JWKS (found through `<issuer>/.well-known/openid-configuration`), the algorithm, `iss`, `aud` (and `azp` when `aud` has several values), `exp`, `nbf`, `iat`, the lifetime cap, required claims, email domain, `email_verified`, groups and `sub` (at most 255 characters). The user becomes a gateway subject keyed on `sub` under the binding key; the email links to an existing member only, and no user is ever created. Usage rows record `gateway_source: direct`, `auth_method: idp` and `idp_provider_id`. Budgets and policy denials render as on the trusted identity path (see Budgets and 429 below).

A rejected token gets `401` with an Anthropic `authentication_error`, `WWW-Authenticate: Bearer error="invalid_token"` and a short reason code (`expired`, `bad_audience`, `bad_signature`, `domain_not_allowed`, `issuer_unavailable` and similar), so Desktop asks the user to sign in again. An unreachable JWKS also answers `401`, never a 5xx. Tokens are never logged; log lines carry a short fingerprint.

### Threat model

- **Token replay**: IdP tokens are bearer credentials, accepted only within `exp` plus skew and the lifetime cap. There is no replay cache, so the replay window equals the token lifetime. Revocation is the IdP's; disabling the provider, or deactivating the linked member, stops access on the next request.
- **Audience confusion**: `aud` must match a configured audience, `azp` is checked on multi-audience tokens, and `(issuer, audience)` is globally unique, so a shared multi-tenant issuer cannot cross accounts. A token naming audiences of two providers is rejected.
- **Issuer spoofing**: the unverified `iss` only selects a candidate; trust comes from the signature against keys fetched from that issuer's discovery document over https. The discovery document's `issuer` must equal the configured string, and `jwks_uri` must be https on the issuer host, a subdomain of it, or a host the admin listed.
- **Algorithm attacks**: `alg: none`, HMAC (including HS256 with the public key as the secret) and algorithms outside the allowlist are rejected; symmetric keys in a JWKS are ignored; `kid` must match a fetched key.
- **Email-claim trust**: the email links to a member only when `email_verified` is true (when required) and the domain is allowed. The subject key is `sub`, so an email change does not move budgets to someone else. Linking never grants console or REST access.
- **JWKS SSRF**: issuer URLs are fetched server side over https only, with private, loopback, link-local, shared and cloud metadata ranges refused by default, no cross-host redirects, a 5 second timeout and a 256 KiB size cap. DNS is checked before each fetch; a name that re-resolves between check and connect is not caught.
- **Denial of service**: the key set is cached per issuer for 5 to 60 minutes (from `Cache-Control`); an unknown `kid` or a failed fetch triggers at most one refetch per minute per issuer. Bearers over 16 KiB are rejected before parsing, and validation finishes before any database write.
- **Fail closed**: once a token's `iss` matched a provider, any error is a `401`; it never falls through to API key authentication.

## Apps gateway route

```bash
preloop agents onboard "Claude Desktop" --model-route apps-gateway \
  --gateway-url https://claude-gateway.internal.example.com --out ./gateway-config
```

This creates a Preloop API key with the scope `model_gateway:trusted_upstream` and a random upstream secret. Preloop stores only the secret's sha256. With `--key-id <id>` it reuses an existing trusted key and generates no new secrets.

Trusted upstream keys need a Preloop server with trusted upstream support, which also restricts creating them to account admins. Before printing anything, the CLI checks that the server enforces the secret (a request to `/anthropic/v1/models` with the new key must get 401 without `x-preloop-upstream-secret` and 200 with it). If the server does not, the CLI revokes the new key and stops.

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
- IdP tokens are accepted on `/anthropic/v1` only, not on the OpenAI or Gemini gateways, the REST API or the console login. Opaque (non-JWT) access tokens and token introspection are not supported.

## Related

- [Cursor, Claude Desktop and other MCP clients](other-mcp-clients.md)
- [Model gateway](../concepts/model-gateway.md)
- [Subject-scoped governance](../concepts/subject-scoped-governance.md)
- [CLI reference: support levels](../cli.md#support-levels)
