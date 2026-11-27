# Delegated grant introspection

An upstream MCP server can use short-lived bearer tokens whose scopes or consent can change at its authorization server. Configure RFC 7662 introspection on that MCP server to check the upstream grant before evaluating tool rules.

The token checked is the bearer in `auth_config.token`, or the OAuth access token in `auth_config.access_token`, that Preloop forwards to the upstream. This configuration applies to the server token; per-caller token exchange and token passthrough are outside this feature.

## Configure the authorization server

Create a confidential client at your authorization server with permission to introspect the upstream access token. Use a dedicated client ID and secret, and select the authentication method the endpoint supports: `basic` for HTTP Basic authentication, or `post` for form client credentials. The endpoint must use HTTPS. Redirects and environment proxy settings are disabled.

The endpoint receives a form POST containing `token`. An active response can look like this synthetic example:

```json
{
  "active": true,
  "scope": "records:read records:list",
  "sub": "subject-example",
  "client_id": "upstream-agent",
  "exp": 2000000000,
  "consent_ref": "consent-example"
}
```

`scope` must be a space-delimited string, and `exp`, when present, must be an integer Unix timestamp. The consent claim name is configurable. A malformed response is treated as unavailable.

## Policy example

The example uses CEL conditions, which every edition evaluates. Simple conditions such as `grant.active == true` work too.

`preloop policy apply` resolves `${NAME}` references from your local environment
before upload, including with `--dry-run`. Unset variables stop the command.
The API preserves unresolved references and returns a warning. Configure the
referenced MCP server in the same policy or in your account before importing a
rule that references `grant`.

```yaml
version: "1.0"
metadata:
  name: Delegated record reads
mcp_servers:
  - name: upstream-protected
    url: https://mcp.example.com/mcp
    auth_type: bearer
    auth_config:
      token: ${UPSTREAM_BEARER_TOKEN}
      introspection:
        endpoint: https://as.example.com/introspect
        client_id: preloop-firewall
        client_secret: ${INTROSPECTION_CLIENT_SECRET}
        client_auth: post
        timeout_seconds: 2
        max_cache_ttl_seconds: 60
        negative_cache_ttl_seconds: 5
        consent_ref_claim: consent_ref
        required_scopes: ["records:read"]
        fail_open: false
tools:
  - name: read_record
    source: upstream-protected
    conditions:
      - expression: 'grant.active && "records:read" in grant.scope'
        condition_type: cel
        action: allow
      - expression: "true"
        condition_type: cel
        action: deny
```

The hard grant gate runs before these rules. An inactive or expired grant denies with `grant_inactive`; a missing configured required scope denies with `scope_not_granted`. These decisions prevent forwarding. A timeout, non-success HTTP response or malformed payload denies with `introspection_unavailable` by default.

Setting `fail_open: true` permits evaluation and forwarding after an introspection failure, with `grant.available` set to `false`. It still denies inactive, expired and insufficient-scope grants. A rule that requires `grant.active` can independently deny an unavailable grant.

## Rule bindings

| Binding | Value |
| --- | --- |
| `grant.available` | Whether introspection supplied a valid response |
| `grant.active` | Authorization server active flag |
| `grant.scope` | List of individual scope strings |
| `grant.sub` | Subject string, or null |
| `grant.client_id` | Delegated client ID, or null |
| `grant.exp` | Expiry Unix timestamp, or null |
| `grant.consent_ref` | Configured consent claim, or null |
| `grant.cached` | Whether this evaluation reused a cached response |

Both simple and CEL rules can use the binding. Imported rules referencing it require introspection configuration on their named MCP server.

## Cache and revocation

Active responses are cached for the smaller of the configured maximum and the remaining token lifetime. The default maximum is 60 seconds; the permitted maximum is 300 seconds. A token without `exp` uses the configured maximum. Inactive and unavailable results use the negative TTL, default 5 seconds and maximum 60 seconds. Set either TTL to zero to disable that cache class.

Revocation or scope narrowing becomes visible when the cached result expires. The process cache is bounded to 4096 entries and isolates server and introspection configuration. It stores hashes of the token and configuration, never the token or introspection secret. Each process maintains its own cache.

The introspection client secret follows the MCP authentication secret redaction and write-only update behavior. JWKS validation, revocation webhooks and automatic token refresh are outside this feature; rotate an upstream token using the MCP server update API.

## Audit evidence and consent searches

The core audit service records the delegated subject, scope list, client ID, consent reference, expiry and cache flag under the audit row's `details.grant`. Both tool-call rows and denied policy decisions retain this attribution. Token and introspection-secret fields are excluded before a write is queued.

With permission to view audit logs, search a consent reference using `GET /api/v1/audit-logs?consent_ref=consent-example` or the grouped timeline at `GET /api/v1/audit-logs/grouped?consent_ref=consent-example`. Matching is exact and always scoped to your account. Pagination and the returned total use the same filter. A partial composite index on account and consent reference supports these lookups; its migration builds and removes the index concurrently to avoid blocking audit inserts.

