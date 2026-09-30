# Conditional Approval with CEL

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After reading this page you can write approval conditions that only trigger when a tool call's arguments match (for example, `deploy` needs approval for production but not staging, and `pay` needs approval above $1000), and test those conditions before saving them.

Both condition types are evaluated by the open-source server. Cloud and Enterprise add a syntax check that rejects invalid CEL when a policy is uploaded; on OSS, an invalid CEL expression surfaces when it is evaluated.

---

## Two Condition Types

Every condition on a tool has a `condition_type`, and this is the first thing to get right:

| `condition_type` | Edition | What it evaluates |
|------------------|---------|-------------------|
| `simple` (default in policy YAML) | All | One comparison: `args.field == 'value'`, `!=`, `>`, `<`, `>=`, `<=`, `args.field.contains('substring')` or `args.field.matches('regex')` |
| `cel` | All | Full CEL expressions: `&&`, `\|\|`, `in`, `has()`, `startsWith()`, `exists()`, and so on |

**If you omit `condition_type` in a policy file, the loader picks it.** An expression that uses CEL operators or functions (`&&`, `||`, `!`, `?`, `[`, `{`, `.contains(`, `.startsWith(`, `.matches(`, `.exists(` and similar) is stored as `cel`, even when it is marked `simple`, so a CEL rule is never silently downgraded. Setting `condition_type: cel` explicitly still documents intent:

```yaml
tools:
  - name: deploy
    source: mcp
    approval_workflow: prod-review
    conditions:
      - expression: "args.environment == 'production'"
        action: require_approval        # simple evaluator handles this fine
      - expression: "args.environment == 'production' && args.force == true"
        action: deny
        condition_type: cel             # && is CEL; the loader would infer it
```

The simple evaluator also lets you drop the `args.` prefix (`amount > 300` works), which is what the web UI condition builder generates.

---

## Policy YAML Shape

Conditions live on tools inside a policy file. The full shape:

```yaml
version: "1.0"
metadata:
  name: production-guard

approval_workflows:
  - name: prod-review
    timeout_seconds: 600
    approvals_required: 1
    approver_teams: [sre-team]

tools:
  - name: deploy
    source: mcp
    approval_workflow: prod-review
    conditions:
      - expression: "args.environment == 'production'"
        action: require_approval
```

Each condition has three fields:

| Field | Values | Description |
|-------|--------|-------------|
| `expression` | string | The condition to evaluate against tool arguments |
| `action` | `require_approval`, `deny`, `allow` | What happens when the expression matches |
| `condition_type` | `simple` (default), `cel` | Which evaluator runs the expression |

!!! warning "The only variable is `args`"
    Expressions are evaluated against the tool call's arguments, bound as `args`. There is no `user`, `tool`, `now()`, or `timestamp()`: conditions cannot reference the calling user, tool metadata, or the current time. If a decision depends on who is calling, use approval workflow routing and RBAC instead.

---

## CEL Syntax

Everything below requires `condition_type: cel`.

### Comparison Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `==` | Equals | `args.amount == 1000` |
| `!=` | Not equals | `args.environment != "dev"` |
| `>` | Greater than | `args.amount > 1000` |
| `>=` | Greater or equal | `args.amount >= 1000` |
| `<` | Less than | `args.amount < 1000` |
| `<=` | Less or equal | `args.amount <= 1000` |

### Logical Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `&&` | AND | `args.amount > 1000 && args.environment == "production"` |
| `\|\|` | OR | `args.priority == "critical" \|\| args.priority == "high"` |
| `!` | NOT | `!args.dry_run` |

### Membership Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `in` | Member of list | `args.environment in ["production", "staging"]` |
| `has()` | Has property | `has(args.rollback_on_failure)` |

---

## Common Patterns

### Environment-Based

```cel
args.environment == "production"
args.environment in ["production", "staging"]
args.environment != "dev"
```

### Amount-Based

```cel
args.amount > 1000
args.amount >= 1000 && args.amount <= 10000
args.amount > 10000 || args.currency != "USD"
```

### String Matching

```cel
args.message.contains("urgent")
args.branch.startsWith("hotfix/")
args.email.matches("@acme\\.com$")
```

### List Operations

```cel
size(args.recipients) > 0
"admin@acme.com" in args.recipients
size(args.recipients) > 10
```

### Nested Arguments

```cel
args.config.replicas > 10
args.metadata.env == "production"
```

### List Comprehensions

```cel
# Match if ANY recipient is external
args.recipients.exists(r, !r.endsWith("@acme.com"))

# Match if ALL amounts are over $100
args.items.all(i, i.amount > 100)
```

