# Claude Code self-hosted runner identity

Editions: Preloop Enterprise and eligible Preloop Cloud deployments. Experimental,
disabled by default. Vendor documentation reviewed 2026-10-10. Synthetic identity
verification does not authorize a live runner or establish application control.

## Choose the execution surface

| Surface | Control owner | Identity and remaining boundary |
| --- | --- | --- |
| Local Claude Code terminal | Device user and endpoint administrator | Local hooks/gateway enrollment; local administrators can change local software |
| Claude Code remote cloud session | Vendor cloud service and organization | Remote-session policy must be verified; an endpoint hook alone does not govern the cloud session |
| Self-hosted runner in an owner-controlled VM | Organization's runner operator | Vendor-issued environment/session identity plus independently isolated compute, resources and network |

A VM is owner-controlled only when the organization controls its host policy,
network and credential delivery. Moving a session onto a VM does not provide
attestation or automatically prevent token replay from another host.

## Prerequisites and release gate

The vendor's [configuration guide](https://code.claude.com/docs/en/self-hosted-environments-configuration)
describes a **Team and Enterprise beta**, with owner enablement. This is a
different entitlement from Enterprise inference hooks. Confirm availability with
the account team and use a designated test tenant. A Console/API key, API Workload
Identity Federation or a personal Pro/Max subscription is not evidence that this
seat-based remote-session path is enabled; API federation is a separate
entitlement and billing path.

The vendor's [self-hosted environment overview](https://code.claude.com/docs/en/self-hosted-environments)
states that self-hosted sessions use the same organization's Claude Code usage
as hosted sessions. Moving subscription sessions onto an owner VM does not by
itself imply API-rate billing. Preserve the documented subscription route and
confirm organization allowance/rate limits; a Console/API-key route is separate.
A Team-first managed settings/MCP pilot is described in the
[rollout decision table](anthropic-managed-rollout.md#versions-and-control-ownership).

The proposed Preloop capability is `anthropic_self_hosted_identity`, off by
default; the backend opts in with
`PRELOOP_ANTHROPIC_SELF_HOSTED_IDENTITY=true`. Administrative bindings live at
`/api/v1/anthropic-self-hosted/bindings`. Verification can be implemented before
credential exchange, but the exchange must return
`503 restricted_runtime_grant_unavailable` until the existing runtime grant
system checks policy generation, scope, expiry and revocation at every REST,
MCP and WebSocket boundary. Do not replace this gate with a broad API key.
Record exact backend/core heads and migration versions before enabling it.
These paths are source-wave contracts, not commands promised in v0.16.0. The
initial identity implementation is under commercial review, not released or
deployed. Its tested reference files are
`examples/anthropic-self-hosted/session-wrapper.py` and the accompanying README
and operator-policy template; use them from the reviewed commercial backend
version after publication.

## Bind the environment and authority

Create an explicit account-owned binding for the vendor organization,
`ccpool_` environment, approved enrollment/managed-agent policy, resource scope
and policy generation. An exchange addresses that binding's ID; a token cannot
select another tenant by nominating an organization or environment string.
Use the deployed OpenAPI schema for binding operations.

The verifier accepts the vendor's `sk-ant-cc-` ES256 session token only against
the fixed vendor JWKS origin. Issuer, audience, pool, organization,
`session_worker` role, session/subject, token ID, actor and expiry must match the
configured contract. Unknown keys, stale JWKS, unavailable identity, wrong
tenant and expired tokens fail closed. See the
[identity specification](https://code.claude.com/docs/en/self-hosted-environments-identity).

Session JWTs are **bearer credentials available to session code**. They prove
vendor-issued environment/session identity, not an executable, host state or
network origin. The vendor publishes no session-revocation feed. Derived
authority must intersect immutable allowed tools/resources, expire no later
than the upstream token, binding policy expiry or the local short lifetime,
and consult authoritative revocation/generation state on every use. A revoked
session or policy generation cannot silently mint replacement authority.

## Prepare isolated execution

Use an ephemeral VM or container per work order, a non-root session user,
read-only wrapper/policy files and a dedicated disposable workspace. Do not
mount host/container sockets, cloud credential directories or a writable
authorization policy into the worker. Disable ambient cloud identity and
metadata-service reachability; permit only reviewed private services and
dedicated egress. Destroy work-order storage and derived credentials after the
session, preserving only redacted evidence.

Keep the environment secret with the orchestrator. The vendor documents this
orchestrator command (a vendor command, not a Preloop CLI command):

```bash
claude self-hosted-runner orchestrator \
  --environment-secret-file /etc/claude/environment-secret \
  --hooks-dir /etc/claude/hooks
```

Do not run it until an authorized test tenant and isolated compute are available.
The runner's `--exec-path` selects a session wrapper. That wrapper must provision
restricted authority before exec, preserve stdin, FD3 and all arguments, and
exec the absolute binary supplied as `CLAUDE_RUNNER_CLAUDE_BIN`. Failed binding,
verification or provisioning aborts the session; there is no direct-route
fallback. Use the implementation's checked reference wrapper after publication,
not a shell fragment that prints tokens or puts them in process arguments.

Do not override `ANTHROPIC_BASE_URL` for subscription inference. The runner's
short-lived OAuth state rotates through stdin and its control descriptor;
identity tokens and ingress-token refresh are different lifecycle mechanisms.
Do not import personal OAuth custody into a fleet deployment.

## Verify network and replay boundaries

The organization's IP allowlist does **not cover self-hosted runner traffic by
default**. Obtain account-team enablement and confirm actual behavior for the
tenant. An allowlist or source IP alone also does not prove executable identity.
Private-service reachability and workload controls remain independent.

Before a pilot, run this plan with disposable test credentials and harmless
resources under explicit authorization:

1. Start one isolated work order and record redacted binding/session IDs, versions
   and expected tool/resource scope. Confirm an allowed harmless action.
2. Deny a whole tool batch and verify no file/resource mutation for any member.
   Reject unknown actor, wrong organization/environment and requested-scope
   escalation before side effects.
3. Attempt to use the same disposable bearer credential from an external network.
   Record whether the resource rejects it, and which network/workload control
   did so. A cryptographically valid token accepted off-host is a replay finding,
   not successful host authentication.
4. Revoke the binding/session before and after issuance, then attempt a fresh
   exchange and each REST/MCP/WebSocket route with the old authority. No route
   may refresh, remint or retain access after authoritative revocation.
5. Change policy generation and expiry, interrupt JWKS/provisioning connectivity,
   and confirm denial without launching the real executable or falling back.
6. Retire the work order and verify workspace deletion, resource closure and
   credential retirement. Record application observations separately from
   synthetic verifier and gateway checks.

This work does not create cloud compute, purchase a tenant or make paid model
requests. Use the [acceptance record](anthropic-acceptance.md) to leave those
results pending until authorized operators execute the plan.

## Rollback and screenshots

Disable new binding exchange, revoke active derived authority and stop affected
work orders before reverting wrapper/configuration. Remove only the dedicated
test resources and environment; preserve unrelated organization settings.
Restore the previous runner executable/wrapper from the recorded version and
rerun the harmless rollback case. A failed rollback remains an open pilot item.

No vendor environment/session screenshot or actual runner capture is available
while a designated sandbox or agreed pilot is awaited. Required captures: enabled entitlement,
environment binding, isolated work-order state, allowed/denied resource access,
external replay result, revocation and retirement. Never capture token files,
environment-secret contents, prompts or raw arguments.
