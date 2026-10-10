# Anthropic Enterprise inference hooks

Editions: Preloop Enterprise and eligible Preloop Cloud deployments. Experimental,
disabled by default. Version review: 2026-10-10. No real Enterprise tenant or
application enforcement has been verified for this integration.

Provider-side hooks can evaluate covered corporate Claude requests even when the
client does not route through a workstation gateway. They need a separate
Anthropic Enterprise organization entitlement and vendor settings. A personal
Pro/Max subscription, Team subscription or Console/API key does not establish
Enterprise-hook coverage.

## Prerequisites and versions

- An authorized Anthropic Enterprise test organization and an **Owner or Primary
  owner** with `organization:manage`. The vendor Admin role is insufficient.
- A Preloop administrator permitted to manage integrations, a published backend
  containing the experimental adapter, and a reviewed immutable policy snapshot.
- A publicly routable HTTPS endpoint on port 443. Use its final URL: the vendor
  does not support tunnels or redirects for callbacks.
- Independent secret custody, a change owner, monitoring and a harmless pilot
  fixture set. Do not use employee prompts or production credentials.

The adapter being prepared in this source wave is not part of the last observed
published CLI release, **v0.16.0**. Before deployment record exact core, commercial
backend, frontend and app versions in the [acceptance record](anthropic-acceptance.md).
Source merges and connection probes do not establish a released or deployed
integration.

The administrator opts in on the backend with
`PRELOOP_ANTHROPIC_INFERENCE_HOOKS_ENABLED=true`; absent or false hides the
`anthropic_inference_hooks` capability and its routes. An individual integration
also starts disabled. The adapter's proposed base path is
`/api/v1/anthropic/inference-hooks`; use the deployed OpenAPI contract when
creating an integration, not a guessed CLI command.

## Configure and tune in a test organization

Use the vendor's current [configuration instructions](https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration)
and [hook overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks).
The settings below were reviewed on 2026-10-10; tenant UI validation is pending.

1. Create a disabled Preloop integration with an explicitly bound tenant and
   policy snapshot. A source label or bearer credential does not prove executable
   identity. Requests cannot nominate their own authorization scope or tenant.
2. In the Anthropic organization, use **Data/privacy > Allow for your
   organization** to unlock hook settings. This forces **Enforce verdicts** off
   initially. Enter the final public callback URL for the integration.
3. The first save reveals a signing secret once. Transfer it directly to the
   integration's encrypted secret reference. Never put it in scripts, images,
   source, tickets or a fleet package. Preloop administrative reads must not
   return it. The callback verifies signatures over the received raw bytes.
4. Use **Test connection**. This sends a signed synthetic request and accepts a
   valid allow or deny response. Record it as a connection test, not an actual
   application or policy-enforcement test.
5. Start with **Shadow mode** and compare allow/deny metadata against harmless
   cases. Shadow verdicts cannot block. Inspect latency and unknown events;
   retain content-free reason codes rather than transcripts.
6. For the reviewed enforced pilot, set **Requests inspected (%)** to **100**,
   leave **Exclusions** empty, turn **Validate tool calls** on, and choose
   **Block the request** for failure handling. Unsampled requests proceed and
   excluded custom roles are not covered. Tool validation is inactive while
   **Enforce verdicts** is off.
7. Set the vendor **Prompt verdict timeout (ms)** above the tested Preloop
   evaluation budget plus network headroom. Vendor range: 1 to 10000 ms;
   default: 5000 ms. The proposed Preloop evaluation range is 100 to 2000 ms.
   Measure the full callback path before activating enforcement.
8. Only after the pilot checklist passes, enable the integration and **Enforce
   verdicts**. Capture each effective vendor setting separately from declared
   Preloop configuration. Do not activate a customer organization as part of a
   local test.

**Allow the request is the vendor's first-save default.** It is fail-open during
errors. A policy deny is different from an endpoint failure: the adapter returns
a successful callback response containing `action: deny` for a policy rejection.
Unknown source/event/identity tests must confirm an explicit denial unless an
administrator has deliberately chosen an uninspected path. Such a path cannot
be counted as governed. Approval-required actions are denied with guidance to a
separately approved route; the hook does not mint approval grants.

## Coverage and evidence