Grant-gate `policy_deny` rows for `grant_inactive`, `scope_not_granted` and
`introspection_unavailable` include the same API key, runtime session and runtime
principal fields as policy allow rows for that caller.

An inactive answer may contain only `{"active": false}`. When it omits the consent
claim, Preloop leaves `details.grant.consent_ref` null and looks up the most recent
stored consent claim for the same account, server ID and authentication/configuration revision.
If found, it records `details.grant.last_known_consent_ref`. This is historical
evidence, **not a claim that the consent is currently active**. The lookup never
uses another denial's fallback reference and adds no token cache or fingerprint.
It does not change upstream credentials, arguments or grant decisions.

Add `include_last_known_consent=true` to either consent search endpoint to include
these denials, for example:

```http
GET /api/v1/audit-logs?consent_ref=consent-example&include_last_known_consent=true
```

The option defaults to false. Both pagination and totals use the same exact,
account-scoped match. Last-known searches and server history lookups have partial
composite indexes built concurrently with audit writes.

History is available only from stored rows carrying the server identity and
revision, written by this version. The revision is an opaque UUID that changes
when the credential binding changes (authentication/introspection configuration,
endpoint, transport or disabling the server). Diagnostic errors, scans and name
edits preserve it. OAuth credential versions are also separated. No reference is inferred from older rows without that attribution or
from writes that have not reached the audit log yet. If reference-only logging
covers the call, the fallback is omitted so a historical clear-text consent
reference cannot be reintroduced. A failed evidence lookup also omits the
fallback; the grant denial still takes effect.

## Test a synthetic grant

In the rule or YAML simulator, enter a synthetic grant JSON object alongside sample arguments, for example `{"active": true, "scope": ["records:read"], "sub": "subject-example"}`. Do not enter a bearer token or client secret. The sample schema rejects credential fields and incorrect types.

For a configured server, simulation requires a sample and applies the same pure inactive, expiry, required-scope and fail-open gate before matching rules. It uses the declared draft configuration when present, otherwise the named account server configuration. Missing samples produce an error; the simulator never calls the authorization server, forwards a tool, creates an approval or writes an audit row. A rule draft without configured introspection can still test `grant.*` conditions against the supplied sample.

## Dispatch and approval identity

The firewall uses the server selected by the same prefix and first-wins routing
as tool dispatch. It copies that server configuration before introspection and
keeps it for policy matching, synchronous approval and forwarding, so editing a
token, prefix or collision owner during an approval cannot switch the approved
call to a different upstream credential. It checks the copied token again after
approval and client-connection waits, honoring expiry and the configured cache
TTL. An asynchronous approval replay resolves a fresh snapshot and applies the
grant gate even though its access-rule approval has already been granted.

## Approval after an upstream scope refusal

An MCP server can opt into an external approval hand-off when the upstream
returns HTTP 403 with `insufficient_scope` in its `WWW-Authenticate` challenge
or JSON error body. Configure the server's `auth_config`:

```json
{
  "on_insufficient_scope": "request_approval",
  "approval_workflow": "00000000-0000-4000-8000-000000000001"
}
```

`approval_workflow` is the UUID of a workflow in the same account. Pair it with
an [external-link workflow](webhooks.md#external-approval-hand-off). All settings
are optional. Without the per-server setting, upstream errors pass through as
before. Other statuses and other 403 errors never trigger this flow. An MCP
error result can also carry an explicit numeric `status` or `status_code` of
403 and an `error: "insufficient_scope"` or `error.code` field in its structured
content; text-only tool errors cannot impersonate an HTTP refusal.

Preloop records the refused `tool_call` with error code `insufficient_scope`,
then creates one approval with the same correlation ID. The external service
owns the question and consent; Preloop neither generates that question with a
model nor substitutes an AI or local bypass decision for external consent.

On approval, Preloop loads the server's current credential, forces fresh grant
introspection when configured, and retries the original call **once**. This
supports either a widened grant on the same token or a credential rotated
while the call was held. The original server and upstream tool identity must
still match; editing the tool catalogue cannot redirect the approved retry to
another server. Current halt and restricted-runtime checks still apply.

The agent receives the retry's tool result. Decline, expiry, a failed grant
check, or a failed retry produces a single tool error naming the outcome and
external reference. A second refusal never creates another approval or a
second retry. The refused attempt and final outcome are separately audited
under the same correlation ID; the decision audit carries the structured
external and grant references.

Async workflows use the same checks through `get_approval_status`. The replay
claim and cached result prevent later polls from executing the call again.
As with other async approvals, stored arguments are subject to storage
redaction; a reference-only record without replayable arguments cannot be
reconstructed into an original call. Token exchange per caller is not part of
this flow.
