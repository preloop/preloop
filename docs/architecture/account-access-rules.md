# Account access rule storage and hooks

Core defines a closed portable access-rule schema and owns CRUD, policy snapshots, audit records and generation invalidation. Enterprise account hierarchy registers its evaluator through H4. With no registered authorizer, core authorization keeps its existing behavior.

Access rules and per-action modes round-trip through policy YAML and appear in standard policy diffs. Rule mutations are account-scoped, use optimistic rule versions and lock the account for snapshot version allocation. Team/role names resolve to IDs at write time. YAML cannot newly enable require_permit: the enterprise preview endpoint is the audited entry point for that transition.

Each account has access_rule_generation. Database triggers increment affected owner, descendant and primary-person membership generations when relevant rule, selector, membership, account or resource identity data changes. Updating ownership invalidates both previous and new owners. Committed notifications contain account IDs and generations only. Core CRUD holds dedicated LISTEN connections and returns detached selector bundles without credentials. Enterprise caches compiled bundles only while that connection is healthy, preserving warm gateway query counts.

H4 flow:run checks run before manual/matrix execution creation and before shared trigger dispatch. Runner acceptance includes the authoritative execution's flow ID for flow-tag conditions. Existing model, tool and resource-list hooks continue to impose the permission, allowed-model, tool-rule, content-policy, budget and kill-switch ceilings.

## Resource sharing integration

The Enterprise account hierarchy provider installs H3 visibility and a transactional
materializer backed by core `crud_resource_share`. Explicit intents store all,
selected, or rule targets; tag, rule, resource, and hierarchy writes reconcile the
recipient index before commit. Request paths read receipts without traversing the
tree. Own model aliases and MCP tool names take precedence over shared resources.
Recipient API responses use `SharedResourceRead`, a closed projection without
credentials, authentication configuration, endpoints, or enrollment data. Mutations
and credential exports remain scoped to the owning account.

Shared model execution resolves owner credentials and tariffs server side while
writing usage under the consuming account. Shared MCP owner restrictions can only
tighten consumer policy, and mandatory approvals resolve the consumer's workflow.
A shared agent retains its owner identity and ceilings, with commands and runtime
keys bound to the consuming account's session. Revocation blocks new targeting;
existing session bindings remain valid through completion. The owner can inspect
its agent's consuming-session metadata at `/api/v1/agents/{id}/shared-sessions`.
Share and target changes record owner and recipient audit mirrors and policy
snapshots. The executable selector and registration belong to the Enterprise
plugin; an OSS process without that provider adds no recipients.
