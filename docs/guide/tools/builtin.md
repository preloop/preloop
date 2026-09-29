# Built-in Tools

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Call Preloop's built-in MCP tools from any connected agent, and gate them with access rules and approval workflows. This page is the argument reference for every built-in tool.

---

## Overview

Preloop provides two categories of built-in tools:

1. **Always-Available Tools**: `request_approval`, `ask_user`, and `get_approval_status` work immediately
2. **Tracker-Dependent Tools**: issue, comment, and pull-request tools appear only when a tracker (GitHub, GitLab, or Jira) is connected; the pull-request and comment-editing tools additionally require a GitHub or GitLab tracker

All built-in tools return **strings**. Tracker tools return the result serialized as JSON; the "Returns" blocks below are illustrative examples of that JSON, not a separate schema.

All built-in tools can be protected by the Safety Layer with access rules and approval workflows.

### Access Rules & Tool Configuration

Each tool supports multiple **access rules** with fine-grained control:

- **Actions**: `allow`, `deny`, or `require_approval` decides whether a tool call proceeds, is blocked, or needs human approval
- **CEL conditions**: Match rule application to specific argument values (e.g. production vs staging, high-value transactions)
- **Priority ordering**: Rules are evaluated in priority order (lowest priority value first); the first matching rule applies

See [CEL Expressions](../approvals/cel-expressions.md) for condition syntax and examples.

---

## Always-Available Tools

These tools work immediately without any setup.

### `request_approval`

Request approval for any action with custom context.

**Purpose:** Let agents explicitly request approval before risky operations

**Arguments:**

- `operation` (string, required) - Description of the operation requiring approval
- `context` (string, required) - Additional context about the situation
- `reasoning` (string, required) - Explanation of why this operation is needed
- `caller` (string, optional) - Name of the agent or flow requesting approval (auto-populated if not specified)
- `approval_workflow` (string, optional) - Name of the approval workflow to use (defaults to the account default)

**Returns:** a string describing the decision, e.g. `Approval granted for operation: ...` on approval, or `Approval denied: ...` with the reason.

**Example:**

```python
result = request_approval(
    operation="Deploy version 2.3.0 to production",
    context="Changes: new API endpoints, fix for bug #123",
    reasoning="Release window closes at 18:00 UTC",
)

if result.startswith("Approval granted"):
    deploy(version="2.3.0", env="production")
```

**When to use:**

- Agent needs to decide when to request approval
- Want to provide rich context about WHY approval is needed
- Multi-step workflows where timing matters

**Difference from policy-gated tools:**

- Policy-gated tools: Automatic interception
- `request_approval`: Explicit agent decision

See [MCP Tool Integration](../../architecture/mcp.md) for details.

---

### `ask_user`

Ask the human a question and wait for their answer, with multiple-choice
options, a free-text reply, or both. Where `request_approval` is a yes/no gate,
`ask_user` returns the human's actual answer so the agent can act on it.

**Purpose:** Let an agent pause and ask the operator to choose between options
or provide input, routed to the same human approval surfaces (mobile, watch,
Slack, email, console).

**Arguments:**

- `question` (string, required): the question to ask
- `options` (string[], optional): answer options to offer as tappable buttons
- `allow_free_text` (boolean, optional, default `true`): let the user type a
  free-text answer
- `context` (string, optional): extra context shown to the human
- `approval_workflow` (string, optional): name of the approval workflow to
  route the question to (defaults to the account default)

