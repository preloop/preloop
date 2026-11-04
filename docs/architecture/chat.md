# Chat Connections

How chat providers are set up and how authenticated chat operations reach Preloop. User-facing setup is in the [chat connections guide](../chat-connections.md).

## Authenticated chat operations

Chat connections authenticate provider ingress before persisting a tenant-owned
receipt. Expiring single-use proofs bind provider users to Preloop users. The
separate chat worker consumes a leased PostgreSQL ingress/outbox, delegates a
fixed read-tool registry and explicit human commands to existing authorized APIs,
and routes the account default model through the existing gateway. Protected
replies revalidate actor, permissions, resource access, and scoped read snapshots
before private provider delivery. Ambiguous writes become observable uncertain
work rather than automatic duplicate operations. Helm installs can enable the
separate Deployment with `chatWorker.enabled`; it shares API credentials and
operator network/CA settings, while provider health remains connection-specific. See
[Chat connections](../chat-connections.md) for setup and operational limits.

The public `/api/v1/features` payload reports an explicit `oss`, `cloud`, or
`enterprise` edition and the running backend `server_version`. Hosted instances
are Cloud; self-hosted proprietary installations are Enterprise; other installs
are OSS. Runtime deployment detection wins over static plugin declarations; plugin counts
never determine the edition.
Feature flags remain the authority for individual capability gates. The console
reuses its cached features payload for the header help menu; documentation,
release notes, and issue/support destinations come from brand URL configuration.

Provider inference-hook setup and health use an OSS Lit lazy view gated by
`anthropic_inference_hooks` and the existing `manage_policies` permission.
Commercial plugins own tenant authorization, encrypted secret custody and
callback evaluation. The console accepts write-only secrets, clears them after
submission/disconnection, and preserves unverified coverage and pending pilot
states even when callbacks are observed. It creates no grants or policy DSL.

The weekly governance review's policy document, version history and review
settings render as a "Governance policy" tab on the Policies page, an OSS Lit
component gated by the `governance_review` capability. `view_policies` reads,
`manage_policies` edits; the extension plugin owns storage and rule compilation.

## Chat setup and optional Cloud support

Chat provider discovery uses encrypted actor/account-bound expiring drafts. IDs
reserve stable callback URLs before installation; completion rechecks management
permission and provider identity/workspace. Mattermost DM listeners use database
leases and canonical post/channel/member lookups. HTTP and sockets pin approved
DNS addresses and share the instance outbound CIDR policy. Disconnected commands
are not replayed. Accepted events use the existing durable ChatWork queue.

Core SupportConversation/Message/Outbox/Quota models and CRUD provide generic
ordered support persistence. Ownership generations fence both staged browser
answers and outbox delivery. Cloud-only retrieval, independent gateway funding,
scoped diagnostics and the private Mattermost operator bridge are supplied by
an EE plugin. The Lit widget/operator view are lazy capability-gated OSS code;
public landing content remains in the existing light DOM. See chat-connections.md
and the EE Cloud support operator guide for configuration and recovery policies.
