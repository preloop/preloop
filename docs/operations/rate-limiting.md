# Request rate limiting

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop throttles its public and authentication endpoints. Every
rule is a fixed window: at most *limit* requests per *window* seconds for one
identity. A refused request gets HTTP 429:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 42
Content-Type: application/json

{"detail": "Too many requests. Try again in 42 seconds.", "code": "rate_limited", "retry_after": 42}
```

## Rules and defaults

| Rule | Endpoints | Counted per | Default |
|---|---|---|---|
| `auth.login.ip` | `POST /api/v1/auth/token`, `POST /api/v1/auth/token/json` | client address | 10 / 60 s |
| `auth.login.user` | same | account and client address, failed attempts | 5 / 60 s |
| `auth.refresh` | `POST /api/v1/auth/refresh` | client address | 60 / 60 s |
| `auth.reset_password` | `POST /api/v1/auth/reset-password` | client address | 10 / 600 s |
| `auth.register` | `POST /api/v1/auth/register` | client address | 10 / 3600 s |
| `oauth.token` | `POST /oauth/token` | client address | 30 / 60 s |
| `oauth.register` | `POST /oauth/register` | client address | 10 / 3600 s |
| `oauth.authorize` | `GET /oauth/authorize` | client address | 60 / 60 s |
| `oauth.revoke` | `POST /oauth/revoke` | client address | 30 / 60 s |
| `approval.public.ip` | `GET /approval/{id}/data`, `POST /approval/{id}/{action}` | client address | 30 / 60 s |
| `approval.public.request` | same | approval request and client address | 10 / 60 s |
| `auth.passkey_challenge` | `POST /api/v1/auth/webauthn/register/options`, `POST /api/v1/auth/webauthn/authenticate/options` | client address¹ | 30 / 60 s (`WEBAUTHN_CHALLENGE_RATE_LIMIT`) |
| `auth.email_send` | `POST /api/v1/auth/resend-verification`, `POST /api/v1/auth/forgot-password` (one budget for both) | client address¹, and separately the target address | 3 / 900 s (`EMAIL_VERIFICATION_RESEND_LIMIT`, `EMAIL_VERIFICATION_RESEND_WINDOW`) |
| `telemetry.otlp_ingest` | `POST /api/v1/telemetry/otlp/v1/{metrics,logs,traces}` | API key | 600 / 60 s (`PRELOOP_OTLP_INGEST_RATE_LIMIT`) |
| `notifications.test_push` | `POST /api/v1/notification-preferences/me/test-push` | user | 5 / 60 s |
| `trackers.connection_test` | `POST /api/v1/trackers/test-and-list-orgs`, `POST /api/v1/trackers/list-projects-for-org`, `POST /api/v1/trackers` | user | 20 / 60 s |
| `account.deletion_request` | `POST /api/v1/account/deletion-request` | client address, and separately the signed-in account or the address a signed-out request names | 3 / 3600 s |
| `account.deletion_confirm` | `POST /api/v1/account/deletion-request/confirm` | client address | 10 / 3600 s |
| `result.validation.capability` | internal native-result validation | launch capability | 10 / 86400 s |

`auth.login.user` counts wrong-password attempts for one account (a username
and an email of the same person share it) from one client address, and is
checked before the password is verified, so guesses beyond the budget never
reach the password hash. Because the address is part of the key, someone
guessing from elsewhere cannot lock the real user out; users behind the same
NAT as the guesser can be throttled for the rest of the window. There is no
lockout beyond the window. Passkeys and single sign-on are not affected.

`auth.passkey_challenge`, `auth.email_send`, `telemetry.otlp_ingest` and
`notifications.test_push` predate the shared limiter and moved onto it with
their defaults, setting names and keys unchanged. Their original settings (in
brackets above) still set the limit; `RATE_LIMIT_<RULE>` overrides them like
any rule. `auth.email_send` charges the client address and the target address
together: a request refused on either is not charged to the other.
`account.deletion_request` charges its two identities the same way. A
signed-out request is also counted once per identity that holds the address;
when that budget is spent, no further link is mailed but the answer does not
change, so the limit does not reveal whether the address has an account.
Refusals keep each endpoint's own message in `detail`. OTLP ingest keeps the
OTLP status envelope (`{"code": 429, "message": "Rate limit exceeded"}`) with
`Retry-After`, because exporters parse that body.

¹ These two rules key on the leftmost `X-Forwarded-For` address, as before,
not on the trusted-proxy walk described below.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | `false` turns every rule off |
| `RATE_LIMIT_BACKEND` | `postgres` | `postgres` shares counters across API replicas; `memory` keeps them per process |
| `RATE_LIMIT_<RULE>` | see table | `<limit>/<seconds>`; the rule name upper-cased with dots as underscores, for example `RATE_LIMIT_AUTH_LOGIN_IP=20/60`. `0/60` turns one rule off |
| `RATE_LIMIT_TRUSTED_PROXIES` | loopback and private ranges | comma-separated CIDRs of proxies allowed to add `X-Forwarded-For` hops |
| `WEBAUTHN_CHALLENGE_RATE_LIMIT` | `30` | requests per minute for `auth.passkey_challenge` (read at startup) |
| `EMAIL_VERIFICATION_RESEND_LIMIT`, `EMAIL_VERIFICATION_RESEND_WINDOW` | `3`, `900` | budget and window in seconds for `auth.email_send` |
| `PRELOOP_OTLP_INGEST_RATE_LIMIT` | `600` | requests per minute per key for `telemetry.otlp_ingest` (read at startup) |

The Helm chart supports limiter configuration through `extraEnv`, which
reaches the API pods. There are no dedicated rate-limit values: using the
same environment settings across Helm and native deployments keeps defaults
in one place and supports new rules without a chart change. For example:

```yaml
extraEnv:
  - name: RATE_LIMIT_AUTH_LOGIN_IP
    value: "20/60"