**Returns:** a string containing the user's answer, e.g. `User answered:
production`, or `No answer provided.` if the human dismissed the question.

**Example:**

```python
answer = ask_user(
    question="Which environment should I deploy to?",
    options=["staging", "production"],
    allow_free_text=False,
)
# answer -> "User answered: staging"
```

The operator sees the question with one button per option (plus a text field
when `allow_free_text` is true) in the mobile and watch apps; the chosen option
or typed text is returned to the agent. Answers flow through the same audit
trail as approvals.

**When to use:**

- The agent needs a decision *between options*, not just allow/deny
- The agent needs a value from the human (a name, an id, a short instruction)

---

### `get_approval_status`

Check the status of a pending approval request. Used with [async approvals](../approvals/async-approvals.md): when a gated tool returns a `pending_approval` response, poll this tool until the request resolves.

**Arguments:**

- `request_id` (string, required) - ID of the approval request to check

**Returns:** a JSON string with the request status (`approved`, `declined`, `expired`, or still pending), an event log of notifications, votes, and escalations, and the final tool result once approved.

**Example:**

```python
status = get_approval_status(request_id="req_abc123")
# Poll periodically (e.g. every 15 seconds) until resolved
```

---

## Tracker-Dependent Tools

These tools only appear when you've connected at least one tracker (GitHub, GitLab, or Jira).

### Why Tracker-Dependent?

Built-in issue tools operate on tracker data:

- No tracker = No issues to query
- Connect GitHub → tools work with GitHub issues
- Connect Jira → tools work with Jira tickets

The pull-request tools (`get_pull_request`, `update_pull_request`, `create_pull_request`) and `update_comment` require a **GitHub or GitLab** tracker specifically.

Connect GitHub, GitLab, or Jira from the console before using tracker-dependent built-in tools.

---

## Issue Management Tools

### `get_issue`

Fetch full details for a single issue.

**Arguments:**

- `issue` (string, required) - Issue identifier: URL, key, or ID (e.g., "PROJ-123", "https://github.com/acme/repo/issues/456")

**Returns (illustrative):**

```json
{
  "id": "PROJ-123",
  "title": "Fix login bug",
  "description": "Users unable to login...",
  "status": "in_progress",
  "priority": "high",
  "assignee": "alice@acme.com",
  "labels": ["bug", "security"]
}
```

**Example:**

```python
issue = get_issue(issue="PROJ-123")
```

**When to preloop:**

- ⚪ Usually NOT needed (read-only operation)
- ✅ Preloop if reading sensitive/confidential issues

---

### `create_issue`

Create a new issue in a connected tracker.

**Arguments:**

- `project` (string, required) - Project/repo to create the issue in
- `title` (string, required) - Issue title
- `description` (string, required) - Detailed description
- `labels` (string[], optional) - Labels/tags
- `assignee` (string, optional) - Assign to user
- `priority` (string, optional) - Priority level
- `status` (string, optional) - Initial status

**Returns (illustrative):**

```json
{
  "id": "PROJ-124",
  "url": "https://github.com/acme/repo/issues/124"
}
```

**Example:**

```python
issue = create_issue(
    project="acme/repo",
    title="Add user authentication",
    description="Implement OAuth2 authentication",
    priority="high",
    labels=["feature", "security"],
    assignee="bob@acme.com"
)
```

**When to preloop:**

- ✅ **Always** - Creating issues can spam your tracker
- ✅ Especially for bulk operations
- ✅ When AI agents create issues automatically

**Recommended policy:**

```yaml
approval_workflows:
  - name: "tracker-writes"
    approvals_required: 1
    approver_users: [team_lead]

tools:
  - name: create_issue
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: "true"
        action: require_approval
        condition_type: cel
        description: "All issue creation needs approval"
```

---

### `update_issue`

Modify an existing issue.

**Arguments:**

- `issue` (string, required) - Issue to update (URL, key, or ID)
- `title` (string, optional) - New title
- `description` (string, optional) - New description
- `status` (string, optional) - New status
- `priority` (string, optional) - New priority
- `assignee` (string, optional) - Reassign
- `labels` (string[], optional) - Update labels

**Example:**

```python
result = update_issue(
    issue="PROJ-123",
    status="closed",
    priority="low"
)
```

**When to preloop:**

- ✅ **High priority/critical issues** - Require approval for status changes
- ✅ **Bulk operations** - Updating many issues at once
- ⚪ Low priority issues - Usually safe without approval

**Recommended policies:**

**Conditional (only high priority):**

```yaml
tools:
  - name: update_issue
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: 'args.priority == "critical" || args.priority == "high"'
        action: require_approval
        condition_type: cel
        description: "High-priority updates need approval"
```

**Status-based:**

```yaml
tools:
  - name: update_issue
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: 'has(args.status) && args.status == "closed"'
        action: require_approval
        condition_type: cel
        description: "Closing issues needs approval"
