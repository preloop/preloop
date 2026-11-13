# Browser access for flow agents

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This page says what enforces network access for a flow agent that drives a
browser, and what does not. The profile that installs the browser is
`preloop-browser` in
[Execution environments and checkpoint recovery](environments-and-recovery.md).
The MCP endpoint documented in [MCP Tool Integration](../../architecture/mcp.md)
is a different path: it is not where these origins are enforced.

## What a flow agent gets

`environments/preloop/browser/enable.sh` registers a `browser` MCP server on
the harness and starts `@playwright/mcp@0.0.82` from the image
(`/opt/preloop-env-tools/node_modules/.bin/playwright-mcp`) with `--isolated`
and `--headless`. The rendered config
(`environments/preloop/browser/playwright-mcp.config.json`) sets
`"isolated": true`. Chromium therefore starts with an empty profile: no
operator cookies and no operator storage state.

The server runs inside the execution container. When the execution ends, that
process is killed with the container. The egress proxy is a sidecar of the
same execution and is removed with it; see [Docker vs Kubernetes](#docker-vs-kubernetes).

## Where origins are enforced

Origin enforcement is the egress proxy in `environments/egress-proxy/`. Every
plain HTTP request and every `CONNECT` is checked against
`EGRESS_ALLOWED_ORIGINS`. The proxy then resolves the name itself and denies
the request when any answer is private, loopback, link-local, CGNAT, ULA,
multicast, or a cloud-metadata address. A name that is on the allowlist is
still denied in that case (DNS rebinding). Denied targets are not dialed.
The proxy dials an address it already checked.

`environments/egress-proxy/README.md` is the contract. The checks live in
`environments/egress-proxy/proxy/config.go` and
`environments/egress-proxy/proxy/proxy_test.go`:

- Allowlist: `TestEmptyAllowlistDenies`, `TestConnectNotAllowlistedLogsDenial`.
- Private-range denial after resolution, including DNS rebinding:
  `TestDNSRebindingPrivateAddress`, `TestMixedAnswersDenied`,
  `TestConnectAllowedDialsCheckedIP`.
- Redirects are not followed. The proxy returns the redirect and does not
  fetch `Location`. The browser's next request is a new proxied request and
  is checked again: `TestRedirectIsNotFollowedAndNextRequestIsChecked`.

Two Chromium flags make a bypass of that proxy impossible. The proxy README
requires both:

```text
--proxy-server=http://egress-proxy:3128
--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE egress-proxy
```

Without them the browser can open connections that never pass the allowlist.
`enable.sh` renders those flags, plus `--proxy-bypass-list=<-loopback>` so
loopback is not a path around the proxy. `environments/preloop/browser/selfcheck.sh`
refuses to launch unless all three are present, then probes
`http://169.254.169.254/`, `https://example.org/`, and a listener on
`127.0.0.1`. The metadata probe is expected to fail before a dial:
`--host-resolver-rules` maps that literal to `~NOTFOUND`, so
`classify()` accepts `ERR_NAME_NOT_RESOLVED` or
`chrome-error://chromewebdata` when no response committed. The hostname
and loopback probes must fail with `egress_denied` or a Chromium
proxy/tunnel error. Any other outcome, including a missing proxy variable,
exits `browser_egress_not_enforced` and aborts profile setup.

## What is not enforcement

Playwright MCP's `--allowed-origins` flag is not a network boundary. The
README for `@playwright/mcp@0.0.82` describes it as:

> semicolon-separated list of TRUSTED origins to allow the browser to
> request. Default is to allow all. Important: *does not* serve as a
> security boundary and *does not* affect redirects.

The same README says Playwright MCP is not a security boundary.

Preloop tool permissions are not a network boundary either. `allowed_mcp_tools`
chooses which tool names a runtime credential can see
([MCP Tool Integration](../../architecture/mcp.md)). A `tool_access_rules` row
can match call arguments with a CEL expression (`condition_type = cel`), for
example `args.url.matches('https://example.com/.*')`.
`backend/preloop/services/policy_evaluator.py` (`_evaluate_cel_condition`)
evaluates that expression and returns allow, deny, or require approval. The
decision is useful for approvals and audit. It does not open or close a
socket, and it does not see traffic the browser sends after the tool call.

## Operator responsibilities

Register the proxy image and any fixture image by digest. `DependencyService`
in `backend/preloop/services/flow_environment.py` accepts only
`name@sha256:<64 hex digits>`. Publishing a digest and registering the
profile are operator steps; the proxy image does not register itself
(`environments/egress-proxy/README.md`).

Choose `EGRESS_ALLOWED_ORIGINS` on the profile's `egress-proxy` service
`env`. `DependencyService.env` is fixed when the profile is registered. A
flow only selects the profile id, so the allowlist cannot be copied from
`agent_config` (`backend/preloop/services/flow_environment.py`). The example
profile allows `http://fixture-site:8080`
(`environments/preloop/profile.json.example`).

`EGRESS_ALLOW_PRIVATE_CIDRS` is the carve-out for the execution network
(the example uses `172.20.0.0/16`). Addresses in that list skip the private
check. The proxy still denies `CONNECT` to its own listen port
(`TestConnectOwnPort`, `TestAllowPrivateCIDR`). Leave
`EGRESS_DENY_PRIVATE` at its default, `true`, unless you intend to turn the
private check off.

On Kubernetes this stacks with the agent-execution NetworkPolicy
(`helm/preloop/templates/agent-networkpolicy.yaml`). The internet egress
rule excludes cluster CIDRs and `169.254.169.254` (the chart falls back to
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, and `169.254.169.254/32`).
Docker has no equivalent policy. There the proxy is the only boundary.

## How test credentials enter

Put synthetic values in the profile `env` or the service `env`. Do not put
operator cookies or a signed-in browser profile in the execution. `--isolated`
starts Chromium with an empty profile, and nothing in `enable.sh` copies a
host browser state.

`environments/fixture-site/` is a generic site to test against, with
synthetic credentials only (`jane@example.com` / `fixture-pass`). The example
profile runs it as the `fixture-site` sidecar on port 8080. Besides the login
pages it has `/escape` (a redirect to `169.254.169.254`), `/offsite` (a
redirect to `https://example.org/`) and `/slow` (answers after 30 seconds).

`scripts/tests/browser_fixture_flow.py` is a model-free QA flow over that
site. It starts the browser MCP server from the harness config `enable.sh`
wrote, signs in, checks that "Welcome, Jane Doe" is visible, then tries a
non-allowlisted origin, the metadata redirect and a navigation past a 5 second
tool timeout, and checks that no Chromium process is left after the server
closes. It writes `result.json` (`url`, `pass`, up to five `notes`,
`checked_at`), capped at 4 KiB with the password redacted. The flow only
reports what the browser saw; the caller checks the proxy log for the
matching denials. Run it on the host with
`pytest backend/tests/services/test_browser_fixture_flow.py`, or in a
Docker-hosted executor with
`scripts/tests/flow_environment_integration.py --browser`.

## Docker vs Kubernetes

`profile_env` and `profile_setup_shell` in
`backend/preloop/services/flow_environment.py` rewrite
`${service.<name>}` to the service name on Docker and to `127.0.0.1` on
Kubernetes. Docker attaches the sidecar to a per-execution network with a
DNS alias (`backend/preloop/agents/container.py`,
`_prepare_environment_services`), so the proxy is `http://egress-proxy:3128`.
Kubernetes runs the proxy as a native sidecar in the execution pod
(`_environment_sidecars`), sharing the pod network namespace, so the proxy
is `http://127.0.0.1:3128`. `PRELOOP_BROWSER_PROXY` on the example profile
uses the `${service.egress-proxy}` placeholder and follows that rewrite.

Sidecar lifetime matches the execution. On Docker, `ContainerExecutor.aclose`
force-deletes the dependency containers and the execution network. On
Kubernetes the sidecar is an init container with `restartPolicy: Always`,
so it stops when the pod stops.

## Limits and follow-ups

The sandbox `browser` server is not routed through the Preloop MCP firewall.
`backend/preloop/services/dynamic_fastmcp.py` derives `browser_step` rows
only for Playwright tools that pass through that firewall
(`_persist_playwright_browser_step`, gated by
`mcp_playwright_derive_browser_steps`). Flow agents therefore do not get
`browser_step` timeline rows from
[issue #885](https://github.com/preloop/preloop/issues/885) for this browser.
The control plane cannot sit on the execution network today.

Resuming a browser session or a harness transcript across executions is out
of scope. `--isolated` does not keep cookies or storage for the next run.
