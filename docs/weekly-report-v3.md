# Weekly report settings

Account settings expose three independent switches through
`GET` and `PUT /api/v1/account/weekly-report/preferences`:

- `account_enabled`: weekly reports for owners and admins, on by default.
- `members_enabled`: personal reports for regular members. The stored nullable
  field uses the edition's plan default until an admin explicitly changes it.
- `member_policy_findings_enabled`: allow personal reports to include explicitly
  attributed stored governance findings, off by default.

`user_enabled` remains the recipient's personal unsubscribe preference.
Account switches require the primary owner, a superuser, or `manage_account`;
a member may update only their own unsubscribe preference. CRUD serializes
account changes and commits their audit records atomically. The console controls
are gated by the existing governance-review capability. Report delivery, plan
defaults and comparison orchestration are provided by the commercial plugin.

The read-only report projections filter by account and optional attributed user
before aggregation. Personal approvals count requests triggered through that
user's keys or managed agents and requests on which they recorded a vote. Raw
finding evidence is excluded from personal projections. Adjacent report windows
are half-open, and replay-validation traffic is excluded from usage totals.