```

---

### `search`

Search for issues and comments in connected trackers using similarity or fulltext search.

**Arguments:**

- `query` (string, required) - Search query
- `project` (string, optional) - Limit search to one project
- `limit` (number, optional) - Max results (default: 10)

**Example:**

```python
results = search(
    query="authentication bug",
    project="acme/repo",
    limit=10
)
```

**When to preloop:**

- ⚪ Usually NOT needed (read-only)
- ✅ Preloop if searching sensitive/confidential data

---

## Comment Tools

### `add_comment`

Add a comment to an issue, pull request, or merge request. Supports inline code comments and thread replies.

**Arguments:**

- `target` (string, required) - Issue/PR/MR to comment on (URL, key, or ID)
- `comment` (string, required) - Comment text
- `path` (string, optional) - File path, for inline code comments
- `line` (number, optional) - Line number, for inline code comments
- `side` (string, optional) - Diff side for inline comments
- `in_reply_to` (string, optional) - Comment ID to reply to as a thread

**Example:**

```python
add_comment(
    target="acme/repo#42",
    comment="LGTM after the retry fix",
)
```

**When to preloop:**

- ✅ Comments are visible to your whole team and external collaborators: gate if agents comment autonomously

---

### `update_comment`

Update or resolve an existing comment on a pull request or merge request (GitHub/GitLab only).

**Arguments:**

- `target` (string, required) - PR/MR the comment belongs to
- `comment_id` (string, required) - Comment to update
- `body` (string, optional) - New comment text
- `resolved` (boolean, optional) - Resolve/unresolve the thread (review comments only)
- `thread_id` (string, optional) - Thread ID for resolution
- `comment_type` (string, optional) - `review_comment` (inline) or `issue_comment` (conversation); omit to auto-detect

**Example:**

```python
update_comment(
    target="acme/repo!17",
    comment_id="123456",
    resolved=True,
)
```

---

## Pull Request Tools

Available with a GitHub or GitLab tracker. Platform is auto-detected from the URL or project.

### `get_pull_request`

Get details of a pull request (GitHub) or merge request (GitLab): metadata, comments, and file changes.

**Arguments:**

- `pull_request` (string, required) - PR/MR identifier or URL
- `include_comments` (boolean, optional, default `true`) - Include comments
- `include_diff` (boolean, optional, default `true`) - Include file changes

**When to preloop:**

- ⚪ Usually NOT needed (read-only)

---

### `create_pull_request`

Create a pull request (GitHub) or merge request (GitLab).

**Arguments:**

- `project` (string, required) - Project as slug (`owner/repo`), full path, or URL
- `title` (string, required) - PR title
- `source_branch` (string, required) - Branch with your changes
- `target_branch` (string, required) - Branch to merge into
- `description` (string, optional) - PR description
- `draft` (boolean, optional, default `false`) - Open as draft
- `assignees` (string[], optional) - Assignees
- `reviewers` (string[], optional) - Reviewers
- `labels` (string[], optional) - Labels
- `milestone` (string, optional) - Milestone
- `extra_options` (object, optional) - GitLab-specific options like `squash`, `remove_source_branch`

**When to preloop:**

- ✅ Opening PRs triggers CI and notifies reviewers: gate for autonomous agents

---

### `update_pull_request`

Update a pull request's metadata, submit a review, and/or manage reactions.

**Arguments:**

- `pull_request` (string, required) - PR/MR identifier or URL
- `title`, `description`, `state`, `labels`, `assignees`, `reviewers`, `draft` (optional) - Metadata updates (`state`: open/closed)
- `review_action` (string, optional) - `approve`, `request_changes`, or `comment`
- `review_body` (string, optional) - Review summary text
- `review_comments` (object[], optional) - Inline review comments
- `add_reaction` / `remove_reaction` (string, optional) - Emoji reaction names

**When to preloop:**

- ✅ **Always for `review_action: approve`**: an agent approving PRs is a merge gate bypass

**Recommended policy:**

```yaml
tools:
  - name: update_pull_request
    source: builtin
    approval_workflow: "pr-review-gate"
    conditions:
      - expression: 'has(args.review_action) && args.review_action == "approve"'
        action: require_approval
        condition_type: cel
        description: "Agent PR approvals need a human sign-off"
