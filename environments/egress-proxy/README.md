# Egress proxy

Statically built forward proxy. It is the only network path a sandboxed
browser gets. Every plain HTTP request and every `CONNECT` is checked
against an origin allowlist, then the proxy resolves the name itself and
refuses private, loopback, link-local, CGNAT, ULA, multicast, and
cloud-metadata addresses. A name that is on the allowlist is still denied
when any resolved address is in those ranges (DNS rebinding).

Denied requests are not dialed. The proxy dials the address it already
checked and does not resolve that name again. It does not follow
redirects: the browser issues a new request, and that request is checked
on its own.

The image is a sidecar described by `DependencyService` (`name`,
`image@sha256`, `port`, `command`, `env`) in
`backend/preloop/services/flow_environment.py`. Publishing a digest and
registering a profile are operator steps and are not done here.

## Run

```text
EGRESS_LISTEN=:3128
EGRESS_ALLOWED_ORIGINS=https://example.com,http://fixture.example
EGRESS_DENY_PRIVATE=true
EGRESS_ALLOW_PRIVATE_CIDRS=172.20.0.0/16
EGRESS_LOG_ALLOWED=false
EGRESS_MAX_CONNS=256
```

| Variable | Default | Meaning |
|---|---|---|
| `EGRESS_LISTEN` | `:3128` | TCP listen address. |
| `EGRESS_ALLOWED_ORIGINS` | empty (deny all) | Comma-separated `scheme://host[:port]`. A host of `*.example.com` allows subdomains and not the apex. `http` implies port 80 and `https` implies port 443. |
| `EGRESS_DENY_PRIVATE` | `true` | After resolution, deny the request if any address is in the blocked ranges below. |
| `EGRESS_ALLOW_PRIVATE_CIDRS` | empty | Comma-separated CIDRs carved out of the private check (for an execution network). Logged at startup. |
| `EGRESS_LOG_ALLOWED` | `false` | When `true`, log each allowed `CONNECT` as one JSON line with `"level":"debug"`. |
| `EGRESS_MAX_CONNS` | `256` | Extra connections get HTTP 503. |

Blocked ranges when `EGRESS_DENY_PRIVATE` is true: `0.0.0.0/8`, `10.0.0.0/8`,
`100.64.0.0/10`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12`,
`192.168.0.0/16`, `224.0.0.0/4`, `169.254.169.254/32`, `::1/128`, `::/128`,
`fc00::/7`, `fe80::/10`, `ff00::/8`, `fd00:ec2::254/128`. IPv4-mapped IPv6
is checked as the embedded IPv4 address.

`GET /healthz` on the proxy itself returns `200` and `ok`. A client request
whose target is `http://egress-proxy:3128/healthz` is a proxied request and
is denied. `CONNECT` to this process's listen port is denied even when a
carve-out would otherwise allow the address.

Dial timeout is 10s. Idle timeout on accepted connections and on a
`CONNECT` tunnel is 120s. A policy denial returns `403`. A checked address
that cannot be dialed returns `502`. Both use the body
`egress_denied: <reason>` and one JSON line on stdout. When a name has
several checked addresses, each is dialed in order until one connects.

```json
{"ts":"...","method":"CONNECT","target":"evil.example:443","reason":"not_allowlisted","resolved":[]}
```

## Chromium

Point the browser at this proxy and disable its own DNS, or it will open
connections that never pass through the allowlist:

```text
--proxy-server=http://egress-proxy:3128
--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE egress-proxy"
```

Without those flags the browser can bypass the proxy. Origin enforcement
lives in this process, not in tool selection, an MCP tool allowlist, or a
Playwright `--allowed-origins` flag.

## Threat model

| Threat | What the proxy does | Test |
|---|---|---|
| DNS rebinding | Resolves the allowlisted name, denies the request when any answer is a blocked address, and dials only a checked IP. | `TestDNSRebindingPrivateAddress`, `TestMixedAnswersDenied`, `TestConnectAllowedDialsCheckedIP` |
| Redirect escape | Returns the redirect unchanged and does not fetch `Location`. The next browser request is checked again. | `TestRedirectIsNotFollowedAndNextRequestIsChecked` |
| IPv6 literals and bracketed hosts | Bracketed addresses are parsed as IPs. `::1` and IPv4-mapped metadata addresses are blocked. An unbracketed IPv6 `CONNECT` target is rejected before dial. | `TestIPv6LiteralAndBrackets` |
| `0.0.0.0` | `0.0.0.0/8` is blocked, including the unspecified address. | `TestUnspecifiedAndWeirdIPLiterals` |
| Decimal and octal IP forms | `2130706433`, `017700000001`, `0x7f000001`, `0177.0.0.1`, and `127.1` are parsed as `127.0.0.1` and blocked. An ambiguous component such as `127.0.0.08` is rejected. | `TestUnspecifiedAndWeirdIPLiterals`, `TestPolicyCanonicalForms` |
| Uppercase and percent-encoded hosts | Hosts are case-folded before the allowlist. Percent-encoded request targets are rejected by the HTTP parser (no dial). The same hosts are decoded before the allowlist and the private check when the policy sees them. | `TestUppercaseAndPercentEncodedHost` |
| `CONNECT` to the proxy's own port | Denied with `proxy_self` even if the address is in `EGRESS_ALLOW_PRIVATE_CIDRS`. | `TestConnectOwnPort` |
| Request smuggling via hop-by-hop headers | `Connection` and the headers it names, plus `Proxy-Authorization`, `Proxy-Connection`, `Keep-Alive`, and `Transfer-Encoding`, are stripped. The upstream request is one message with a single `Content-Length`. A conflicting `Transfer-Encoding` is not forwarded as a second request. | `TestHopByHopStripped`, `TestSmuggledTransferEncodingIsRejected` |
