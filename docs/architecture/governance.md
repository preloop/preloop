# Subject-Scoped Governance

Governance is applied to the concrete subject using the platform, not only the parent account. This chapter covers subject-scoped configuration, tool access rules, and tool output filters.

## Subject-Scoped Governance
*   **Purpose:** Apply governance decisions against the concrete subject using the platform, not just the parent account.
*   **Scope Chain:** Resolution currently walks the active API key first, then the linked managed agent, and finally falls back to account defaults.
*   **Configuration Surface:** Subject-scoped governance can carry `allowed_models`, per-model budget metadata, ordered tool access rules, and `tool_enabled_overrides`.
*   **Enforcement Points:** The same subject context is propagated through MCP tool listing, policy evaluation, and model gateway budget checks so one runtime token sees only the intended tools and models.
*   **Primary Use Case:** Managed agent owners can grant a broad account-level tool catalog while restricting one enrolled desktop/CLI runtime to a tighter set of tools and models.

## Access Rules System

The tool configuration system has been expanded with a **ToolAccessRule** model that replaces the simpler ToolApprovalCondition approach.

**ToolAccessRule Model** (`backend/preloop/models/models/tool_access_rule.py`):

| Field | Description |
|-------|-------------|
| `action` | "allow", "deny", or "require_approval" |
| `condition_expression` | CEL expression for conditional evaluation (e.g., `args.environment == "production"`) |
| `condition_type` | "simple" or "cel" |
| `priority` | Integer for rule ordering (evaluated in priority order, first match wins) |
| `description` | Human-readable description (for deny rules, returned as denial message to the agent) |
| `is_enabled` | Toggle individual rules on/off |
| `approval_workflow_id` | Links to an ApprovalWorkflow for "require_approval" rules |

**Evaluation:** Rules are evaluated at runtime in `DynamicFastMCP._evaluate_policy()` — the first matching enabled rule determines the action. If no rules match, the tool call is allowed by default (but audited in EE).

**Access Rule API Endpoints:**
- `POST /api/v1/tool-configurations/{config_id}/access-rules` - Create access rule
- `PUT /api/v1/access-rules/{rule_id}` - Update access rule
- `DELETE /api/v1/access-rules/{rule_id}` - Delete access rule

## Tool Output Filters

Account-scoped `ToolOutputFilter` rules strip named top-level fields from MCP tool JSON results on the proxy hot path before results reach the calling agent, trimming wasted context tokens. The model, CRUD layer, and proxy application live in the OSS core; the Enterprise billing plugin exposes `/api/v1/billing/cost/output-filters` CRUD and the Console tools editor provides a filter dialog (also reachable from session-optimization suggestions). Persistent per-tool cost findings are tracked as `ToolCostFlag` rows and surfaced in the agent detail view.
