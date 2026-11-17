# Consent-gated tool calls with LifeCare.ID

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

LifeCare.ID is a consent layer for personal health data. The person holds the grants, every grant has a consent receipt, and every read writes a line in the person's ledger.

This page shows how to put Preloop in front of an agent that reads from a LifeCare.ID data source, so that:

- every tool call is checked against the person's grant before it is forwarded,
- a revoked grant is refused at Preloop, and the source is never called,
- the Preloop audit row and the person's ledger line carry the same consent id.

Preloop governs the agent. LifeCare.ID governs the data. Preloop never issues, widens or stores a grant. It asks LifeCare.ID whether the grant behind the token is active, and refuses when it is not.

This page is written for and was run on Preloop 1.0.2. The flow was run end to end on a self-hosted 1.0.2 against the LifeCare.ID sandbox with a synthetic person. The client example in step 4 was run on 1.0.2 against a local test source.

## What you need

- A Preloop account or a self-hosted install of Preloop 1.0.2.
- A LifeCare.ID data source that speaks MCP. The sandbox source is `https://lifecare.id/sandbox-source/mcp`.
- A LifeCare.ID access token for that source. For introspection, use your own sandbox client's id and secret. See the LifeCare.ID developer documentation for both.

## 1. Add the data source with introspection

Add the source as an MCP server and point introspection at the LifeCare.ID token endpoint. The three `${...}` values below are placeholders: on 1.0.2, put the real values in before you apply the file, and do not commit the result. The 1.0.2 `preloop policy apply` uploads the file as it is and does not read your shell environment. From the first CLI release after 1.0.2, `preloop policy apply` fills `${...}` references from your environment, so you can export the three values and keep the placeholders in the file (`--no-resolve-env` keeps them literal).

```yaml
version: "1.0"
metadata:
  name: LifeCare.ID sandbox
mcp_servers:
  - name: lifecare-sandbox-source
    url: https://lifecare.id/sandbox-source/mcp
    auth_type: bearer
    auth_config:
      token: ${LIFECARE_ACCESS_TOKEN}
      introspection:
        endpoint: https://sandbox.lifecare.id/v1/token/introspect
        client_id: ${LIFECARE_INTROSPECTION_CLIENT_ID}
        client_secret: ${LIFECARE_INTROSPECTION_CLIENT_SECRET}
        client_auth: basic
        consent_ref_claim: consent_id
        max_cache_ttl_seconds: 0
        negative_cache_ttl_seconds: 0
        fail_open: false
```

What the settings mean here:

| Setting | Value | Why |
|---|---|---|
| `consent_ref_claim` | `consent_id` | LifeCare.ID returns the consent id under this name. Preloop records it as `grant.consent_ref`. |
| `max_cache_ttl_seconds` | `0` | Ask on every call. A revoke takes effect on the next call. |
| `negative_cache_ttl_seconds` | `0` | An inactive answer is not reused. The next call asks again. |
| `fail_open` | `false` | If LifeCare.ID cannot be reached, the call is refused. |

With a cache above zero, a revoke becomes visible when the cached answer expires. The default is 60 seconds for active grants and 5 seconds for inactive ones. See [Delegated grant introspection](../grant-introspection.md).

## 2. Allow only what the grant covers

Introspection already refuses an inactive grant before any rule runs. Add a rule for the scopes this agent may ask for at all:

```yaml
tools:
  - name: read_scope
    source: lifecare-sandbox-source
    conditions:
      - expression: "args.scope == 'daily'"
        action: allow
        description: daily is in the grant
      - expression: "true"
        action: deny
        description: every other scope
```

The last condition matters. A tool with only an allow condition lets every other call through to the source, which then refuses it with a 403.

A rule can only narrow. Preloop can refuse a call that the token would allow. It cannot allow a call that the token does not cover, because the source checks the token again.

## 3. Keep the record by reference

Reference-only logging governs the audit rows. On 1.0.2 the application log of the `api` container still prints tool arguments at INFO when a rule is evaluated; that is a defect and is being fixed, so until the fixed release treat container logs as holding argument values.

