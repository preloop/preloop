# ADR 0001: API versioning before 1.0

Editions: OSS. Contributor documentation for this repository.

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-10 |
| Issue | [#977](https://github.com/preloop/preloop/issues/977) |
| Decider | project founder |
| Policy text | [Compatibility policy, API versioning](../../compatibility.md#api-versioning) |

This record settles what `/api/v1` promises at 1.0 and how the public
surfaces that do not carry that prefix are versioned. It is written so that
the decision can be taken by reading this page; the inventory in the
appendix was produced from the tree at `67a082ee0`, and every number cites
the command that produced it.

## Context

- All 315 path keys in `openapi.yaml` sit under `/api/v1/`. Nothing states
  what `v1` promises and there is no `v2` mechanism.
- Four surfaces sit outside that prefix and carry no Preloop version:
  OAuth (`/oauth/*`, `/.well-known/*`), MCP (`/mcp`, canonical `/mcp/v1`),
  the model gateway (`/openai/v1`, `/anthropic/v1`, `/gemini/v1beta`) and
  a handful of browser pages (`/approval/*`, `/invitations/accept`,
  `/docs/*`).
- No request or response header carries an API version. The CLI sends
  `X-Client-Version`; the server answers `GET /api/v1/version` without
  authentication and already returns `server_version`.
- `docs/compatibility.md` (merged in #1004) already fixes the deprecation
  rule for public surfaces (announce one minor ahead, remove no earlier
  than two minors later) and leaves the "API versioning" section pending
  on this record.
- Adding a prefix or moving a surface after 1.0 is itself a breaking
  change, so this has to be settled before 1.0.0.

## Decision points

Each point gives the options, one recommendation with its reasoning in at
most five lines, and the option that loses and why. Numbers in brackets
refer to the appendix.

### 1. What `/api/v1` commits to

Options:

- **A. Path version.** `/api/v1` is the stable 1.x surface under the
  compatibility policy. An incompatible REST change means `/api/v2`, served
  alongside `/api/v1` for the deprecation window.
- **B. Header version.** `/api/v1` stays a fixed prefix forever; incompatible
  changes are selected with a request header (for example
  `Preloop-API-Version: 2026-10-01`), with the default pinned per API key or
  account.

Recommendation: **A**.

1. Every client already addresses the API by path [A1, A3]; the Helm nginx
   config routes `location /api/` by prefix [A4], so `/api/v2` needs no
   proxy or ingress change.
2. B needs a pinned default per API key or account (no such column or
   setting exists), header-keyed caches and logs, and a branch in every
   endpoint that changes; none of that exists and none is needed for 1.0.
3. A's cost (two routers during the window) is paid only if a `v2` is ever
   cut. B's cost is paid on day one by every client on every request.
4. The CLI, the sidecars and the mobile apps talk to self-hosted servers at
   many versions; a path is visible in every log line, curl and proxy rule,
   a header is not.

Loser: **B**. It moves the versioning burden from the one party that can
see the whole surface at cut time (the server) to every client on every
request, and the inventory shows no client is set up to send it [A3]. The
two nginx exact-match rules on `/api/v1/...` paths [A4] would also have to
learn about the header.

Window: `/api/v1` keeps being served for at least six months after the
first release that ships `/api/v2`, and never fewer than the two minor
releases the compatibility rule already requires, whichever is later.
Removing `/api/v1` is a major release under semantic versioning.

### 2. OAuth paths

Options:

- **(i)** Fixed by the RFCs they implement and excluded from the Preloop
  version promise.
- **(ii)** Follow the REST decision (move under `/api/v1/oauth`, or version
  with `/api/v2`).

Recommendation: **(i)**.

1. `/.well-known/oauth-authorization-server` is fixed by RFC 8414 and
   `/.well-known/oauth-protected-resource` by RFC 9728; a client cannot be
   told to look elsewhere.
2. `/oauth/authorize`, `/oauth/token`, `/oauth/register` and
   `/oauth/revoke` are advertised by that metadata document, which is the
   contract; MCP clients discover them there, the CLI hardcodes them [A3].
3. Moving them would break every onboarded agent's OAuth for no gain.

Loser: **(ii)**. The RFCs already fix the discovery locations and clients
already follow them; a Preloop prefix would add nothing but a migration.

### 3. MCP and model gateway paths

Options:

- **(i)** The path is the contract and is versioned by the protocol or
  upstream API it mirrors, not by Preloop.
- **(ii)** Follow the REST decision (`/mcp/v2` and `/openai/v2` when
  `/api/v2` is cut).

Recommendation: **(i)**.

1. `/mcp` (canonical `/mcp/v1`) is written into every onboarded agent's
   config by the CLI and the sidecars [A3]; the protocol version is
   negotiated inside MCP (`protocolVersion` at `initialize`, and the
   `MCP-Protocol-Version` header in newer spec revisions), not in the path.
2. `/openai/v1`, `/anthropic/v1` and `/gemini/v1beta` mirror the upstream
   providers' public API versions; the nanobot and codex sidecars check that
   the gateway URL ends in `/openai/v1` [A3]; the Helm ingress splits
   `/openai`, `/anthropic` and `/gemini` to the gateway Service [A4].
3. A new gateway prefix appears only when the upstream adds one (for
   example `/openai/v2` beside `/openai/v1`), never because Preloop's REST
   API moved.

Loser: **(ii)**. The versions in those paths belong to other specifications;
a Preloop `v2` there would mean "the same OpenAI v1 wire format at a
different path", which helps nobody and breaks the ingress split.

### 4. Version discovery header

Options:

- **(i)** None; clients call `GET /api/v1/version`.
- **(ii)** A response header `Preloop-API-Version` on every `/api/v1/`
  response, diagnostics only, no request-side meaning.
- **(iii)** A request header that pins behaviour (option B by another
  name).

Recommendation: **(ii)**, with `Deprecation` and `Sunset` added once a
`v2` exists.

1. The value is the `info.version` of the `openapi.yaml` the server serves
   (today identical to the release version), so a reader of a proxy log or
   a support bundle can fetch the exact spec a response was produced under.
2. It discloses nothing new: `GET /api/v1/version` is unauthenticated and
   already returns `server_version` [A5].
3. Once `/api/v2` ships, `/api/v1` responses also carry `Deprecation`
   (RFC 9745) and `Sunset` (RFC 8594), which is the "response header" the
   compatibility rule already promises for runtime deprecation notice.
4. The name has no `X-` prefix (RFC 6648); the existing `X-Preloop-*`
   headers stay as they are because renaming them would be a breaking change.

Loser: **(iii)**, because it is option B and loses for the same reasons;
and **(i)**, because a second request is not available after the fact in
logs and support bundles.

## Decision

Adopt A for `/api/v1`, (i) for OAuth, (i) for MCP and the gateway, and
(ii) for discovery. In one sentence: the path is the version for
everything Preloop owns, the specification is the version for everything
Preloop mirrors, and one response header says which server answered.

The policy text that follows from this is in
[Compatibility policy, API versioning](../../compatibility.md#api-versioning).
It is marked proposed until this record is Accepted.

## Additions beyond the issue

Two points the issue did not raise, surfaced by the inventory:

- The probe endpoints `/api/v1/ping`, `/api/v1/health` and
  `/api/v1/version` are hidden from `openapi.yaml` but used by Helm probes
  and the CLI [A2]; the policy names them explicitly so they are covered.
- `Deprecation` and `Sunset` are named as the runtime deprecation headers
  for a future `v1` sunset, because the compatibility rule promises "a
  response header" without saying which.

## Consequences

If Accepted:

- `docs/compatibility.md` "API versioning" drops its proposed marker and
  becomes binding at 1.0.0.
- Follow-up issues to file and link from the 1.0.0 readiness checklist
  (#978):
    1. Emit `Preloop-API-Version` on every `/api/v1/` response (one
       middleware, one test). Small.
    2. Introduce one API prefix constant in the Go CLI (`cli/internal/api`)
       and one in the console (`frontend/src/api.ts`) so a future `v2` cut
       is a one-line change in each instead of 48 and 58 files [A3]. Small,
       mechanical, no behaviour change.
    3. Document the `/mcp` to `/mcp/v1` rewrite as stable in the MCP
       guide, and send `MCP-Protocol-Version` on the MCP transport when the
       vendored MCP library supports it. Small.
- No second router, no redirect and no pinning column are needed now.
  `/api/v2` is created only when an incompatible REST change is actually
  required.

If Rejected in favour of B:

- A pinning column on API keys and accounts, a settings surface for it, a
  header-aware cache key in every caching layer, and a client change in the
  CLI (one chokepoint), the console (two chokepoints plus raw `fetch`
  calls), each sidecar and the mobile apps before any benefit is seen.

## Appendix: inventory

All commands were run at the root of a fresh clone of `preloop/preloop` at
`67a082ee0` (main on 2026-10-10). Counts are what the commands printed.

### A1. Paths in `openapi.yaml`

```sh
$ grep -cE '^  /api/v1/' openapi.yaml
315
$ grep -cE '^  /' openapi.yaml
315
$ grep -E '^  /' openapi.yaml | grep -vE '^  /api/v1/' | wc -l
0
```

315 path keys, all under `/api/v1/`. The issue counted 253 at `aec9193`;
the tree has grown since.

### A2. Unprefixed public routes and where they are mounted

```sh
$ grep -n -E '\.include_router\(|\.mount\(|@app\.get\(' backend/preloop/api/app.py
$ grep -n 'APIRouter\|@router\.\(get\|post\)' backend/preloop/api/endpoints/oauth_server.py backend/preloop/api/endpoints/oauth_consent.py backend/preloop/api/endpoints/public_approval.py
$ grep -n '/mcp\b\|app.mount\|/mcp/v1' backend/preloop/services/mcp_http.py
```

| Surface | Path | Mount | Route definition |
|---|---|---|---|
| OAuth metadata | `GET /.well-known/oauth-authorization-server` | `backend/preloop/api/app.py:946` | `backend/preloop/api/endpoints/oauth_server.py:68` |
| OAuth metadata | `GET /.well-known/oauth-protected-resource[/{path}]` | `app.py:946` | `oauth_server.py:82`, `:98` |
| OAuth | `GET /oauth/authorize` | `app.py:946` | `oauth_server.py:114` |
| OAuth | `POST /oauth/token` | `app.py:946` | `oauth_server.py:158` |
| OAuth | `POST /oauth/register` | `app.py:946` | `oauth_server.py:605` |
| OAuth | `POST /oauth/revoke` | `app.py:946` | `oauth_server.py:734` |
| OAuth consent page (HTML) | `GET`/`POST /mcp/authorize/consent` | `app.py:940` | `backend/preloop/api/endpoints/oauth_consent.py:123`, `:218` |
| MCP | `/mcp` (mount), canonical `/mcp/v1` | `backend/preloop/services/mcp_http.py:661` | `/mcp` to `/mcp/v1` rewrite: `mcp_http.py:34-54` (`MCPPathRewriteMiddleware`, added at `app.py:1522`); `/mcp/` to `/v1`: `mcp_http.py:57-70` |
| Model gateway | `/openai/v1/*` | `app.py:847` | `backend/preloop/api/endpoints/openai_gateway.py` |
| Model gateway | `/anthropic/v1/*` | `app.py:853` | `backend/preloop/api/endpoints/anthropic_gateway.py` |
| Model gateway | `/gemini/v1beta/*` | `app.py:859` | `backend/preloop/api/endpoints/gemini_gateway.py` |
| Approval page (HTML) | `GET /approval/{request_id}` | `app.py:1315` | same |
| Approval page data | `GET /approval/{request_id}/data`, `POST /approval/{request_id}/{route}` | `app.py:990` | `backend/preloop/api/endpoints/public_approval.py:32`, `:139`, `:220` |
| Invitation page (HTML) | `GET /invitations/accept` | `app.py:1340` | same |
| API docs (HTML) | `GET /docs/api`, `GET /docs/redoc` | `app.py:1544`, `:1566` | same |
| Static assets | `/static` | `app.py:1535` | same |

All gateway routers and the OAuth routers are registered with
`include_in_schema=False`, so none of these appear in `openapi.yaml`.

Routes under `/api/v1` that are also hidden from the schema (so "the paths
in `openapi.yaml`" does not cover them today):

```sh
$ grep -n '@app.get("/api/v1' backend/preloop/api/app.py
1556:    @app.get("/api/v1/openapi.yaml", include_in_schema=False)
1557:    @app.get("/api/v1/spec", include_in_schema=False)
$ grep -n '@router\.\(get\|post\)' backend/preloop/api/endpoints/health.py backend/preloop/api/endpoints/version.py
backend/preloop/api/endpoints/health.py:17:@router.get("/ping")
backend/preloop/api/endpoints/health.py:107:@router.get("/health")
backend/preloop/api/endpoints/version.py:96:@router.get("/version", response_model=VersionInfo)
backend/preloop/api/endpoints/version.py:241:@router.get("/version/status", response_model=VersionStatus)
$ grep -n 'path: /api/v1' helm/preloop/templates/api-deployment.yaml helm/preloop/templates/gateway-deployment.yaml
helm/preloop/templates/api-deployment.yaml:386:              path: /api/v1/ping
helm/preloop/templates/api-deployment.yaml:395:              path: /api/v1/health
helm/preloop/templates/gateway-deployment.yaml:290:              path: /api/v1/ping
helm/preloop/templates/gateway-deployment.yaml:299:              path: /api/v1/health
```

Mounts: health and version at `app.py:1719` and `:1722`, features at
`app.py:993`, `openapi.json` at `app.py:1366`.

### A3. Clients that hardcode `/api/v1` (and the other prefixes)

Go CLI (`cli/`):

```sh
$ find cli/internal -name '*.go' ! -name '*_test.go' -print0 | xargs -0 grep -c '/api/v1' | awk -F: '{s+=$2} END {print s}'
154
$ grep -rl --include='*.go' '/api/v1' cli | grep -v _test.go | wc -l
48
$ grep -rn --include='*.go' -E 'const [a-zA-Z]+ = "/api/v1' cli/internal | grep -v _test.go | wc -l
5
$ grep -n 'func (c \*Client) newRequest' cli/internal/api/client.go
428:func (c *Client) newRequest(
$ find cli/internal -name '*.go' ! -name '*_test.go' -print0 | xargs -0 grep -ho -E '"[^"]*/(mcp(/v1)?|openai/v1|anthropic/v1|gemini/v1beta)(/[^"]*)?"' | sort | uniq -c | sort -rn
  16 "/mcp/v1"
   9 "/openai/v1/chat/completions"
   4 "/openai/v1"
   2 "/openai/v1/responses"
   2 "/anthropic/v1/messages"
   2 "/anthropic/v1"
   1 "/mcp"
   1 "/gemini/v1beta/models/%s:generateContent"
   1 "/gemini/v1beta/models/{model}:generateContent"
   1 "/anthropic/v1/models"
$ find cli/internal -name '*.go' ! -name '*_test.go' -print0 | xargs -0 grep -ho -E '"/(oauth/[a-z]+|\.well-known/[a-z-]+)[^"]*"' | sort | uniq -c
   1 "/oauth/authorize"
   1 "/oauth/revoke"
   3 "/oauth/token"
```

154 `/api/v1` literals in 48 non-test files; five path constants, no
shared prefix constant. Every request passes through one function
(`newRequest`, `client.go:428`) that sets headers, so sending a header
(option B) is one line; changing the prefix (a future `v2` under option A)
touches 48 files until follow-up 2 above lands.

Console (`frontend/`):

```sh
$ grep -rn '/api/v1' frontend/src --include='*.ts' --include='*.js' | grep -v '\.test\.\|\.spec\.\|__tests__' | wc -l
468
$ grep -rl '/api/v1' frontend/src --include='*.ts' --include='*.js' | grep -v '\.test\.\|\.spec\.\|__tests__' | wc -l
58
$ grep -n -E 'function fetchWithAuth|function fetchPublic' frontend/src/api.ts
528:export async function fetchWithAuth(
1090:export async function fetchPublic(
```

468 literals in 58 files, 299 of them in `api.ts`. Two header chokepoints
but many raw `fetch(` calls beside them. The console ships in the same
release as the server, so it is never version-skewed and is not a consumer
of either option; the count only measures the cost of a future prefix
change.

Runtime plugins (`runtime-plugins/`):

```sh
$ for d in runtime-plugins/*/; do n=$(grep -rn '/api/v1' "$d" | grep -v -i test | wc -l | tr -d ' '); echo "$n $d"; done | sort -rn
10 runtime-plugins/skyvern-preloop/
7 runtime-plugins/opencode-preloop/
6 runtime-plugins/hermes-preloop/
4 runtime-plugins/openclaw-preloop/
4 runtime-plugins/nanobot-preloop/
4 runtime-plugins/browser-use-preloop/
1 runtime-plugins/harness-preloop/
1 runtime-plugins/codex-preloop/
1 runtime-plugins/claude-preloop/
0 runtime-plugins/tests/
$ grep -rn -E 'openai/v1|anthropic/v1|gemini/v1beta|/mcp\b' runtime-plugins | grep -v -i test | grep -v '\.md:' | wc -l
15
```

Most sidecars receive `control_ws_url` from the server (a full URL that
already contains `/api/v1/agents/control/ws`) and derive the
permission-check URL from it (`opencode-preloop/src/index.ts:554`). The
nanobot plugin (`runtime.py:138`, `cli.py:58`, `:76`) and the skyvern
plugin (`preloop_api.py:31-34`) hardcode `/api/v1`. The nanobot plugin
(`runtime.py:366-387`) and the codex plugin (`src/employee.ts:16-23`)
assert that the gateway URL ends in `/openai/v1`; nanobot writes
`/mcp/v1` into the agent config (`runtime.py:525`). The gateway and MCP
paths are therefore already client-side contracts.

SDKs: there is no SDK directory in the repository
(`find . -maxdepth 3 -type d -iname '*sdk*'` returns nothing). The mobile
apps are proprietary and out of tree; they use `/api/v1` and read the
`clients` map from `GET /api/v1/version` for their minimum version.

Verdict on option B's cost: sending a header is cheap at the CLI and
console chokepoints, but B's real cost is server-side (per-key or
per-account pinned default, header-keyed caches, a branch per changed
endpoint) plus the long tail that bypasses those chokepoints (raw console
`fetch` calls, three sidecars, mobile apps, curl users). The inventory
does not make B cheaper than A.

### A4. Proxies and ingress route by path prefix

```sh
$ grep -n -E 'location ' helm/preloop/templates/configmap-nginx.yaml | head -12
46:        location = /api/v1/agents/permission-check {
62:        location = /api/v1/agent-deployments {
76:        location /api/ {
117:        location ^~ /approval/ {
128:        location ^~ /install/ {
139:        location ^~ /invitations/accept {
151:        location ^~ /oauth/ {
161:        location ^~ /.well-known/ {
173:        location ^~ /mcp {
200:        location ^~ /openai/ {
215:        location ^~ /anthropic/ {
230:        location ^~ /gemini/ {
$ grep -n 'tuple "/openai"' helm/preloop/templates/ingress.yaml
151:          {{- range $path := tuple "/openai" "/anthropic" "/gemini" }}
```

`/api/` is a prefix location, so `/api/v2` routes with no change. Two
exact-match locations exist for `/api/v1/...` paths (lines 46 and 62) and
would need a `v2` twin if those endpoints moved. The gateway prefixes are
what the Kubernetes ingress uses to split traffic to the gateway Service.

### A5. Existing headers and version endpoint

```sh
$ grep -rhn 'X-Preloop-[A-Za-z-]*' backend/preloop --include='*.py' | grep -v /tests/ | grep -o 'X-Preloop-[A-Za-z-]*' | sort -u | wc -l
29
$ grep -n 'ClientVersionHeader\s*=' cli/internal/version/check.go
44:const ClientVersionHeader = "X-Client-Version"
$ grep -rn -i 'mcp-protocol-version' backend/preloop cli/internal --include='*.py' --include='*.go' | grep -v test | wc -l
0
$ grep -n 'protocolVersion' backend/preloop/services/mcp_http.py
355:                "protocolVersion": "2024-11-05",
```

The 29 existing Preloop-specific headers all use the `X-Preloop-` prefix
(the most used are `X-Preloop-Session-Id`, `X-Preloop-Warning` and
`X-Preloop-Signature`). The server does not yet read or send
`MCP-Protocol-Version`; the MCP `initialize` answer states
`protocolVersion` `2024-11-05`. `GET /api/v1/version`
(`backend/preloop/api/endpoints/version.py:96`) takes no authentication
dependency and returns `server_version`.
