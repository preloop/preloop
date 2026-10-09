# Account access rule storage and hooks

Core defines a closed portable access-rule schema and owns CRUD, policy snapshots, audit records and generation invalidation. Enterprise account hierarchy registers its evaluator through H4. With no registered authorizer, core authorization keeps its existing behavior.

Access rules and per-action modes round-trip through policy YAML and appear in standard policy diffs. Rule mutations are account-scoped, use optimistic rule versions and lock the account for snapshot version allocation. Team/role names resolve to IDs at write time. YAML cannot newly enable require_permit: the enterprise preview endpoint is the audited entry point for that transition.

Each account has access_rule_generation. Database triggers increment affected owner, descendant and primary-person membership generations when relevant rule, selector, membership, account or resource identity data changes. Updating ownership invalidates both previous and new owners. Committed notifications contain account IDs and generations only. Core CRUD holds dedicated LISTEN connections and returns detached selector bundles without credentials. Enterprise caches compiled bundles only while that connection is healthy, preserving warm gateway query counts.

H4 flow:run checks run before manual/matrix execution creation and before shared trigger dispatch. Runner acceptance includes the authoritative execution's flow ID for flow-tag conditions. Existing model, tool and resource-list hooks continue to impose the permission, allowed-model, tool-rule, content-policy, budget and kill-switch ceilings.