By default the audit row stores the tool arguments, and here they include the person's DID. Turn on reference-only logging for this tool so the row keeps a salted hash of the arguments and only the two values you need to match the ledger:

```yaml
sensitive_data:
  reference_only:
    - id: lifecare-reads
      scope: {tools: [read_scope], servers: [lifecare-sandbox-source]}
      keep_fields:
        - "$result.consent_id"
        - "$result.ledger_entry_hash"
      approver_view: redacted
```

In the console: Policies, Sensitive data, Reference-only logging, Add reference-only scope.

The scope applies to rows written after you save it. To apply it to rows already stored, an account admin runs `preloop policy apply-stored`. It is a dry run unless you pass `--apply`. See [Sensitive data](../sensitive-data.md).

Apply the file with `preloop policy apply policy.yaml`, then scan the server (Tools, the server entry, Scan for new tools) so its tools appear. Applying a policy does not scan a new server.

## 4. Call the tool from the agent

The agent calls `read_scope` at Preloop's MCP endpoint, not at the source. Preloop checks the grant and the rule, then forwards the call with the token from step 1. No model provider is needed for this.

The agent needs its own credential. In the console: Agents, Onboard existing agent, Connect a custom agent. Name the agent, then choose Register agent & mint credential on the last step. The result screen shows the credential once. It starts with `agt_`. Over the API the same two steps are `POST /api/v1/agents` and `POST /api/v1/agents/{agent_id}/credentials`.

The endpoint is `/mcp/v1` on your Preloop URL. It speaks MCP over streamable HTTP: every message is a JSON-RPC `POST` to that one URL, with these headers:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <agent credential>` |
| `Content-Type` | `application/json` |
| `Accept` | `application/json, text/event-stream` (both values; with only `application/json` the server answers 406) |

Preloop runs the endpoint stateless. `initialize` returns no `Mcp-Session-Id` header, so there is no session id to carry from one request to the next.

```bash
PRELOOP_URL=https://YOUR_PRELOOP_URL
AGENT_TOKEN=agt_YOUR_AGENT_CREDENTIAL

# 1. initialize
curl -sS -X POST "$PRELOOP_URL/mcp/v1" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"lifecare-reader","version":"1.0"}}}'

# 2. initialized notification (answers 202 with no body)
curl -sS -X POST "$PRELOOP_URL/mcp/v1" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3. the read
curl -sS -X POST "$PRELOOP_URL/mcp/v1" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_scope","arguments":{"scope":"daily","owner":"YOUR_PERSON_DID"}}}'
```

On 1.0.2 the answer to a request with an `id` comes back with HTTP 200 and `Content-Type: text/event-stream`. The body is one server-sent event whose `data:` line holds the JSON-RPC message:

```
event: message
data: {"jsonrpc":"2.0","id":2,"result":{...}}
```

With the grant active, `result` holds the answer of the source, with `isError` set to `false`. If the source also returns structured content, it is passed on as `structuredContent`. Shortened:

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"<answer of the source>"}],"isError":false}}
```