```

---

## Issue Intelligence Tools

### `estimate_compliance`

Check whether issues meet a compliance metric such as "Definition of Ready".

**Arguments:**

- `issues` (string[], required) - Issues to check, as URLs or issue keys
- `compliance_metric` (string, optional, default `"DoR"`) - Metric to evaluate against

**Example:**

```python
compliance = estimate_compliance(
    issues=["PROJ-123", "PROJ-124"],
    compliance_metric="DoR",
)
```

**When to preloop:**

- ⚪ Usually NOT needed (analysis only, no modifications)

**Use cases:**

- Automated issue quality checks
- Pre-sprint grooming
- Compliance reporting

---

### `improve_compliance`

Get AI-powered suggestions to improve issue compliance.

**Arguments:**

- `issues` (string[], required) - Issues to improve, as URLs or issue keys
- `compliance_metric` (string, optional, default `"DoR"`) - Metric to improve against

**Example:**

```python
improvements = improve_compliance(issues=["PROJ-123"])
```

**When to preloop:**

- ⚪ Returns suggestions only, it does not modify issues. Gate `update_issue` instead if agents apply the suggestions.

---

## Tool Availability Matrix

| Tool | Always Available | Requires Tracker | Read-Only | Should Preloop? |
|------|------------------|------------------|-----------|-----------------|
| `request_approval` | ✅ | ❌ | ❌ | ⚪ N/A |
| `ask_user` | ✅ | ❌ | ❌ | ⚪ N/A |
| `get_approval_status` | ✅ | ❌ | ✅ | ⚪ No |
| `get_issue` | ❌ | ✅ Any | ✅ | ⚪ Usually no |
| `create_issue` | ❌ | ✅ Any | ❌ | ✅ Yes |
| `update_issue` | ❌ | ✅ Any | ❌ | ✅ Conditionally |
| `search` | ❌ | ✅ Any | ✅ | ⚪ Usually no |
| `estimate_compliance` | ❌ | ✅ Any | ✅ | ⚪ No |
| `improve_compliance` | ❌ | ✅ Any | ✅ | ⚪ No |
| `add_comment` | ❌ | ✅ Any | ❌ | ✅ Conditionally |
| `update_comment` | ❌ | ✅ GitHub/GitLab | ❌ | ✅ Conditionally |
| `get_pull_request` | ❌ | ✅ GitHub/GitLab | ✅ | ⚪ Usually no |
| `create_pull_request` | ❌ | ✅ GitHub/GitLab | ❌ | ✅ Yes |
| `update_pull_request` | ❌ | ✅ GitHub/GitLab | ❌ | ✅ Yes (reviews) |

---

## Prelooping Strategies

### Read-Only Tools (Low Risk)

**Tools:** `get_issue`, `search`, `get_pull_request`, `estimate_compliance`, `improve_compliance`

**Strategy:** Usually don't preloop

- No side effects
- No data modification
- Fast and safe

**Exception:** Sensitive data

```yaml
tools:
  - name: get_issue
    source: builtin
    approval_workflow: "sensitive-reads"
    conditions:
      - expression: 'args.issue.startsWith("CONFIDENTIAL-")'
        action: require_approval
        condition_type: cel
        description: "Confidential issues need approval to read"
```

### Write Tools (Medium-High Risk)

**Tools:** `create_issue`, `update_issue`, `add_comment`, `update_comment`, `create_pull_request`, `update_pull_request`

**Strategy:** Preloop conditionally

```yaml
approval_workflows:
  - name: "tracker-writes"
    approvals_required: 1
    approver_users: [team_lead]

tools:
  # Always require approval for issue creation
  - name: create_issue
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: "true"
        action: require_approval
        condition_type: cel

  # Only for high-priority or status changes
  - name: update_issue
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: 'args.priority == "critical" || args.priority == "high" || has(args.status)'
        action: require_approval
        condition_type: cel

  # Gate agent PR approvals
  - name: update_pull_request
    source: builtin
    approval_workflow: "tracker-writes"
    conditions:
      - expression: 'has(args.review_action) && args.review_action == "approve"'
        action: require_approval
        condition_type: cel
```

---

## Error Handling

### Common Errors

**Tool not available:**

```json
{
  "error": "Tool not found: create_issue",
  "code": -32601,
  "message": "No trackers connected. Connect GitHub, GitLab, or Jira to use this tool."
}
```

**Solution:** Connect a tracker under **Trackers > Add tracker**

**Invalid issue ID:**

```json
{
  "error": "Issue not found: INVALID-123",
  "code": -32000,
  "message": "Issue INVALID-123 does not exist in any connected tracker"
}
```

**Solution:** Verify issue ID format matches your tracker

**Permission denied:**

```json
{
  "error": "Permission denied",
  "code": -32002,
  "message": "User lacks permission to create issues in project PROJ"
}
```

**Solution:** Check tracker permissions or contact admin

---
