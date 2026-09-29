# Managing Teams

Create teams, add members, and assign team roles so you can route approvals to groups and grant permissions in one place.

!!! info "Editions"
    Team management is part of the Enterprise RBAC plugin, available in Preloop Cloud and Preloop Enterprise.

---

## What Teams Do

Teams in Preloop are groups of users used in two places:

- **Approval routing** — approval workflows reference teams via `approver_teams` (and `escalation_teams`), so any member can vote on a request
- **Permissions** — roles assigned to a team apply to all its members, combined as a union with each member's individual roles

Teams are intentionally lightweight: a name, a description, members, and roles. There are no team leads, team notification channels, on-call rotations, hierarchies, statuses, or per-team quorums. Notification preferences are per-user, and quorum is the single workflow-level `approvals_required` integer.

---

## Creating a Team

1. Go to **Settings** → **Teams**
2. Click **+ Create Team**
3. Enter a **Name** and optional **Description**
4. Click **Create**

Pick names that match real approval or permission boundaries (`SRE`, `Finance`, `Security`) — they stay meaningful when reused across approval workflows.

You can edit the name and description later, or delete the team, from the same page.

---

## Managing Members

1. **Settings** → **Teams** → find the team
2. Open the **Team Members** modal
3. Add users from the account's user list, or remove existing members

Removing a member takes away the team's role permissions and stops team-routed approval notifications for them; their individual roles are untouched.

---

## Assigning Team Roles

1. **Settings** → **Teams** → find the team
2. Open the **Manage Team Roles** modal
3. Assign one or more roles (e.g. Editor)

All current and future members get the assigned roles' permissions, as a **union** with their individual roles. See [Roles & Permissions](roles.md) for what each role grants.

---

## Using Teams in Approval Workflows

Reference the team by name in a workflow's `approver_teams`. Quorum is the workflow's `approvals_required`:

```yaml
approval_workflows:
  - name: deploy-review
    approvals_required: 2
    approver_teams: [sre-team]
    escalation_teams: [platform-leads]
    timeout_seconds: 600
```

Every member of `sre-team` is notified; the request resolves after any 2 of them approve. On timeout, it escalates to `platform-leads`.

[Teams in Approval Workflows](../approvals/teams.md) is the full deep-dive: data model, quorum behavior, invitation flow, and recommended patterns.

---