### Guarding Optional Arguments

```cel
has(args.environment) && args.environment == "production"
```

---

## Real-World Examples

### Destructive Commands Need Approval

```yaml
tools:
  - name: bash
    source: builtin
    approval_workflow: ops-review
    conditions:
      - expression: "args.command.contains('rm -rf') || args.command.contains('DROP TABLE')"
        action: require_approval
        condition_type: cel
```

### Tiered Payment Approval

Route the same tool to different workflows by amount, put each tier in its own condition and workflow:

```yaml
approval_workflows:
  - name: finance-single
    approvals_required: 1
    approver_teams: [finance]
  - name: finance-quorum
    approvals_required: 2
    approver_teams: [finance]

tools:
  - name: pay
    source: mcp
    approval_workflow: finance-single
    conditions:
      - expression: "args.amount >= 1000"
        action: require_approval
```

For the higher tier, configure a second tool configuration (or edit the condition in the UI) pointing at `finance-quorum` with `args.amount >= 10000`.

### Production Database Guard

```yaml
tools:
  - name: drop_table
    source: mcp
    approval_workflow: dba-review
    conditions:
      - expression: "args.database.startsWith('prod_')"
        action: require_approval
        condition_type: cel
```

### Bulk Email Guard

```yaml
tools:
  - name: send_email
    source: mcp
    approval_workflow: marketing-review
    conditions:
      - expression: "size(args.recipients) > 50"
        action: require_approval
        condition_type: cel
```

---

## Testing Expressions

### Web UI

1. Open the tool's configuration and go to its approval condition
2. Click **Test Expression**
3. Provide sample arguments as JSON
4. Evaluate and check the result

### API

Test an expression against sample arguments before saving it:

```bash
curl -X POST \
  "$PRELOOP_URL/api/v1/tool-configurations/$CONFIG_ID/approval-condition/test" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "expression": "args.amount > 1000",
    "sample_args": {"amount": 5000}
  }'
```

The response reports whether the expression matched:

```json
{"matches": true, "error": null, "evaluation_context": {...}}
```

### What to Test

1. **Boundary values**: exactly at thresholds (`amount == 1000`)
2. **Missing arguments**: required args absent from `sample_args`
3. **Edge cases**: empty lists, null values
4. **Type mismatches**: string where a number is expected

---

## Common Mistakes

### Using assignment instead of comparison

```cel
args.environment = "production"   // wrong: single =
args.environment == "production"  // correct
```

### Not checking if an argument exists

```cel
args.optional_field == "value"                             // errors if missing
has(args.optional_field) && args.optional_field == "value" // correct
```

### Wrong case in string comparison

Comparisons are case-sensitive. Match the exact value the tool sends: check a real tool call's arguments in the request log rather than guessing at capitalization.

### Confusing OR with a bare value

```cel
args.environment == "production" || "staging"   // wrong
args.environment in ["production", "staging"]   // correct
```

### Missing parentheses in mixed logic

```cel
// Evaluates as (a && b) || c, probably not what you meant:
args.amount > 1000 && args.environment == "production" || args.priority == "critical"

// Explicit:
args.amount > 1000 && (args.environment == "production" || args.priority == "critical")
```

### Writing CEL without `condition_type: cel`

The most common failure: an expression with `&&`, `in`, or `startsWith()` on a condition that defaults to the simple evaluator. Set `condition_type: cel`.

---

## CEL Function Reference

Available with `condition_type: cel`. All functions operate on `args` values.

### String Functions

| Function | Description | Example |
|----------|-------------|---------|
| `contains(str)` | Contains substring | `args.message.contains("urgent")` |
| `startsWith(str)` | Starts with prefix | `args.branch.startsWith("hotfix/")` |
| `endsWith(str)` | Ends with suffix | `args.email.endsWith("@acme.com")` |
| `matches(regex)` | Matches regex | `args.email.matches("^[a-z]+@")` |
| `size()` | String length | `size(args.message) > 100` |

### List Functions

| Function | Description | Example |
|----------|-------------|---------|
| `size(list)` | List length | `size(args.recipients) > 10` |
| `in` | Membership | `"admin" in args.roles` |
| `has()` | Field exists | `has(args.tags)` |
| `exists(var, cond)` | Any element matches | `args.items.exists(i, i.amount > 1000)` |
| `all(var, cond)` | All elements match | `args.items.all(i, i.verified == true)` |

### Type Conversion

| Function | Description | Example |
|----------|-------------|---------|
| `int()` | To integer | `int(args.amount) > 1000` |
| `double()` | To double | `double(args.percentage) > 0.5` |
