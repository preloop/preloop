# Claude apps gateway and Claude Desktop routing: end-to-end harness

Test-only harness for #1411. It proves the two routes that send Claude
Desktop and Claude Code model traffic through Preloop, and records what
actually arrives at Preloop:

- **Route G**: Claude Code (or Desktop via `bootstrapUrl`) signs in to a
  Claude apps gateway, which forwards inference to Preloop as a
  `provider: anthropic` upstream with `forward_user_identity: true`.
- **Route D**: Desktop's `inferenceProvider: gateway` pointed straight at
  Preloop (`desktop-direct.sh` sends a Desktop-shaped request).

Nothing here talks to a real IdP, a real model provider or any shared
Preloop. The model behind Preloop is a stub (`stub/harness_server.py`).

## Layout

```
Claude Code (client container, throwaway HOME)
   | /login device flow, bearer token
   v
claude gateway (gateway/gateway.yaml) --OIDC--> Dex (dex/config.yaml, 2 users)
   |  upstream "preloop": x-api-key = trusted upstream key,
   |  identity headers, x-preloop-upstream-secret
   v
recorder (records header names)  -->  Preloop (built from this checkout)  -->  stub-model
   upstream "spare" (counts requests; must stay at 0 after a 429)
```

| Service | What | Host port (loopback only) |
| - | - | - |
| `preloop` | Preloop built from `../..` (API and model gateway) | 18900 (`HARNESS_PRELOOP_PORT`) |
| `claude-gateway` | `claude gateway --config gateway.yaml` | 8080 |
| `client` | Claude Code, shares the gateway's network namespace so the gateway is `http://localhost:8080` (Claude Code accepts plain http only for loopback) | none |
| `dex` | OIDC IdP, users `alice@example.com` and `bob@example.com`, password `password` | none |
| `recorder` / `spare` / `stub-model` | test doubles | 19000 / 19100 / none |

## Run

```bash
cd examples/claude-apps-gateway
cp .env.example .env            # verify.sh does this when .env is missing
PRELOOP_DISABLE_TELEMETRY=true ./verify.sh
./desktop-direct.sh
docker compose down -v
```

`verify.sh` builds and starts the stack (`docker compose up --wait`), seeds
Preloop (`seed/seed.py`: account, users, stub-backed models, a normal key and
a trusted upstream key with scope `model_gateway:trusted_upstream`, an
upstream secret hash and a `per_subject_budget`), runs the steps and writes
`runs/<timestamp>/report.md` with versions, results and the observed header
names. Steps 2 to 4 assert on Preloop API responses
(`/api/v1/account/gateway-usage/search`, `/api/v1/budget/policies`), not logs.

| Step | What it proves |
| - | - |
| 1 | Real Claude Code `/login` with `forceLoginMethod: gateway` and `forceLoginGatewayUrl` (written to the client container's own managed-settings.json), completed through Dex, in a throwaway HOME |
| 2 | One `claude -p` reaches Preloop; usage row has `gateway_source=claude_apps_gateway`, subject email and id, `client` |
| 3 | A tiny `gateway_subject` budget for bob makes the gateway return `429`, integer `retry-after`, `error.type=billing_error`, and the second upstream gets nothing |
| 4 | Forged `x-claude-gateway-user-*` headers on a normal key are ignored; a wrong `x-preloop-upstream-secret` on the trusted key is `401` |
| 5 | Lists every request header name Preloop received from the gateway |
| 6 | Rollback: revoking the key makes the gateway fail over (documented on 401); a gateway config without the Preloop upstream sends Preloop nothing |

Checks that need the #1409 backend contract report **PENDING** (not FAIL)
when Preloop does not serve `GET /anthropic/v1/models`. `STRICT=1` turns
PENDING into a failure.

The real Claude Desktop app is covered by the manual
[DESKTOP-CHECKLIST.md](DESKTOP-CHECKLIST.md).

## Versions tested

| Component | Version |
| - | - |
| Claude Code (gateway server and client) | 2.1.288 (`CLAUDE_CODE_VERSION` in `.env`; `headers:` needs 2.1.277+) |
| Dex | v2.41.1 |
| Gateway Postgres | 16 |
| Preloop | the checkout the harness is built from (the run report records the commit) |

## Observed behaviour (gateway 2.1.288)

- Header names Preloop receives from the gateway on `/v1/messages`:
  `accept, accept-encoding, anthropic-beta, anthropic-version, connection,
  content-length, content-type, host, user-agent, x-api-key,
  x-claude-gateway-user-email, x-claude-gateway-user-id,
  x-litellm-end-user-id, x-preloop-upstream-secret, x-stainless-*`.
- `x-claude-code-session-id` is **not** forwarded.
- `user-agent` **is** forwarded as the client's value with a gateway suffix,
  for example `claude-cli/2.1.288 (external, sdk-cli) cc-gateway/2.1.288`.
- The gateway rewrites a bare model id to its dated id before relaying
  (`claude-sonnet-4-5` becomes `claude-sonnet-4-5-20250929`); Preloop needs
  a model alias for the dated id, or it answers 404 and the gateway fails
  over to the next upstream without telling the developer.
- Desktop direct mode: base URL `<preloop>/anthropic` works;
  `<preloop>/anthropic/v1` produces `/anthropic/v1/v1/messages` (404).

## CI

Local-only. The `claude` binary installs in a container, but step 1 needs
the interactive `/login` TUI driven through a pseudo-terminal and a full
Preloop image build, and the gateway docs state there is no unattended
sign-in flow for CI. The harness is not wired into CI; run it locally.

## Safety

- The `claude` binary exists only inside the `preloop-cag-claude` image.
- Every Claude Code invocation uses a throwaway `HOME` inside the `client`
  container; `/etc/claude-code/managed-settings.json` is written inside that
  disposable container only.
- The harness never reads or writes the host's `~/.claude*`,
  `/Library/Managed Preferences`, the Windows registry or
  `/etc/claude-desktop`.
- All secrets are placeholders from `.env.example`.
