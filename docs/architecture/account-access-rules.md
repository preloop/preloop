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

Flow definitions bind global or account-owned AI models. Shared model visibility
and gateway invocation do not authorize a flow model binding. Tool configuration
references to MCP servers and managed agents also remain owner-scoped; visibility
of a shared resource does not grant ownership of its configuration.

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

## Exact-resource data contract

`ResourceSelector.ids` is an optional list of at most 100 distinct UUIDs. A
nonempty list requires `AccessRuleDefinition.resource_type`; IDs, tags and
account selectors intersect. Core validates exact IDs against owner-local CRUD
models, including subject IDs and existing team/role reference resolution.
Legacy rules without IDs retain their syntax. Names and mutable tags never
serve as resource identity.

`crud_access_rule.resource_access(account_id, resource_type, resource_id)` returns
`ResourceAccessDocument`: canonical owner/type/UUID, local rule definitions,
requirements, document `version` (initially zero) and account `generation`.
`write_resource_access` accepts the same identity, `ResourceAccessUpdate`, both
`expected_version` and `expected_generation`, the actor and preview impact
`losing_subjects`. Its caller must authenticate and validate the preview; this
CRUD layer does not grant permission to call it. It locks the owner, validates
all references and atomically replaces only that resource's local permits and
requirements, snapshots and audits. Audit details include explicit before/after
resource documents and the supplied losing-subject set. Unrelated metadata, rules, account modes
and ancestor policies remain intact. Empty updates remove the local rules and
gates together, retaining a version tombstone. Generic rule writes/deletes
reject managed local rules; use the resource document to edit them.

Account metadata stores `resource_access` as typed `ResourceAccessPolicy`
objects plus an internal `version`. The portable policy field excludes that
storage version. Each object has `resource_type`, `resource_id`, `rule_ids` and
`requirements`. Every requirement has an existing `action`, the sole explicit
mode `require_permit` and `permit_rule_ids`. An absent action inherits. An empty
permit list with an explicit requirement allows no subjects through that gate.
Selecting a team without a requirement leaves the resource inheriting.

```yaml
resource_access:
  - resource_type: ai_model
    resource_id: 11111111-1111-4111-8111-111111111111
    rule_ids: [22222222-2222-4222-8222-222222222222]
    requirements:
      - action: model:invoke
        mode: require_permit
        permit_rule_ids: [22222222-2222-4222-8222-222222222222]
```

The linked access rule must be an enabled `permit`, `scope: self`, with
`resource_type: ai_model`, `resource_selector.ids` containing exactly that
resource UUID, and actions including `model:invoke`. Tags, conditions and
account selectors may further narrow it. Local IDs cannot reference broad,
foreign, disabled, forbidden or mismatched rules or another resource's permits.
Supported execution pairs are `model:invoke`/`ai_model`, `tool:call`/`mcp_tool`,
`flow:run`/`flow` and `runner:accept`/`runner`. `resource:view` supports canonical
CRUD-backed resource types; `resource:share` additionally requires a shareable
type. `runner_pool` has no canonical model and is rejected for exact IDs.

The H4 `resource:view` callers cover owner/shared details and their read-only
model usage/session/interaction/pricing and credential marker/export reads,
MCP tool inventory, flow usage/governance, and agent shared sessions, bindings,
governance, credentials and enrollments, as well as the normal inventories,
model overview and agent control inventory. Managed-agent owner checks receive
the same raw summary attributes on list and detail paths. No authorizer means these checks preserve OSS behavior.

Bundles include `account_modes`, keyed by source account UUID, so each ancestor
and local `require_permit` gate can require an ordinary permit from that source.
The merged `modes` and `ancestor_require_permit` fields remain for compatibility.
Bundles include `resource_access` with explicit `account_id` provenance for
hierarchy owners. Managed local permits are excluded from ordinary `rules` and
carried separately in `resource_access_rules` with the same owner provenance,
including ancestor-local `scope: self` rules. This keeps local allowlists from
satisfying account or ancestor permit gates, and keeps every binding resolvable. Enterprise must evaluate every applicable local requirement
as an independent **AND** ceiling, satisfied only by its designated
`permit_rule_ids` for the exact owner/type/UUID/action. Broad tag/account permits
cannot satisfy it. Account and ancestor gates, forbids and existing permission
ceilings still apply separately. Core ships storage and validation only; it
does not advertise usable resource restrictions or install an evaluator.

Requirements round-trip through export, snapshots and policy diffs. YAML cannot
activate or tighten requirements, change the designated links of an active gate,
or edit permits supporting an active local gate: use the validated
preview/application path. YAML may remove a requirement to restore inheritance;
retaining a document's local permits after that removal does not leave a gate.
Removing a whole document requires removing its managed permits too. Omitted `resource_access`
preserves bindings; removing a document must also remove its managed permits,
and replacing rules cannot leave dangling bindings. Metadata and rule triggers
invalidate generations only on commit; rollback preserves the previous bundle.

Resource deletion cleanup is registered by the core CRUD package for ORM and
bulk ORM deletes. Before commit it removes the deleted typed resource's local
document and exact permits, snapshots/audits the cleanup, and removes deleted
IDs from ordinary multi-ID rules (deleting a rule when its last ID disappears).
Cleanup shares the deletion transaction and rolls back with it; it never turns
an emptied selector into a broad permit or leaves stale restrictions blocking
unrelated resource edits and policy roundtrips.
