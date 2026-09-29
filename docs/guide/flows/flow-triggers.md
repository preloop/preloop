# Flow Triggers

Complete reference for all flow trigger types and their event payloads.

---

## Overview

Flows can be triggered by:

1. **Webhooks** - HTTP POST from any service
2. **GitHub Events** - PR, issue, push, release events
3. **GitLab Events** - MR, issue, pipeline events
4. **Jira Events** - Issue and comment events

Schedule/cron triggers are not yet available.

A tracker-triggered flow can subscribe to multiple event types at once (`trigger_event_types` is an array), for example, both `issue_opened` and `issue_updated`.

This guide provides complete event payload structures for each trigger type.

---

## Webhook Triggers

### Configuration

**Setup:**

1. Create flow with Trigger Type: Webhook
2. Save the flow
3. Copy the generated webhook URL
4. Use the URL to trigger the flow from external services

**Webhook URL Format:**

```
https://<your-preloop-host>/api/v1/webhooks/flows/{flow_id}/{webhook_secret}
```

**Request Requirements:**

- Method: `POST`
- Content-Type: `application/json` recommended
- Body: Any valid JSON (if the body can't be parsed as JSON, the payload falls back to `{}`)

### Payload Structure

You control the payload structure completely. Send any JSON you need:

```json
{
  "event": "payment_received",
  "timestamp": "2025-01-25T10:30:00Z",
  "data": {
    "amount": 1500,
    "currency": "USD",
    "customer_id": "cust_123",
    "contract_id": "contract_456"
  },
  "metadata": {
    "source": "payment_gateway",
    "ip_address": "192.168.1.1"
  }
}
```

### Accessing in Prompt Template

All webhook payload data is available under `trigger_event.payload`:

```
Event Type: {{trigger_event.payload.event}}
Amount: ${{trigger_event.payload.data.amount}}
Customer: {{trigger_event.payload.data.customer_id}}
Contract: {{trigger_event.payload.data.contract_id}}
Source: {{trigger_event.payload.metadata.source}}
```

### Example: Stripe Webhook

```json
{
  "type": "payment_intent.succeeded",
  "data": {
    "object": {
      "id": "pi_123",
      "amount": 150000,
      "currency": "usd",
      "customer": "cus_123",
      "metadata": {
        "contract_id": "CONTRACT-2025-001"
      }
    }
  }
}
```

**Access in prompt:**

```
Payment ID: {{trigger_event.payload.data.object.id}}
Amount: ${{trigger_event.payload.data.object.amount}}
Contract: {{trigger_event.payload.data.object.metadata.contract_id}}
```

### Example: Custom Service Webhook

```json
{
  "alert_type": "server_down",
  "severity": "critical",
  "server": {
    "hostname": "web-prod-01",
    "ip": "10.0.1.50",
    "region": "us-east-1"
  },
  "details": {
    "error": "Connection timeout",
    "last_seen": "2025-01-25T10:25:00Z"
  }
}
```

**Access in prompt:**

```
Alert: {{trigger_event.payload.alert_type}}
Severity: {{trigger_event.payload.severity}}
Server: {{trigger_event.payload.server.hostname}}
Error: {{trigger_event.payload.details.error}}
```

---

## GitHub Events

### Common Fields

All GitHub events include these base fields:

```json
{
  "action": "opened|closed|reopened|edited|labeled|etc",
  "repository": {
    "id": 123456,
    "name": "my-repo",
    "full_name": "acme/my-repo",
    "owner": {
      "login": "acme",
      "type": "Organization"
    },
    "html_url": "https://github.com/acme/my-repo",
    "default_branch": "main"
  },
  "organization": {
    "login": "acme",
    "id": 789
  },
  "sender": {
    "login": "username",
    "id": 12345,
    "type": "User"
  }
}
```

### Issue Events

#### issue_opened

Triggered when a new issue is created.

**Event Type:** `issue_opened`

**Payload:**

```json
{
  "action": "opened",
  "issue": {
    "id": 123456789,
    "number": 42,
    "title": "Bug: Application crashes on startup",
    "body": "## Description\n\nThe application crashes immediately after launching...",
    "state": "open",
    "user": {
      "login": "reporter_username",
      "id": 12345
    },
    "assignee": null,
    "assignees": [],
    "labels": [
      {
        "name": "bug",
        "color": "d73a4a"
      }
    ],
    "milestone": null,
    "created_at": "2025-01-25T10:30:00Z",
    "updated_at": "2025-01-25T10:30:00Z",
    "html_url": "https://github.com/acme/my-repo/issues/42"
  },
  "repository": { ... },
  "sender": { ... }
}
```

**Access in prompt:**

```
Issue #{{trigger_event.payload.issue.number}}: {{trigger_event.payload.issue.title}}
Body: {{trigger_event.payload.issue.body}}
Author: {{trigger_event.payload.issue.user.login}}
URL: {{trigger_event.payload.issue.html_url}}
Labels: {{trigger_event.payload.issue.labels}}
```

#### issue_updated

Triggered when issue is edited (title, body, labels changed, etc.).

**Action values:** `edited`, `labeled`, `unlabeled`, `assigned`, `unassigned`, `milestoned`, `demilestoned`

**Payload:** Same structure as `issue_opened`, but `action` field indicates what changed.

**Additional fields when edited:**

```json
{
  "changes": {
    "title": {
      "from": "Old title"
    },
    "body": {
      "from": "Old body"
    }
  }
}
```

#### issue_closed

Triggered when issue is closed.

**Payload:** Same as `issue_opened` with `action: "closed"` and `issue.state: "closed"`.

#### issue_reopened

Triggered when closed issue is reopened.

**Payload:** Same structure with `action: "reopened"`.

---

### Pull Request Events

#### pull_request_opened

Triggered when new PR is created.

**Event Type:** `pull_request_opened`

**Payload:**

```json
{
  "action": "opened",
  "number": 123,
  "pull_request": {
    "id": 987654321,
    "number": 123,
    "title": "Add new feature",
    "body": "## Changes\n\n- Added feature X\n- Fixed bug Y",
    "state": "open",
    "draft": false,
    "merged": false,
    "mergeable": true,
    "mergeable_state": "clean",
    "user": {
      "login": "developer_username",
      "id": 23456
    },
    "assignee": null,
    "assignees": [],
    "requested_reviewers": [
      {
        "login": "reviewer_username",
        "id": 34567
      }
    ],
    "labels": [],
    "milestone": null,
    "head": {
      "ref": "feature/new-feature",
      "sha": "abc123def456",
      "repo": {
        "name": "my-repo",
        "full_name": "acme/my-repo"
      }
    },
    "base": {
      "ref": "main",
      "sha": "def456abc123",
      "repo": {
        "name": "my-repo",
        "full_name": "acme/my-repo"
      }
    },
    "created_at": "2025-01-25T11:00:00Z",
    "updated_at": "2025-01-25T11:00:00Z",
    "html_url": "https://github.com/acme/my-repo/pull/123"
  },
  "repository": { ... },
  "sender": { ... }
}
```

**Access in prompt:**

```
PR #{{trigger_event.payload.pull_request.number}}: {{trigger_event.payload.pull_request.title}}
Author: {{trigger_event.payload.pull_request.user.login}}
Branch: {{trigger_event.payload.pull_request.head.ref}} → {{trigger_event.payload.pull_request.base.ref}}
URL: {{trigger_event.payload.pull_request.html_url}}
Draft: {{trigger_event.payload.pull_request.draft}}
Reviewers: {{trigger_event.payload.pull_request.requested_reviewers}}
```

#### pull_request_updated

Triggered when PR is edited or new commits are pushed.

**Action values:** `edited`, `synchronize` (new commits), `labeled`, `unlabeled`, `assigned`, `unassigned`, `review_requested`, `review_request_removed`

#### pull_request_merged

Triggered when PR is merged.

**Payload:** Same as `pull_request_opened` with:
```json
{
  "action": "closed",
  "pull_request": {
    "state": "closed",
    "merged": true,
    "merged_at": "2025-01-25T12:00:00Z",
    "merged_by": {
      "login": "merger_username"
    }
  }
}
```

**Filter to only merged PRs:**

```yaml
Event: pull_request_closed
Filters:
  merged: true
```

#### pull_request_closed

Triggered when PR is closed (merged or without merge).

Use `pull_request.merged` field to distinguish:
- `merged: true` - PR was merged
- `merged: false` - PR was closed without merging

---

### Comment Events

#### comment_created

Triggered when comment is created on issue or PR.

**Event Type:** `comment_created`

**Payload:**

```json
{
  "action": "created",
  "comment": {
    "id": 111222333,
    "body": "This looks good to me! LGTM 👍",
    "user": {
      "login": "commenter_username",
      "id": 45678
    },
    "created_at": "2025-01-25T13:00:00Z",
    "updated_at": "2025-01-25T13:00:00Z",
    "html_url": "https://github.com/acme/my-repo/issues/42#issuecomment-111222333"
  },
  "issue": {
    "number": 42,
    "title": "Issue title",
    "state": "open",
    "html_url": "https://github.com/acme/my-repo/issues/42"
  },
  "repository": { ... },
  "sender": { ... }
}
```

**Note:** For PR comments, `issue` field still exists (GitHub treats PR comments as issue comments internally).

**Access in prompt:**

```
Comment on Issue #{{trigger_event.payload.issue.number}}
Author: {{trigger_event.payload.comment.user.login}}
Comment: {{trigger_event.payload.comment.body}}
URL: {{trigger_event.payload.comment.html_url}}
```

#### comment_updated

Triggered when comment is edited.

**Payload:** Same as `comment_created` with `action: "edited"`.

---

### Push Events

#### push

Triggered when commits are pushed to a repository.

**Event Type:** `push`

**Payload:**

```json
{
  "ref": "refs/heads/main",
  "before": "abc123def456",
  "after": "def456ghi789",
  "created": false,
  "deleted": false,
  "forced": false,
  "commits": [
    {
      "id": "def456ghi789",
      "message": "Fix critical bug in payment processing",
      "timestamp": "2025-01-25T14:00:00Z",
      "author": {
        "name": "Developer Name",
        "email": "dev@example.com",
        "username": "dev_username"
      },
      "added": ["src/payment.py"],
      "removed": [],
      "modified": ["src/config.py"]
    }
  ],
  "head_commit": {
    "id": "def456ghi789",
    "message": "Fix critical bug in payment processing",
    "timestamp": "2025-01-25T14:00:00Z",
    "author": { ... }
  },
  "repository": { ... },
  "pusher": {
    "name": "pusher_username",
    "email": "pusher@example.com"
  },
  "sender": { ... }
}
```

**Extract branch name:**

```
Branch: {{trigger_event.payload.ref}}  # "refs/heads/main"
```

To get just "main", you'll need agent to parse it.

**Access in prompt:**

```
Pushed to: {{trigger_event.payload.ref}}
Commits: {{trigger_event.payload.commits}}
Author: {{trigger_event.payload.head_commit.author.name}}
Message: {{trigger_event.payload.head_commit.message}}
Files changed: {{trigger_event.payload.head_commit.modified}}
```

---

### Release Events

#### release

Triggered when release is published.

**Event Type:** `release`

**Payload:**

```json
{
  "action": "published",
  "release": {
    "id": 123456,
    "tag_name": "v1.2.0",
    "name": "Version 1.2.0",
    "body": "## Changes\n\n- Feature A\n- Bug fix B",
    "draft": false,
    "prerelease": false,
    "created_at": "2025-01-25T15:00:00Z",
    "published_at": "2025-01-25T15:00:00Z",
    "author": {
      "login": "releaser_username"
    },
    "html_url": "https://github.com/acme/my-repo/releases/tag/v1.2.0"
  },
  "repository": { ... },
  "sender": { ... }
}
```

**Access in prompt:**

```
New release: {{trigger_event.payload.release.tag_name}}
Title: {{trigger_event.payload.release.name}}
Notes: {{trigger_event.payload.release.body}}
URL: {{trigger_event.payload.release.html_url}}
```

---

## GitLab Events

### Common Fields

All GitLab events include:

```json
{
  "object_kind": "merge_request|issue|note|push|pipeline|etc",
  "project": {
    "id": 123,
    "name": "my-project",
    "path_with_namespace": "acme/my-project",
    "web_url": "https://gitlab.com/acme/my-project",
    "default_branch": "main"
  },
  "user": {
    "id": 456,
    "username": "username",
    "name": "User Name",
    "email": "user@example.com"
  }
}
```

### Merge Request Events

#### merge_request_opened

**Event Type:** `merge_request_opened`

**Payload:**

```json
{
  "object_kind": "merge_request",
  "event_type": "merge_request",
  "user": { ... },
  "project": { ... },
  "object_attributes": {
    "id": 123456,
    "iid": 42,
    "title": "Add new feature",
    "description": "## Changes\n\nAdded feature X",
    "state": "opened",
    "merged_at": null,
    "merge_status": "can_be_merged",
    "detailed_merge_status": "mergeable",
    "draft": false,
    "work_in_progress": false,
    "source_branch": "feature/new-feature",
    "target_branch": "main",
    "source_project_id": 123,
    "target_project_id": 123,
    "author_id": 456,
    "assignee_id": 789,
    "assignee_ids": [789],
    "reviewer_ids": [111, 222],
    "labels": [
      {
        "id": 1,
        "title": "feature",
        "color": "#428bca"
      }
    ],
    "milestone_id": null,
    "created_at": "2025-01-25T10:00:00Z",
    "updated_at": "2025-01-25T10:00:00Z",
    "url": "https://gitlab.com/acme/my-project/-/merge_requests/42"
  },
  "assignees": [
    {
      "id": 789,
      "username": "assignee_username",
      "name": "Assignee Name"
    }
  ],
  "reviewers": [
    {
      "id": 111,
      "username": "reviewer1"
    }
  ],
  "labels": [
    {
      "id": 1,
      "title": "feature",
      "color": "#428bca"
    }
  ]
}
```

**Access in prompt:**

```
MR !{{trigger_event.payload.object_attributes.iid}}: {{trigger_event.payload.object_attributes.title}}
Description: {{trigger_event.payload.object_attributes.description}}
Branch: {{trigger_event.payload.object_attributes.source_branch}} → {{trigger_event.payload.object_attributes.target_branch}}
URL: {{trigger_event.payload.object_attributes.url}}
Author: {{trigger_event.payload.user.username}}
State: {{trigger_event.payload.object_attributes.state}}
Merge Status: {{trigger_event.payload.object_attributes.detailed_merge_status}}
```

#### merge_request_merged

**Payload:** Same structure with:
```json
{
  "object_attributes": {
    "state": "merged",
    "merge_status": "merged",
    "merged_at": "2025-01-25T11:00:00Z",
    "merged_by_id": 333
  }
}
```

#### merge_request_approved

Triggered when MR receives approval (GitLab-specific).

**Payload:** Same as merge_request_opened with additional approval info.

---

### Issue Events

#### issue_opened

**Event Type:** `issue_opened`

**Payload:**

```json
{
  "object_kind": "issue",
  "event_type": "issue",
  "user": { ... },
  "project": { ... },
  "object_attributes": {
    "id": 789012,
    "iid": 25,
    "title": "Bug: Login page not loading",
    "description": "## Steps to Reproduce\n\n1. Navigate to /login\n2. Page shows white screen",
    "state": "opened",
    "author_id": 456,
    "assignee_ids": [789],
    "labels": [
      {
        "id": 2,
        "title": "bug",
        "color": "#d73a4a"
      }
    ],
    "milestone_id": null,
    "created_at": "2025-01-25T12:00:00Z",
    "updated_at": "2025-01-25T12:00:00Z",
    "url": "https://gitlab.com/acme/my-project/-/issues/25"
  },
  "assignees": [
    {
      "id": 789,
      "username": "assignee_username"
    }
  ],
  "labels": [
    {
      "id": 2,
      "title": "bug"
    }
  ]
}
```

**Access in prompt:**

```
Issue #{{trigger_event.payload.object_attributes.iid}}: {{trigger_event.payload.object_attributes.title}}
Description: {{trigger_event.payload.object_attributes.description}}
URL: {{trigger_event.payload.object_attributes.url}}
Labels: {{trigger_event.payload.labels}}
```

---

### Comment Events (Notes)

#### comment_created

Comments in GitLab are called "notes".

**Event Type:** `comment_created`

**Payload:**

```json
{
  "object_kind": "note",
  "event_type": "note",
  "user": { ... },
  "project": { ... },
  "object_attributes": {
    "id": 111222,
    "note": "This looks great! Approved.",
    "noteable_type": "MergeRequest",
    "author_id": 456,
    "created_at": "2025-01-25T13:00:00Z",
    "updated_at": "2025-01-25T13:00:00Z",
    "noteable_id": 42,
    "url": "https://gitlab.com/acme/my-project/-/merge_requests/42#note_111222"
  },
  "merge_request": {
    "id": 123456,
    "iid": 42,
    "title": "MR title",
    "url": "https://gitlab.com/acme/my-project/-/merge_requests/42"
  }
}
```

**Access in prompt:**

```
Comment on MR !{{trigger_event.payload.merge_request.iid}}
Author: {{trigger_event.payload.user.username}}
Comment: {{trigger_event.payload.object_attributes.note}}
URL: {{trigger_event.payload.object_attributes.url}}
```

---

### Pipeline Events

#### pipeline

Triggered when CI/CD pipeline status changes.

**Event Type:** `pipeline`

**Payload:**

```json
{
  "object_kind": "pipeline",
  "object_attributes": {
    "id": 123456,
    "ref": "main",
    "tag": false,
    "sha": "abc123def456",
    "status": "success",
    "stages": ["build", "test", "deploy"],
    "created_at": "2025-01-25T14:00:00Z",
    "finished_at": "2025-01-25T14:15:00Z",
    "duration": 900,
    "source": "push"
  },
  "merge_request": {
    "id": 42,
    "iid": 42,
    "title": "MR title",
    "source_branch": "feature-branch",
    "target_branch": "main"
  },
  "user": { ... },
  "project": { ... },
  "commit": {
    "id": "abc123def456",
    "message": "Commit message",
    "timestamp": "2025-01-25T14:00:00Z",
    "author": {
      "name": "Author Name",
      "email": "author@example.com"
    }
  }
}
```

**Status values:** `created`, `waiting_for_resource`, `preparing`, `pending`, `running`, `success`, `failed`, `canceled`, `skipped`, `manual`

**Access in prompt:**

```
Pipeline: {{trigger_event.payload.object_attributes.status}}
Branch: {{trigger_event.payload.object_attributes.ref}}
Duration: {{trigger_event.payload.object_attributes.duration}}s
MR: {{trigger_event.payload.merge_request.title}}
```

---

## Jira Events

### Common Fields

All Jira events include:

```json
{
  "webhookEvent": "jira:issue_created|jira:issue_updated|etc",
  "timestamp": 1706184000000,
  "user": {
    "self": "https://your-domain.atlassian.net/rest/api/2/user?accountId=...",
    "accountId": "557058:...",
    "displayName": "User Name",
    "emailAddress": "user@example.com"
  }
}
```

### Issue Events

#### issue_opened

**Event Type:** `issue_opened`

**Payload:**

```json
{
  "webhookEvent": "jira:issue_created",
  "timestamp": 1706184000000,
  "issue_event_type_name": "issue_created",
  "user": { ... },
  "issue": {
    "id": "12345",
    "key": "PROJ-123",
    "self": "https://your-domain.atlassian.net/rest/api/2/issue/12345",
    "fields": {
      "summary": "Application crashes on startup",
      "description": "## Steps to Reproduce\n\n1. Launch app\n2. Crash occurs immediately",
      "issuetype": {
        "id": "10001",
        "name": "Bug",
        "iconUrl": "..."
      },
      "status": {
        "id": "10000",
        "name": "To Do",
        "statusCategory": {
          "key": "new"
        }
      },
      "priority": {
        "id": "3",
        "name": "Medium"
      },
      "assignee": {
        "accountId": "557058:...",
        "displayName": "Assignee Name",
        "emailAddress": "assignee@example.com"
      },
      "reporter": {
        "accountId": "557058:...",
        "displayName": "Reporter Name"
      },
      "labels": ["bug", "critical"],
      "created": "2025-01-25T10:00:00.000+0000",
      "updated": "2025-01-25T10:00:00.000+0000"
    }
  }
}
```

**Access in prompt:**

```
Jira Issue: {{trigger_event.payload.issue.key}}
Summary: {{trigger_event.payload.issue.fields.summary}}
Description: {{trigger_event.payload.issue.fields.description}}
Type: {{trigger_event.payload.issue.fields.issuetype.name}}
Priority: {{trigger_event.payload.issue.fields.priority.name}}
Status: {{trigger_event.payload.issue.fields.status.name}}
Assignee: {{trigger_event.payload.issue.fields.assignee.displayName}}
Reporter: {{trigger_event.payload.issue.fields.reporter.displayName}}
Labels: {{trigger_event.payload.issue.fields.labels}}
```

#### issue_updated

**Event Type:** `issue_updated`

**Payload:** Same as `issue_opened` with additional `changelog` field:

```json
{
  "webhookEvent": "jira:issue_updated",
  "changelog": {
    "items": [
      {
        "field": "status",
        "fieldtype": "jira",
        "from": "10000",
        "fromString": "To Do",
        "to": "10001",
        "toString": "In Progress"
      }
    ]
  },
  "issue": { ... }
}
```

**Access in prompt:**

```
Issue updated: {{trigger_event.payload.issue.key}}
Changes: {{trigger_event.payload.changelog.items}}
```

Array indexing is not supported in templates: pass the whole `changelog.items` array and ask the agent to read the `fromString`/`toString` values.

---

## Event Filters

### Filter Configuration

When configuring tracker triggers, you can add filters to narrow when flows trigger.

**Available Filters:**


| Filter | Applies To | Description |
|--------|-----------|-------------|
| Author/Creator | All | Username who created the issue/PR/MR |
| Assignee | All | Who it's assigned to |
| Reviewer | PR/MR only | Requested reviewer |
| Labels | All | Must have ALL specified labels |
| Milestone | GitHub/GitLab | Milestone name |
| Priority | Jira only | Priority level |
| Issue Type | Jira only | Issue type name |
| State | PR/MR | State (open, closed, merged) |
| Draft | PR/MR | Draft status |
| Merged | PR/MR | Merge status |
| Mergeable State | GitHub PR | Mergeable state |

### Filter Logic

- **All filters are AND:** All conditions must match
- **Labels are OR:** Any label in the list matches
- **Empty filters match everything**

### Example Filter Combinations

**Only critical bugs from team members:**
```yaml
Event: issue_opened
Filters:
  labels: [bug, critical]
  assignee: alice,bob,charlie
```

**Only merged PRs to main branch:**
```yaml
Event: pull_request_closed
Filters:
  merged: true
  # Plus in trigger_config:
  target_branch: main
```

**Only ready-for-review MRs from backend team:**
```yaml
Event: merge_request_opened
Filters:
  draft: false
  author: backend-dev-1,backend-dev-2,backend-dev-3
  labels: [backend]
```

**Only high-priority Jira bugs:**
```yaml
Event: issue_opened
Filters:
  priority: High
  issue_type: Bug
```

---

## Template Variable Reference

### Accessing Nested Fields

Use dot notation to access nested fields:

```
{{trigger_event.payload.issue.user.login}}
{{trigger_event.payload.object_attributes.title}}
{{trigger_event.payload.pull_request.head.ref}}
```

### Arrays

Template resolution is dot-notation over objects only: array indexing like `labels[0]` is not supported. Pass the whole array:

```
Labels: {{trigger_event.payload.issue.labels}}
```

**Tip:** Let the AI agent parse arrays in the prompt rather than trying complex template syntax.

### Conditional Logic

Template variables don't support conditional logic. Instead, write prompts that handle all cases:

```
Process this event:

Title: {{trigger_event.payload.issue.title}}
State: {{trigger_event.payload.issue.state}}

If state is "open", add "triaged" label.
If state is "closed", post a thank you comment.
```

---

## Testing Triggers

### Test Run with Custom Data

When using Test Run on flows with template variables:

1. Click **Test Run** button
2. Fill in values for each `{{trigger_event.*}}` variable
3. Click **Run Test**

Example:
```
trigger_event.payload.issue.title: "Test Issue"
trigger_event.payload.issue.number: 42
trigger_event.payload.issue.user.login: "testuser"
```

### Test Webhooks with curl

```bash
curl -X POST 'YOUR_WEBHOOK_URL' \
  -H 'Content-Type: application/json' \
  -d '{
    "test": true,
    "data": {
      "amount": 500,
      "recipient": "test@example.com"
    }
  }'
```

### Test Tracker Events

Trigger real events in your tracker (GitHub, GitLab, Jira):
- Create a test issue
- Open a test PR
- Add a comment

Watch the flow execution in Preloop.

---