| Surface or signal | Required control | Current evidence |
| --- | --- | --- |
| Eligible corporate Claude request covered by the vendor hook | Enterprise organization, full inspection, effective enforcement | Vendor protocol reviewed; real application pending |
| Current tool-use batch in a covered request | Effective tool validation and all-or-nothing deny before execution | Synthetic adapter tests must be recorded; app side effects pending |
| Alternate eligible corporate client | Vendor confirms that client is covered; repeat the same pilot | Pending for each client/version |
| Excluded role, sampled-out request or shadow verdict | No blocking guarantee | Explicitly outside an enforced claim |
| Personal subscription or unrelated API organization | Separate vendor/route control | Enterprise integration proves nothing about this path |
| Direct model route outside the covered vendor surface | Network/resource control and route-specific verification | No universal detection or prevention claim |
| Callback seen or successful connection test | Health and protocol observation | Does not prove complete coverage or effective vendor settings |

The vendor [availability overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks)
was reviewed on 2026-10-10. Its conversation coverage and tool limitations are
separate, and none of the named surfaces has a verified pilot result here:

| Vendor surface | Published scope | Pilot state |
| --- | --- | --- |
| claude.ai on desktop, mobile and web | Eligible Enterprise conversation requests | Pending per client/version |
| Cowork | Eligible Enterprise conversation requests; tool omissions below still apply | Pending |
| Claude Code CLI | Eligible Enterprise conversation requests, including user fix/plugin sessions and local `/code-review` | Pending; source strings do not attest executable or host |
| Claude Tag in Slack | Eligible conversation requests; post-reply measurement is excluded | Pending |
| Platform/API organizations | Outside this organization hook | Excluded |
| Amazon Bedrock and Google Cloud routes | Hook unavailable | Excluded |
| Ancillary title generation | Outside conversational enforcement | Excluded |
| Hosted Claude Security scans, hosted Code Review and smart reports | Outside this hook | Excluded; distinct from user fix/local review sessions |

**Tool validation does not cover every tool action.** The reviewed vendor
overview notes a small share of voice tool calls may be missed. Native
connector/plugin/skills/research/search, model-switch, end-chat, time and
past-chat-memory tool paths may be omitted from tool-call validation and become
visible only in a subsequent prompt. Observing that next prompt cannot prevent
a side effect that already occurred. Use independent resource authorization for
those paths and record omissions in the actual pilot rather than claiming full
tool interception from 100% prompt inspection.

Health remains **coverage unverified**, **pilot pending**, and vendor settings
**unknown** or administrator **declared** until actual evidence is recorded.
Never translate a healthy endpoint into a verified-enforcement badge. See the
vendor's [endpoint protocol](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint)
for request limits and supported event semantics. Proxy and worker resource
limits must match the tested backend rather than accepting unbounded payloads.

## Rotation, outage and recovery

Use the integration's rotation action to install a replacement signing secret
with a bounded overlap (proposed maximum 300 seconds). Coordinate the vendor
change and record a signed delivery with the new secret, rejection of the old
secret after overlap, and unchanged tenant/policy binding. A rotated secret must
not reopen disabled or revoked authority.

The vendor circuit breaker stops calling a failing endpoint and applies the
configured failure mode. Reviewed behavior: recovery probes begin after ten
minutes, approximately once per minute; a valid allow **or deny** resets it.
Settings changes after a trip cancel automatic recovery. During outage, verify
the real application's displayed error and absence of forbidden side effects.
After recovery, recheck a denied request, effective failure mode and policy
snapshot. Do not treat a connection probe as evidence that ordinary traffic has
resumed.

## Rollback

Before activation export the reviewed settings and policy reference without
secrets, identify the change owner and choose the safe failure mode. If a pilot
fails, stop expansion and remove resource access or stop affected sessions where
needed. Disabling the Preloop integration while the vendor remains fail-closed
can block covered traffic; coordinate both changes explicitly. Turning off
vendor enforcement permits traffic and is a recorded policy exception, not a
safe implicit fallback. Restore only this integration's previous settings,
revoke its signing secret when retired, and rerun the rollback case with harmless
fixtures. Preserve unrelated organization policies.

## Screenshots and next steps

No vendor screenshot is available: a designated Enterprise sandbox or agreed
pilot is awaited. Capture the effective endpoint, 100% inspection, empty exclusions,
tool validation, enforcement, failure mode, denied app action and outage/recovery
status during an authorized pilot. Redact secrets and organizational identifiers.
See [acceptance and provenance](anthropic-acceptance.md),
[self-hosted runner identity](anthropic-self-hosted-runner.md), and
[managed endpoint rollout](anthropic-managed-rollout.md).
