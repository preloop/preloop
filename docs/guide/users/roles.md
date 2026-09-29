# Roles & Permissions

Assign roles to users and teams to control what they can do in your Preloop account. This page documents the 7 system roles, what each one permits, and how permissions combine.

!!! info "Editions"
    The open-source core **enforces** permissions and seeds the 7 system roles; it exposes `GET /api/v1/roles` to list them. The user, team, and role **management** UI and APIs (inviting users, assigning roles, managing teams) are part of the Enterprise RBAC plugin, available in Preloop Cloud and Preloop Enterprise.

---

## Overview

**Preloop uses Role-Based Access Control (RBAC)** to manage what users can do in your account. Every user is assigned one or more roles, and each role grants a specific set of permissions.

**Key concepts:**

- **Permissions** - Atomic units of access (e.g., "manage_users", "create_flows")
- **Roles** - Collections of permissions (e.g., "Admin" has many permissions)
- **Users** - Assigned roles that determine their access level
- **Teams** - Groups of users that can inherit roles

**Benefits:**

- Fine-grained access control
- Principle of least privilege
- Easy permission management
- Audit trail of role changes

---

## System Roles

Preloop provides **7 system roles** that cover common access patterns. These roles are available to all accounts and cannot be modified.

### 1. Owner

**Full account access including billing and account management.**

**Permissions:**

- All permissions from Admin role
- Manage billing and subscription
- Close/delete account
- Transfer account ownership
- View all audit logs

**Use cases:**

- Account creator (automatically assigned)
- Primary decision maker
- Billing contact

**Who should have this:**

- Company founder or C-level executive
- Person responsible for payment
- **Limit to 1-2 users** for security

<!-- TODO screenshot: `role-owner-permissions.png` -->

---

### 2. Admin

**Full access except billing and account closure.**

**Permissions:**

- Manage users (invite, remove, change roles)
- Manage teams
- Manage all tools and MCP servers
- Create and manage approval workflows
- Create and manage flows
- Manage trackers (GitHub, GitLab, Jira)
- View all projects and activity
- Approve any approval request
- View audit logs

**Cannot:**

- Manage billing/subscription
- Close/delete account
- Transfer ownership

**Use cases:**

- Engineering managers
- DevOps leads
- Platform administrators

**Who should have this:**

- Technical leaders who need full control
- Users managing the platform day-to-day
- **Typically 2-5 users**

---

### 3. Editor

**Configure the control plane: agents, tools, policies, models, flows, and trackers.**

**Permissions:**

- Create and edit flows, tools, policies, and approval workflows
- Manage MCP servers and AI models
- Manage agents and runtime sessions
- Connect trackers and projects
- View cost analytics
- Decide approval requests and control managed agents

**Cannot:**

- Manage users, teams, or invitations
- Manage billing or close the account
- Manage budget policies

**Use cases:**

- Senior engineers
- Technical product managers
- Automation / platform engineers

**Who should have this:**

- Users building and governing automations
- Users configuring MCP firewall policies
- **Typically 5-15 users**

---

### 4. Executor

**Operate day-to-day: execute flows, decide approvals, and control agents.**

**Permissions:**

- Execute existing flows and use tools
- View and decide approval requests
- View agents, sessions, models, and cost
- Control managed agents
- Create/comment issues

**Cannot:**

- Create or edit flows, tools, policies, or models
- Add MCP servers or trackers
- Manage users, teams, or billing

**Use cases:**

- Junior engineers
- Operations team members
- Support engineers

**Who should have this:**

- Users who run existing automations and handle approvals
- Users who talk to managed agents without changing config
- **Most common role - typically 20-50% of users**

---

### 5. Tracker Manager

**Manage issue trackers and sync data.**

**Permissions:**

- Add and remove trackers (GitHub, GitLab, Jira)
- Configure tracker settings
- Force sync tracker data
- View tracker connection status
- View issues and projects

**Cannot:**

- Create flows
- Manage tools
- Manage users
- Access approval workflows

**Use cases:**

- Engineering managers (focused on project management)
- Scrum masters
- Project managers

**Who should have this:**

- Users responsible for issue tracking
- Users managing project data
- **Typically 2-5 users**

---

### 6. Analyst

**Read-only access plus compliance, cost, and audit insights.**

**Permissions:**

- View flows, tools, policies, MCP servers, agents, sessions, and models
- View cost analytics and audit logs
- Run compliance checks
- Detect duplicate issues and manage dependencies

**Cannot:**

- Create or modify control-plane configuration
- Execute flows or decide approvals
- Control managed agents
- Manage users, teams, or billing

**Use cases:**

- Compliance officers
- Security auditors
- Business analysts
- Stakeholders who need visibility

**Who should have this:**

- Users who need to monitor and analyze
- Non-technical stakeholders
- **Typically 5-10 users**

---

### 7. Viewer

**Read-only access to core control-plane resources.**

**Permissions:**

- View flows, tools, policies, MCP servers, trackers, projects, issues
- View models, agents, sessions, and compliance results

**Cannot:**

- View users, teams, cost, audit logs, or approvals
- Execute, configure, or decide anything

**Use cases:**

- External consultants
- Temporary contractors
- Demo accounts

**Who should have this:**

- Users who just need visibility
- External parties with limited access
- **Use sparingly - prefer Analyst for internal users**

---

## Role Comparison Matrix

