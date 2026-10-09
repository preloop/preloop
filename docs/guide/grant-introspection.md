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

The example uses CEL conditions, which require the enterprise CEL evaluator. Simple conditions such as `grant.active == true` are available with the open-source evaluator.

Use secret environment references rather than real credentials in a policy file. Configure the referenced MCP server in the same policy or in your account before importing a rule that references `grant`.

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

The enterprise audit plugin records the delegated subject, scope list, client ID, consent reference, expiry and cache flag under the audit row's `details.grant`. Both tool-call rows and denied policy decisions retain this attribution. Token and introspection-secret fields are excluded before a write is queued.

With permission to view audit logs, search a consent reference using `GET /api/v1/audit-logs?consent_ref=consent-example` or the grouped timeline at `GET /api/v1/audit-logs/grouped?consent_ref=consent-example`. Matching is exact and always scoped to your account. Pagination and the returned total use the same filter. A partial composite index on account and consent reference supports these lookups; its migration builds and removes the index concurrently to avoid blocking audit inserts.

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
