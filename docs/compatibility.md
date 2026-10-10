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
- The MCP endpoint (`/mcp`, canonical `/mcp/v1`), the model gateway
  prefixes (`/openai/v1`, `/anthropic/v1`, `/gemini/v1beta`) and the OAuth
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

Status: proposed in
[ADR 0001](architecture/decisions/0001-api-versioning.md) for
[#977](https://github.com/preloop/preloop/issues/977). This section becomes
binding at 1.0.0 when that record is Accepted; until then it describes the
intended policy.

### What `/api/v1` promises

`/api/v1` is the REST surface for the whole 1.x release line. Within it,
the rule above applies: changes are additive within a minor line, removals
and incompatible changes are announced as deprecations and ship no earlier
than two minor releases later.

The version is in the path and nowhere else. There is no request header
that selects an API version, and no per-key or per-account pinning. Two
clients sending the same request to the same server get the same answer.

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

On `/api/v1`, any of the following is breaking and needs the deprecation
path above (or `/api/v2`):

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
excluded from the `/api/v1` version promise. Each one is versioned by the
specification it implements, not by Preloop:

| Surface | Paths | Versioned by |
|---|---|---|
| OAuth discovery | `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource` | RFC 8414 and RFC 9728 fix these locations. |
| OAuth endpoints | `/oauth/authorize`, `/oauth/token`, `/oauth/register`, `/oauth/revoke` | RFC 6749, 7591 and 7009. The metadata document is the contract; clients should take these URLs from it. |
| MCP | `/mcp` (rewritten to `/mcp/v1`), `/mcp/v1` | The MCP protocol version negotiated at `initialize` (`protocolVersion`) and the `MCP-Protocol-Version` header. The `v1` in the path is Preloop's mount and will not change while the MCP transport stays compatible. |
| Model gateway | `/openai/v1`, `/anthropic/v1`, `/gemini/v1beta` | The upstream provider's API version the prefix mirrors. Request and response bodies are the provider's; Preloop's additions (`X-Preloop-*` headers, budget and policy errors) follow the additive rule above. A new prefix (for example `/openai/v2`) appears only when the provider ships one, beside the old one. |

Browser pages such as `/approval/{id}`, `/invitations/accept`,
`/mcp/authorize/consent` and `/docs/*` are console HTML, not public API,
and may change in any release.

### Version discovery

Every response under `/api/v1/` carries one header:

```text
Preloop-API-Version: <info.version of the openapi.yaml this server serves>
```

It is diagnostics only. It tells a reader of a proxy log, a support bundle
or a `curl -i` which API document the answer was produced under, so the
matching `openapi.yaml` can be fetched. It has no request-side meaning:
sending it changes nothing. The same value is available without the
header from `GET /api/v1/version` (`server_version`), which needs no
authentication.

The header is not emitted yet; it is one of the follow-ups filed when ADR
0001 is Accepted.