```

### Shared counters

The `postgres` backend keeps counters in the `rate_limit_bucket` table and
updates them with one atomic statement, so N API replicas share one budget and
a restart does not reset it. Expired rows are purged by the API itself.

### When the counter store is unavailable

If the database cannot answer a limiter query, the API logs
`Rate limit store unavailable` (at most once a minute per process),
increments `preloop_rate_limit_store_errors_total{rule}` and decides as
follows:

| Endpoint class | Rules | Store unavailable |
|---|---|---|
| Password sign-in | `auth.login.ip`, `auth.login.user` | Same rule, in-process counters |
| Session and token renewal, sign-out | `auth.refresh`, `oauth.token`, `oauth.revoke` | Same rule, in-process counters |
| Account recovery and sign-up | `auth.reset_password`, `auth.register`, `auth.email_send`, `account.deletion_request`, `account.deletion_confirm` | Same rule, in-process counters |
| OAuth authorization and client registration | `oauth.authorize`, `oauth.register` | Same rule, in-process counters |
| Public approval links | `approval.public.ip`, `approval.public.request` | Same rule, in-process counters |
| Passkey challenges, telemetry ingest, test push, tracker tests | `auth.passkey_challenge`, `telemetry.otlp_ingest`, `notifications.test_push`, `trackers.connection_test` | Same rule, in-process counters |
| Second-factor proofs | the fixed MFA budgets | Refused: the proof is not accepted |

The first six classes neither fail open nor fail closed. A request is never
refused because the limiter failed, so sign-in, token renewal and approval
links stay up; it is never left unthrottled either, so a degraded database is
not a window for unlimited guessing. Each replica enforces the full budget on
its own until the store answers again, which is at most N times the budget
across N replicas.

Second-factor proofs are the exception. Their budgets are counted in the same
transaction that accepts the proof, are not configurable, and have no
in-process fallback: when the count cannot be read, the proof fails.

## Sizing the defaults

A limit that refuses ordinary use is worse than none, so the defaults leave
room for the bursts below. Every per-address rule counts all users behind one
address together; check the default against your largest shared address (an
office or VPN egress, a CI fleet's NAT gateway) before lowering it, and raise
it if that address is larger than the burst listed.

| Rule | Ordinary traffic it must not refuse | Check the default against |
|---|---|---|
| `auth.login.ip` | Password sign-ins from one address, including scripts that sign in before each step | Peak `POST /api/v1/auth/token*` per client address per minute |
| `auth.login.user` | A person mistyping a password a few times; only failures count | Failed sign-ins per account per minute |
| `auth.refresh` | Console tabs renewing a session: one request per tab when its access token expires (24 hours by default), so tabs opened in the same minute renew in the same minute a day later | Peak `POST /api/v1/auth/refresh` per client address per minute |
| `oauth.token` | `preloop auth login` (one request) and CLI or MCP client token renewal (one request per client when its access token expires) | Peak `POST /oauth/token` per client address per minute |
| `oauth.revoke` | `preloop auth logout` and MCP client disconnects: at most one per login | Keep at or above `oauth.token` |
| `oauth.authorize` | One request per CLI or MCP sign-in | Peak `GET /oauth/authorize` per client address per minute |
| `oauth.register` | One request the first time an MCP client connects | New MCP clients per address per hour |
| `approval.public.*` | An approver opening a link, reloading it and deciding; several approvers behind one address | Approval notifications delivered to one address per minute |

Requests that carry an API key, a CLI or console bearer token or a CI
identity are not counted by any of these rules: only endpoints that issue
credentials or are reachable without one have a rule. Automation that
authenticates with a key therefore cannot spend the sign-in budget of people
behind the same address.

A refused request does not end a session. The console keeps the user signed
in when `POST /api/v1/auth/refresh` answers 429 and tries again on the next
request; the CLI keeps its stored login when a renewal is refused.

### Client address and proxies

The client address is found by walking the `X-Forwarded-For` chain plus the
TCP peer from the right and skipping every hop inside
`RATE_LIMIT_TRUSTED_PROXIES`. The first address outside those ranges is the
one your own edge proxy saw; anything a client wrote into `X-Forwarded-For`
sits to its left and is never used. `X-Real-IP` is ignored.

- Helm and Docker Compose work with the default (their proxies run on
  private addresses).
- If your users also connect from private addresses (an intranet install),
  narrow `RATE_LIMIT_TRUSTED_PROXIES` to your proxies' addresses. Otherwise
  the leftmost address is used, which a client can choose.
- If your load balancer replaces `X-Forwarded-For` instead of appending to it,
  every user shares the load balancer's bucket. Configure it to append (or
  enable `use-forwarded-headers` on ingress-nginx behind a trusted L7 balancer).

## Observability

Each refusal logs `Rate limit hit: rule=... key=... retry_after=...` (the key
is a hash prefix, never the address or username) and increments the
OpenTelemetry counter `preloop_rate_limit_hits_total{rule}`. Store fallbacks
increment `preloop_rate_limit_store_errors_total{rule}`. Counters are exported
when OTLP metrics are configured.

Ingress-level limits remain a useful optional extra layer.

### Failed-login audit events

Password sign-in through both `/api/v1/auth/token` and
`/api/v1/auth/token/json` records an `auth.login.failed` event for a failed
authentication attempt with an identifiable account. Wrong passwords use
`reason_code=invalid_credentials`; the event includes the password method,
person and membership identifiers, and source IP, never the password.
Unknown identities have no tenant audit destination and produce an unscoped,
content-free authentication event in the service log instead.

The audit trail records individual outcomes, not the running
`auth.login.user` bucket count. Requests rejected by the IP or failed-login
budget before credential verification do not enter the password audit path;
those refusals appear in the limiter logs and metrics described above.
