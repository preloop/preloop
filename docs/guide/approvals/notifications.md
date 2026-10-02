# Multi-Channel Notifications

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After reading this page you can route a workflow's approval requests to Slack, Mattermost, or a webhook, and set per-user email and mobile push preferences so approvers get pinged where they actually are.

!!! info "Edition notes"
    All notification channels (**email, mobile push, Slack, Mattermost, and webhooks**) are available in the **open-source** edition. The mobile and watch apps are proprietary clients, but they work against self-hosted open-source servers.

---

## Overview

When an approval is required, Preloop notifies through two kinds of channels:

- **Per-user channels**: email and mobile push, controlled by each approver's own preferences
- **Per-workflow channels**: Slack, Mattermost, and webhooks, configured on the approval workflow via `channel_configs`

The web dashboard always shows pending requests in real time regardless of channel configuration.

### Email and Push Interplay

Delivery is mobile-first:

- Users with **push enabled** get the push notification immediately; their email is delayed **15 seconds** and skipped entirely if the request is resolved first (for example, approved from the phone).
- Users with **email only** get the email immediately.

---

## Per-User Preferences

Each user configures three settings in **Settings > Notifications**:

| Setting | Values | Description |
|---------|--------|-------------|
| `preferred_channel` | `email`, `mobile_push` | Which channel to prioritize |
| `enable_email` | on/off | Receive approval emails |
| `enable_mobile_push` | on/off | Receive push notifications on registered devices |

That is the full set: there are no quiet hours, digests, or per-tool notification overrides.

---

## Email

Every approval email includes the tool name, formatted arguments, the agent's reasoning (if provided), and direct **Approve** / **Decline** links.

The links carry a secure token, so no login is required: click, decide, done. Sensitive argument values are redacted before they are included (see [Redaction](../../security/redaction.md)).

```
Subject: Approval Required: pay

Tool Call: pay
Arguments:
  - recipient: alice@example.com
  - amount: $1,000

[Approve] [Decline]
```

---

## Mobile Push

Available on iPhone, iPad, Apple Watch, and Android.

1. Approval request created
2. Push sent to all registered devices immediately
3. Tap to open the request, approve or decline from the app (or straight from the watch)

### Setup

1. Install the app: [App Store](https://apps.apple.com/us/app/preloop/id6757803021) or [Google Play](https://play.google.com/store/apps/details?id=ai.spacecode.preloop)
2. Self-hosted deployment? Enter your server URL before signing in
3. Sign in and grant notification permissions

See [Mobile Apps](../clients/mobile-apps.md) for the full setup flow.

---

## Slack and Mattermost

Slack and Mattermost notifications post to an **incoming webhook** configured per approval workflow. The message includes the tool name, redacted arguments, agent reasoning, and a Review button that opens the token-based approval page.

Configure via `channel_configs` on the workflow, in policy YAML:

```yaml
approval_workflows:
  - name: deploy-review
    approvals_required: 1
    approver_teams: [sre-team]
    channel_configs:
      slack:
        webhook_url: "https://hooks.slack.com/services/T000/B000/XXXX"
```

Mattermost is identical: use the `mattermost` key with your Mattermost incoming webhook URL:

```yaml
    channel_configs:
      mattermost:
        webhook_url: "https://mattermost.example.com/hooks/XXXX"
```

Because each workflow carries its own `channel_configs`, you can point different workflows at different channels (deployments to `#deploys`, payments to `#finance`) by creating a webhook per channel and one workflow per audience.

---

## Webhooks

Send approval events to your own service. Configure the `webhook` key on the workflow:

```yaml
    channel_configs:
      webhook:
        url: "https://my-service.example.com/approvals"
        headers:
          Authorization: "Bearer ${MY_TOKEN}"
```

Preloop POSTs a JSON payload:

```json
{
  "type": "approval_request",
  "request_id": "6f1c0d2e-0000-4000-8000-000000000001",
  "tool_name": "deploy",
  "summary": "Deploy to production",
  "tool_args": {"environment": "production"},
  "agent_reasoning": "…",
  "status": "pending",
  "requested_at": "2026-07-17T20:00:00Z",
  "expires_at": "2026-07-17T20:10:00Z",
  "actions": {
    "review": "https://preloop.example.com/console/approval/6f1c…?token=TOKEN",
    "approve": "…same as review…",
    "decline": "…same as review…",
    "view": "…same as review…"
  },
  "decision": {
    "method": "POST",
    "approve_url": "https://preloop.example.com/approval/6f1c…/approve?token=TOKEN",
    "decline_url": "https://preloop.example.com/approval/6f1c…/decline?token=TOKEN"
  }
}
```

There are two kinds of URL in the payload:

- **`actions`** are pages for a person. Every key opens the same approval page in a browser (`GET`). They do not decide anything. `approve`, `decline` and `view` are deprecated aliases of `review`, kept for receivers that read them.
- **`decision`** is for a system. `POST` to `approve_url` or `decline_url` to record the decision. The token in the query string is the only credential: send no `Authorization` header. The body is optional; send `{"comment": "..."}` to record why.

Sensitive argument values are [redacted](../../security/redaction.md). The token in these URLs decides the request, so treat the payload as a secret.

### Worked example: payload in, decision back

Your service receives the payload above, asks whoever must decide, and answers with one call:

```bash
# Approve, with a comment
curl -X POST "$(jq -r .decision.approve_url payload.json)" \
  -H "Content-Type: application/json" \
  -d '{"comment": "Confirmed by the account owner"}'

# Or decline (the body is optional)
curl -X POST "$(jq -r .decision.decline_url payload.json)"
```

A `200` returns the request with its new `status` (`approved` or `declined`). A `400` means it was already decided or expired; a `404` means the id or token is wrong.

If your service holds a Preloop API key instead, the authenticated routes take the same bodies:

```bash
curl -X POST "https://preloop.example.com/api/v1/approval-requests/$REQUEST_ID/approve" \
  -H "Authorization: Bearer $PRELOOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"comment": "Confirmed by the account owner"}'
```

`/approve` and `/decline` need no body. `/decide` takes `{"approved": true|false, "comment": "..."}` and requires `approved`. The CLI does the same with `preloop approvals approve <id> --reason "..."` and `preloop approvals deny <id> --reason "..."`.

---

## Web Dashboard

Always on. The notification bell shows pending requests; clicking one opens the full request with arguments, timeline, and Approve/Decline buttons. Updates arrive over WebSocket: new requests and status changes appear without a refresh.

---

## Testing Your Setup

1. Configure a test tool with approval required
2. Call the tool from your MCP client
3. Verify notifications arrive on each configured channel
4. Approve from one channel
5. Verify the others reflect the decision, and, if you have push enabled, that the delayed email was skipped

---

## Related Pages

- [Async Approvals](async-approvals.md): non-blocking approvals with polling
- [Teams in Approval Workflows](teams.md): routing to groups of approvers
- [Per-Tool Justification](justification.md): agent reasoning in notifications
- [Native Tool Approvals](ai-approvals.md#native-tool-approvals): approvals for agents' shell and file operations, delivered over the same channels
