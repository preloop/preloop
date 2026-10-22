# Compatibility policy

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Operators upgrading a self-hosted install, and anyone scripting against
Preloop, need to know which surfaces are stable and how a change to them
is announced. This page is that contract. It is a prerequisite for 1.0.0.

Preloop versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A minor release line is one `X.Y` (for example every `0.18.z` patch). The
rules below apply only to the public surfaces.

## Public surfaces

- REST API under `/api/v1` (the paths and schemas in `openapi.yaml`, plus
  five endpoints hidden from the schema that clients outside the console
  depend on: `/api/v1/ping` and `/api/v1/health` (Helm probes),
  `/api/v1/version` (CLI and mobile apps), `/api/v1/features` (CLI) and
  `/api/v1/openapi.json` (the schema itself). Other hidden endpoints under
  `/api/v1`, such as `/api/v1/version/status` and
  `/api/v1/configuration-capabilities`, serve only the console, which
  ships in the same release as the server, and are intentionally outside
  the promise). See [API versioning](#api-versioning).
- The MCP endpoint (`/mcp`, with `/mcp/v1` retained for compatibility),
  the model gateway prefixes (`/openai/v1`, `/anthropic/v1`, `/gemini/v1beta`) and the OAuth
  endpoints (`/oauth/*`, `/.well-known/*`). Their paths are stable; their
  wire formats are owned by the protocol or provider they implement, see
  [Surfaces outside `/api/v1`](#surfaces-outside-apiv1).
- CLI commands and flags (`preloop ...`), including exit codes and
  machine-readable output.
- Webhook payloads Preloop sends and the event types it accepts.
- The runtime plugin protocol (`preloop.agent_control.v1`) used by the
  published sidecars.
- Helm chart values.
- `result.json` and evidence pack formats produced by flow executions.
- Database migrations: forward-only, applied by the upgrade hook. See
  [schema migrations](operations/schema-migrations.md).

## What is not public

Anything not listed above is not public and can change in any release.
That includes internal Python modules, database tables, console HTML, and
undocumented endpoints.

## The rule

1. Within a minor release line, changes to a public surface are additive
   only (new fields, new optional flags, new endpoints, new event types).
2. A removal or incompatible change is announced as a deprecation at least
   one minor release ahead, in the changelog and, where the surface allows
   it, at runtime (a response header, a CLI warning, a log line).
3. The removal ships no earlier than two minor releases after the
   deprecation. A surface deprecated in `0.18.0` is still present in
   `0.19.x` and is not removed before `0.20.0`.
4. Security fixes may break this rule when there is no safe additive path.
   The changelog says so and names the affected surface.
5. Migrations are forward-only. Downgrade means restore from backup.

## API versioning

Status: accepted on 2026-10-13 in
[ADR 0001](architecture/decisions/0001-api-versioning.md) for
[#977](https://github.com/preloop/preloop/issues/977). This section becomes
binding at 1.0.0. Pending implementation work is identified below.

### What `/api/v1` promises

`/api/v1` remains a stable REST surface throughout the 1.x release line.
Compatible additions may ship within it; incompatible changes require a new
path version. Deprecation notice never permits breaking `/api/v1` in place.
The security exception in the general rule still applies.

The version is in the path and nowhere else. There is no request header
that selects an API version, and no per-key or per-account pinning. Two
clients use the same REST contract regardless of their key or account;
authorization and account-specific data still determine their results.

An incompatible change to the REST API is made by adding `/api/v2` beside
`/api/v1`, not by changing `/api/v1`. When that happens:

- `/api/v2` is cut only for a change that cannot be made additively; it is
  not cut on a schedule and not to tidy up.
- Both trees are served during the deprecation window. `/api/v1` keeps
  working for at least six months after the first release that ships
  `/api/v2`, and never for fewer than the two minor releases the rule
  requires, whichever is later.
- During the window every `/api/v1` response carries a `Deprecation`
  header ([RFC 9745](https://www.rfc-editor.org/rfc/rfc9745)) and, once
  the removal release is known, a `Sunset` header
  ([RFC 8594](https://www.rfc-editor.org/rfc/rfc8594)) with that date.
- Removing `/api/v1` is a major release under semantic versioning.

### What counts as a breaking change

On `/api/v1`, any of the following is breaking and requires `/api/v2` while
preserving `/api/v1` during the deprecation window above:

- Removing or renaming a path, a method on a path, a query parameter, a
  request field or a response field.
- Changing the type, format or meaning of an existing field, or making an
  optional request field required.
- Changing a status code for an existing outcome, or the shape of the
  error body.
- Tightening validation so a request that was accepted is now rejected.
- Changing an enum so that a value a client may already send or receive
  disappears.

The following are not breaking and may ship in any release:

- New paths, methods, optional query parameters, optional request fields,
  response fields and enum values a client is not required to handle.
- New response headers.
- Changes behind a feature flag or an opt-in parameter.
- Any change to an endpoint that `openapi.yaml` does not list and this
  page does not name.

Clients are expected to ignore response fields and headers they do not
know. A client that fails on an unknown field is not covered.

### Surfaces outside `/api/v1`

These surfaces are public, their paths are stable, and they are explicitly
excluded from the `/api/v1` version promise. Their protocol or provider
contracts evolve independently of the REST path version. Preloop-owned URLs and extensions
remain covered by the general compatibility rule:

| Surface | Paths | Versioned by |
|---|---|---|
| OAuth discovery | `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource` | RFC 8414 and RFC 9728 define well-known URL construction from the issuer/resource identifier; keep `/.well-known/` in its standard position. Path-specific metadata may also apply. |
| OAuth endpoints | `/oauth/authorize`, `/oauth/token`, `/oauth/register`, `/oauth/revoke` | OAuth specifications, including RFC 6749, 7591 and 7009. These literal paths are chosen by Preloop, not mandated by the RFCs; they remain stable and are advertised in authorization-server metadata. |
| MCP | `/mcp` (rewritten to `/mcp/v1`), `/mcp/v1` | Dated MCP protocol revisions, selected at the same URL. `/mcp` is the documented entry point; `/mcp/v1` remains compatible. The mount suffix is not an MCP protocol or SDK version, and upgrading either does not require `/mcp/v2`. |
| Model gateway | `/openai/v1`, `/anthropic/v1`, `/gemini/v1beta` | The upstream provider's API version the prefix mirrors, including applicable provider version headers. Request and response bodies are the provider's; Preloop's additions (`X-Preloop-*` headers, budget and policy errors) follow the additive rule above. A new prefix (for example `/openai/v2`) appears only when the provider ships one, beside the old one. |

MCP revisions `2025-11-25` and earlier use `initialize` version negotiation
(and `MCP-Protocol-Version` for applicable HTTP revisions). Revision
`2026-07-28` uses per-request `_meta` version metadata and a matching
`MCP-Protocol-Version` header, with `server/discover` for discovering
supported versions and capabilities. Modern and legacy clients can share
an endpoint under the
[MCP compatibility specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning).
The [tested Preloop matrix](architecture/mcp-protocol-compatibility.md) records
SDK/FastMCP versions, supported transports, and migration details.
Preloop supports the modern revision with Python SDK v2 and FastMCP 4, while
retaining the legacy revisions documented in that matrix
([#1629](https://github.com/preloop/preloop/issues/1629)).

Browser pages such as `/approval/{id}`, `/invitations/accept`,
`/mcp/authorize/consent` and `/docs/*` are console HTML, not public API,
and may change in any release.

### Version discovery

The accepted design adds a diagnostics-only header to every response under
`/api/v1/`, including errors:

```text
Preloop-Server-Version: <server software release>
```

Its value is consistent with `GET /api/v1/version` (`server_version`), which
needs no authentication. It identifies the software release that answered,
not the REST contract version, and has no request-side meaning. Clients do
not send a header to select REST behavior.

The header is not emitted yet. Implementation and client REST-prefix
centralization are tracked in
[#1630](https://github.com/preloop/preloop/issues/1630), linked from the
[1.0.0 readiness checklist](https://github.com/preloop/preloop/issues/978).