| Capability | Owner | Admin | Editor | Executor | Tracker Mgr | Analyst | Viewer |
|-----------|-------|-------|--------|----------|-------------|---------|--------|
| Manage users / teams / invites | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Manage billing / close account | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Configure agents / tools / policies / models | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Control managed agents | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Execute flows / decide approvals | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| View cost analytics | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |
| Manage budgets | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Manage trackers / projects / issues | ✅ | ✅ | ✅ | limited | ✅ | ❌ | ❌ |
| Run compliance / duplicates / deps | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ |
| View audit logs | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| Read-only core resources | ✅ | ✅ | ✅ | ✅ | partial | ✅ | ✅ |

Permission names use `verb_resource` underscores (for example `view_cost`, `decide_approvals`, `create_projects`). Re-run `python scripts/init_system_roles.py` after upgrades to reconcile system roles on existing deployments.

**Notes:**

- Owner bypasses explicit permission rows and is treated as having every permission.
- Executor can create/comment issues and view sessions/cost, but cannot change tool/policy/model configuration.
- Tracker Manager is specialized for tracker/project/org/issue data and does not manage the agent control plane.
- Analyst is read-heavy plus compliance/cost/audit analysis; it cannot mutate control-plane config or decide approvals.
---

## Managing User Roles

### Adding a New User

1. Go to **Settings** → **Users**
2. Click **+ Invite User**
3. Fill in the form:
   ```
   Email: alice@acme.com
   Role: Editor
   Teams: [Optional - add to teams]
   Send invitation email: ✅
   ```
4. Click **Send Invitation**

<!-- TODO screenshot: `users-invite-form.png` -->

**What happens:**

- Invitation email sent to user
- User clicks link and creates account
- User automatically added with specified role
- User appears in Users list

---

### Changing a User's Role

1. Go to **Settings** → **Users**
2. Find the user
3. Click **Edit** (pencil icon)
4. Select new role from dropdown:
   ```
   Current Role: Executor
   New Role: Editor
   ```
5. Click **Save**

<!-- TODO screenshot: `users-change-role.png` -->

**Important notes:**

- Changes take effect immediately
- User doesn't need to log out/in
- Previous permissions removed
- New permissions granted
- Role change logged in audit trail

**Who can change roles:**

- Owner can change anyone's role
- Admin can change roles except Owner
- No one else can change roles

---

### Removing a User

1. Go to **Settings** → **Users**
2. Find the user
3. Click **Remove** (trash icon)
4. Confirm removal

**What happens:**

- User immediately loses access
- User's API keys revoked
- Audit logs retained

**Who can remove users:**

- Owner can remove anyone
- Admin can remove anyone except Owner
- No one else can remove users

---

## Team Roles

Teams can be assigned roles, granting all team members those permissions.

### How Team Roles Work

**Scenario:**

- Team: "SRE Team"
- Members: Alice, Bob, Charlie
- Team Role: Editor

**Result:**

- Alice has Editor permissions
- Bob has Editor permissions
- Charlie has Editor permissions

**If Bob is also individually assigned Admin:**

- Bob has the union of Admin and Editor permissions (in practice, Admin's — it is a superset)

### Assigning a Role to a Team

1. Go to **Settings** → **Teams**
2. Find the team
3. Open **Manage Team Roles**
4. Assign the role (e.g. Editor)

<!-- TODO screenshot: `team-role-assign.png` -->

All team members immediately get Editor permissions. See [Managing Teams](managing-teams.md) for the rest of team management.

---

## Permission Hierarchies

### Role Hierarchy

Roles are hierarchical for role management purposes:

```
Owner (highest)
  ↓
Admin
  ↓
Editor
  ↓
Executor
  ↓
Tracker Manager
  ↓
Analyst
  ↓
Viewer (lowest)
```

**Rules:**

- Owner can manage all roles
- Admin can manage all roles except Owner
- Others cannot manage roles

### Permission Combination

**Rule:** A user gets the **union** of all permissions from all their roles — individual and team-inherited. There is no "highest role wins"; every permission granted by any assigned role applies.

**Example:**

- Individual role: Executor
- Team role: Editor
- **Effective permissions:** everything Executor grants plus everything Editor grants

---

## Approval Permissions

### Who Can Approve Requests?

**Any role can approve IF:**

1. They're listed as approver in the tool's approval workflow
2. OR they're in a team that's listed as approver
3. OR the policy says "Any Admin/Owner"

**Example:**

```yaml
approval_workflows:
  - name: deploy-review
    approvals_required: 1
    approver_teams: [sre-team]
    approver_users: [cto]

tools:
  - name: deploy_production
    source: deploy-server
    approval_workflow: deploy-review
    conditions:
      - expression: "true"
        action: require_approval
        condition_type: cel
```

Can approve:
- ✅ Members of sre-team (any role)
- ✅ cto (any role)
- ❌ Other users (even if Admin)

**Exception:**

- Owner and Admin can override and approve any request in emergency situations

---

## Audit Trail

### Viewing Role Changes

1. Go to **Settings** → **Audit Logs**
2. Filter by category: "User Management"
3. See events:
   - User invited
   - User role changed
   - User removed
   - Team role changed

**Each entry shows:**

- Who made the change
- What changed (old role → new role)
- When it happened
- IP address

<!-- TODO screenshot: `audit-role-changes.png` -->

---

## API Access with Roles

### API Keys Inherit User Role

When you create an API key:
- API key inherits YOUR role
- API calls have same permissions as you
- Subject to same approval workflows

**Example:**


If you're an Executor:
- API key can execute flows ✅
- API key cannot create flows ❌
- API key cannot add MCP servers ❌

---
