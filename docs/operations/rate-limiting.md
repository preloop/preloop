# Request rate limiting

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop throttles its public and authentication endpoints (EE #66). Every
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
| `approval.public.ip` | `GET /approval/{id}/data`, `POST /approval/{id}/{action}` | client address | 30 / 60 s |
| `approval.public.request` | same | approval request and client address | 10 / 60 s |

`auth.login.user` counts wrong-password attempts for one account (a username
and an email of the same person share it) from one client address, and is
checked before the password is verified, so guesses beyond the budget never
reach the password hash. Because the address is part of the key, someone
guessing from elsewhere cannot lock the real user out; users behind the same
NAT as the guesser can be throttled for the rest of the window. There is no
lockout beyond the window. Passkeys and single sign-on are not affected.

Forgot-password, verification resend, passkey challenges and OTLP ingest keep
their existing limits and settings.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | `false` turns every rule off |
| `RATE_LIMIT_BACKEND` | `postgres` | `postgres` shares counters across API replicas; `memory` keeps them per process |
| `RATE_LIMIT_<RULE>` | see table | `<limit>/<seconds>`; the rule name upper-cased with dots as underscores, for example `RATE_LIMIT_AUTH_LOGIN_IP=20/60`. `0/60` turns one rule off |
| `RATE_LIMIT_TRUSTED_PROXIES` | loopback and private ranges | comma-separated CIDRs of proxies allowed to add `X-Forwarded-For` hops |

With the Helm chart, set these through `extraEnv`, for example:

```yaml
extraEnv:
  - name: RATE_LIMIT_AUTH_LOGIN_IP
    value: "20/60"
```

### Shared counters

The `postgres` backend keeps counters in the `rate_limit_bucket` table and
updates them with one atomic statement, so N API replicas share one budget and
a restart does not reset it. Expired rows are purged by the API itself.

If the database cannot answer a limiter query, the API logs
`Rate limit store unavailable` (at most once a minute per process) and applies
the same rule with in-process counters. Requests are not refused because the
limiter failed, and they are not left unthrottled either.

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