A refusal is also HTTP 200. It is a tool result with `isError` set to `true`, not a JSON-RPC error, and the text gives the reason:

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"Access denied: grant_inactive"}],"isError":true}}
```

| Case | Text the caller gets |
|---|---|
| Grant revoked or expired | `Access denied: grant_inactive` |
| Scope other than `daily` (the last condition in step 2) | `Access denied: every other scope` |
| LifeCare.ID introspection cannot be reached, with `fail_open: false` | `Access denied: introspection_unavailable` |

The text after `Access denied:` in the second row is the `description` of the condition that matched.

A missing or invalid credential is refused before any of this, with HTTP 401 and the JSON-RPC error `-32001`, "Authentication required. Provide Authorization: Bearer <token> header."

Any MCP client library can make the same call: point its streamable HTTP transport at the URL and set the `Authorization` header. See [Custom clients](../getting-started/connect-mcp-client.md#custom-clients).

## 5. What you see

One read with an active grant, then one read after the person revokes:

| Step | Preloop audit row | LifeCare.ID |
|---|---|---|
| Read, grant active | `policy_allow`, then `tool_call` with status `executed`. `details.grant` holds `active: true`, the scope list and `consent_ref`. | Read served. One access line in the person's ledger with the same consent id. |
| Person revokes | nothing | Revocation recorded. |
| Read, grant revoked | `policy_deny` with rule `grant_inactive`. On a self-hosted 1.0.2 open source install, no `tool_call` row (see below). | Never called. No ledger line. |

A deployment that runs the audit service for refused calls (the audit outbox, or an audit plugin) also writes a `tool_call` row with status `declined` for a refused call. The self-hosted 1.0.2 open source install does not.

The rows of one call share a `correlation_id`. The rows are hash-chained, so a removed or edited row is detectable. Sealing runs in the background: a new row shows empty chain fields for a minute or two.

To find every call made under one consent:

```
GET /api/v1/audit-logs?consent_ref=<consent id>
```

This returns every row that holds that consent id: the served reads, and refusals by rule while the grant was active. A refusal after revoke is not in the answer, because LifeCare.ID's inactive introspection response carries no consent id. Find those by time and `grant_inactive`.

On the served read, the `tool_call` row holds both kept values. The `policy_allow` row is written before the call, so it holds the consent id from introspection and no ledger hash.

## 6. Join the two records

Preloop identifies itself on every call to the source:

| Header | Value |
|---|---|
| `User-Agent` | `Preloop/<version>` |
| `X-Preloop-Correlation-Id` | the `correlation_id` of the audit rows for this call |

LifeCare.ID's ledger line does not store the correlation id today. Until it does, match the two records on the consent id and the time.

The headers are self-asserted. Use them to match records, not to authenticate the caller. See [Preloop identity headers](../tools/external-mcp.md#preloop-identity-headers).

## 7. Run it inside the node

For real data, run Preloop next to the agent, inside the boundary where the data lives:

```bash
curl -fsSL https://preloop.ai/install/oss | \
  PRELOOP_VERSION=1.0.2 \
  PRELOOP_DISABLE_TELEMETRY=true \
  sh
```

`PRELOOP_DISABLE_TELEMETRY=true` turns off the version check (one shortly after start, then daily), which is the only call Preloop makes to preloop.ai.

On 1.0.2 the installer does not save this setting. Add the line `PRELOOP_DISABLE_TELEMETRY=true` to the `.env` file in the install directory by hand (`~/.preloop-oss/.env`, or `$INSTALL_DIR/.env` if you set `INSTALL_DIR`). Without that line, a `docker compose up` from a shell that does not have the variable recreates the `api` container with the setting empty, which turns the check back on. From the first release after 1.0.2, the installer writes this line to `.env` for you.

Preloop's price catalogue also fetches a public model price file from `raw.githubusercontent.com`. On 1.0.2 the `api`, `gateway`, `worker` and `flow-worker` containers each fetch it once when they start, then again every 6 hours. After a failed fetch, the next try is 15 minutes later. The gateway can also fetch it once when it records usage for a model it has no price for. The request is a fixed URL and carries no account data. With that host blocked, the `api` and `gateway` services start healthy and tool calls work; the price refresh logs a connection error and shows as failed in the health details. That was tested with the host name pointed at loopback, which fails at once. It does not prove behaviour on a network that drops packets silently.

## Limits

- The token checked is the one Preloop holds for the source. Preloop does not exchange tokens per caller.
- A LifeCare.ID token keeps the one scope it was issued for. A wider or new grant means a new token, which you put on the server entry. Preloop uses the replaced token on the next call.
- Asking the person for a wider grant during a run is not supported yet. A call outside the grant is refused.
- Preloop does not read or register LifeCare.ID agent manifests.
- LifeCare.ID access tokens last at most twenty minutes and have no refresh token. Replace the token on the server entry with the MCP server update API (`PUT /api/v1/mcp-servers/{server_id}`) before it expires.
