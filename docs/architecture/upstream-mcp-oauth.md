# Upstream MCP OAuth lifecycle

The external MCP client uses `services/upstream_mcp_oauth.py`. This is separate
from Preloop's inbound MCP authorization server. Apply the additive
`20261110_upstream_mcp_oauth` migration before running this application version.
Drain old application workers before accepting new OAuth setup/refresh requests:
they cannot read the new encrypted references. Downgrading removes the new
connection records and their references; grants created by this lifecycle then
need reconnecting. Existing bearer/API-key connections are unchanged; existing OAuth grants remain
usable and are moved into encrypted storage on their next refresh or reconnect.

## Discovery and authorization

An unauthenticated GET reads the MCP endpoint's `WWW-Authenticate`
`resource_metadata` challenge. Without an advertised location, discovery tries
the path-aware and origin `/.well-known/oauth-protected-resource` locations.
The metadata's resource must match the configured URL. Its selected
`authorization_servers` issuer is resolved through RFC 8414 or OIDC metadata,
whose issuer must match exactly. Several advertised issuers require an explicit
`auth_config.issuer`; explicit metadata never falls back to contradictory
legacy metadata. Legacy servers may advertise authorization-server metadata
at their MCP path or origin when protected-resource metadata is absent.

Setup accepts a pre-registered `client_id`, optional write-only `client_secret`,
`token_endpoint_auth_method` (`none`, `client_secret_post`, `client_secret_basic`),
and `scope` or `scopes`. Otherwise it attempts advertised RFC 7591 public client
registration. Authorization uses S256 PKCE, a state nonce, the exact callback URI
and resource indicators on both authorization and token requests. Configure
`PRELOOP_URL` as the external console origin to obtain the correct callback URI.

The ten-minute transaction stores hashed state/browser nonces, account and user
identity, the user's authentication generation, server configuration version,
issuer, redirect URI, and encrypted PKCE/context. An HttpOnly, SameSite=Lax cookie
binds the callback to the initiating browser (Secure on HTTPS). CRUD locks the
server and consumes the transaction once, before token exchange, on any worker.
Expired transactions are removed on subsequent setup/completion; the indexed
CRUD `cleanup` operation can also be invoked by maintenance. Expiration is
always enforced even before physical cleanup. Deletion, disconnect, or a
configuration replacement invalidates obsolete state. Credential completion
compares the version again so an in-flight exchange cannot undo replacement.

## Storage and refresh

`upstream_mcp_oauth_connection` owns a tenant-scoped `SecretReference` using the
existing local encrypted backend. The secret contains the grant and registration
credentials. `mcp_server.auth_config` retains public configuration; API reads
reconstruct only a redacted view. Policy snapshots do not copy new OAuth grants.
A write-only edit merges redaction markers under the server lock, so a stale
browser response cannot restore tokens superseded by a refresh.

Before every MCP session, the pool reads the current connection version.
Tokens expiring within 60 seconds require refresh; absent expiry is treated as
unknown. CRUD claims a bounded lease in a short transaction, releases the DB
before the token request, then commits the complete rotated pair and expiry
atomically. A missing replacement refresh token retains the previous value;
missing expiry clears the previous expiry. Other workers wait for the winning
version. A stale completion cannot overwrite a new version or disconnect.
Configured introspection checks the resolved token again before a session.

A refresh POST has a 15-second deadline and is never retried automatically.
Invalid grants, malformed responses, timeouts, and expired abandoned leases
require reconnecting because the token might already have been consumed. An
explicit rate limit releases the lease with a cooldown. At most one recovery
refresh is allowed for an explicit authentication rejection during session
setup, before dispatch. A dispatched tool call is never replayed; connection
loss or timeout returns an **unknown outcome**, instructing the caller to check
the remote result before deciding to invoke the tool again.

## Network and privacy boundaries

Discovery has a 45-second total budget, 15 seconds per request, at most two
transient retries, and three redirects with loop detection. `Retry-After` must
fit the remaining budget. Every outbound destination and redirect is validated.
DNS results are checked and the connection is pinned to a checked IP while
preserving HTTP Host and TLS SNI/certificate validation. Ambient proxy settings
and credentials are disabled. Token/registration POST redirects are refused.

HTTPS public addresses are the default. Explicit local development can allow
private or loopback CIDRs through `MCP_OAUTH_ALLOWED_CIDRS`; HTTP requires this
operator opt-in. Link-local, unspecified, multicast, and reserved addresses stay
blocked. This setting is independent of model-gateway network exceptions.

Metadata and registration never receive the MCP bearer. Errors omit provider
bodies. Application access logs redact OAuth callback/authorize queries and error
telemetry excludes these routes. Configure any external reverse proxy to omit
query strings for these routes too.

## Console and disconnect

The Lit tools setup shows disconnected, connecting, connected, and reconnect
required. Connect/reconnect opens provider consent; cancel/disconnect clears
local access/refresh grants and pending transactions and invalidates pooled
credentials. Encrypted client registration remains available for reconnect.
Disconnect intentionally makes no upstream revocation request; revocation is
not part of this local operation. Disabling an OAuth server drops its grant the
same way, so it reads as disconnected and needs a reconnect after re-enabling.

Every write goes through `crud_mcp_server`, including policy apply: OAuth secrets
never reach the `auth_config` JSONB column, and switching a server away from
OAuth keeps only the public fields of its old configuration. A snapshot
round-trip re-applies the exported public configuration without touching the
stored grant.

All automated coverage uses synthetic providers and a local PostgreSQL database.
Real-provider authenticated compatibility remains a separately authorized release
gate. This lifecycle continues to use the MCP SDK 1.x transport API.
