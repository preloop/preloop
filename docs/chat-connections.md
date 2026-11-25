# Chat connections

Ask Preloop about agents and spend, receive private approval notices, and send
explicit notes or commands using your existing Preloop permissions. A team
administrator connects the bot; each person links their own account. Shared bot
credentials never grant another person's permissions.

## Before starting

Run database migrations and the separate durable chat worker:

```sh
cd backend/preloop/models
alembic upgrade head
python -m preloop.services.chat_worker
```

For Helm deployments, enable the optional worker in your deployment values:

```yaml
chatWorker:
  enabled: true
```

This creates a separate `-chat-worker` Deployment running the command above.
The sync worker pools do not process chat jobs. The chat worker shares the API's
image, database, signing/encryption secrets, NATS connection, and operator
`extraEnv`, `extraEnvFrom`, `extraVolumes`, and `extraVolumeMounts`. If you supply
an encryption key or provider CA through those hooks, apply the same settings to
both processes. Run migrations before starting the worker. See the
[Helm chart guide](https://github.com/preloop/preloop/blob/main/helm/preloop/README.md#chat-worker).

Set `PRELOOP_URL` to your externally reachable HTTPS origin before setting up
Slack, Discord or Teams. Provider callbacks cannot use a localhost or placeholder URL.
Configure encryption for stored credentials. Setup secrets live in encrypted,
actor/account-bound drafts for 30 minutes and are never stored in the browser.
The connection ID reserves its final callback before installation. Expired drafts
must be restarted. Management permissions are checked again when finishing.

Open **Settings → Chat connections → Add connection**, choose the service, and
enter its bot credentials. Preloop discovers identity and named workspaces for
Slack, Mattermost and Discord. Teams requires an application ID and directory ID;
it shows the directory ID without requesting Graph access. One workspace selects automatically;
choose a name when there are several. A custom connection name is optional under
**Advanced settings**. After saving, distinguish **Configured**, **Worker ready**,
**Provider event received**, and **Your account linked**. Configuration alone does
not mean a worker or provider callback has connected.

## Mattermost setup

1. Create a Mattermost bot account and copy its **bot access token secret**. A
   Token ID is not a credential. Do not use a human or system-admin token.
2. Add the bot to the team whose members may use Preloop. In Mattermost, open
   that team's menu → **Invite People → Invite Member**, find the bot in
   **Add or Invite People**, and select **Invite Members**. Bot accounts can be
   invited using this flow; see [Mattermost's bot account instructions](https://docs.mattermost.com/developers/integrate/reference/bot-accounts/).
   If invitations are restricted, ask an administrator to open **System Console →
   User Management → Teams**, select the team, then **Members → Add Members**,
   select the bot, and **Add → Save**. See the official
   [team membership administration guide](https://docs.mattermost.com/administration-guide/configure/user-management-configuration-settings).
3. Enter the server's HTTPS origin (for example `https://chat.example.com`) and
   the token. Preloop verifies `/api/v4/users/me` and lists that bot's teams by
   display name. A verified bot with no team memberships is a valid incomplete
   setup: **Finish** stays disabled and the wizard shows its `@username` and
   invitation instructions. Add that bot to a team, return to the same wizard,
   and select **Refresh installation**. Refresh reuses the encrypted draft and
   token, including after a temporary provider error; it does not extend the
   30-minute expiry. Once one team appears it selects automatically; choose
   explicitly if several appear. Do not create a new token or give the bot
   administrator privileges to solve missing membership.
4. Select the team and finish. The default is a bot WebSocket listener for direct
   messages: **no `/preloop` command or slash-command token is required**.
5. Select **Link my account**, then privately message the bot with `link CODE`.
   Keep the code private. Refresh the connection to see the linked state.

Advanced team lookup accepts the team name or normal team URL, such as
`https://chat.example.com/engineering`. It resolves the name through the API and
checks bot membership. Invite links, channel URLs, and guessed IDs are rejected.

Saving the connection does not start a worker. Check **Worker ready** and
**Provider event received** in the connection, then link your account and send a
fresh DM to verify delivery. A running Kubernetes pod alone does not verify
provider connectivity. The worker has no HTTP health endpoint; inspect connection
health and worker logs for credentials, outbound policy, TLS, or WebSocket errors.
The Cloud documentation support service below has a separate lifecycle; enabling
it does not start this chat worker.

The initial WebSocket transport accepts one-to-one DMs only. Public mentions and
group DMs are not supported. Both the bot and human must still be members of the
selected team, even though a DM channel has no team ID. Canonical posts, channel
membership, user identity, and team membership are fetched from the authenticated
API. Bot/system messages and old posts are ignored. Stable post IDs deduplicate
repeated frames. Replica leases prevent multiple active listeners for a connection.
Disabling a connection or revoking its credentials stops delivery.

After an outage the listener reconnects with bounded backoff. It does **not** replay
commands posted while disconnected: send those again deliberately after the worker
is ready. Accepted queue jobs remain durable. A delivery interrupted during an
external write is marked uncertain and is not blindly replayed.

Mattermost HTTPS destinations reuse the instance outbound network policy. Public
addresses work by default. For self-hosted intranet servers, the instance operator
can allow the necessary private subnet with `MODEL_ENDPOINT_ALLOWED_CIDRS`.
Link-local metadata addresses remain blocked. HTTP and WebSocket connections pin
the validated DNS address while preserving TLS verification and the Host header;
credential-bearing redirects are refused. The process must trust your server's CA
through the system trust store or `SSL_CERT_FILE`.

### Existing slash-command connections

Existing connections retain token verification and team checks. The legacy
connection API remains available for advanced setups. To create `/preloop`
manually in Mattermost **Integrations → Slash Commands**, use:

- Trigger: `preloop`; method: **POST**.
- Request URL: the exact connection callback displayed by Preloop.
- Response username/icon: your bot's presentation, as desired.
- Verification token: copy the per-command token into the legacy connection API's
  `verification_secret`, separately from its `bot_token`.

Run `/preloop /link CODE` for linking and `/preloop /note SESSION_ID TEXT` for a
note. Existing connections can continue operating as configured. The wizard does
not create slash commands or grant `manage_slash_commands` permissions.

## Slack setup

Ordinary direct messages and `app_mention` Events API events are the portable
baseline. No `/preloop` slash command or paid Slack agent surface is needed.

For your own Slack app:

1. Create an app at [Slack apps](https://api.slack.com/apps), enable its bot, and
   install it to your workspace. Copy the **Bot User OAuth Token** (`xoxb-…`).
2. Enter the token in Preloop. `auth.test` discovers the workspace and bot user.
3. Use the generated minimal manifest and reserved callback. Its scopes are
   `chat:write`, `im:write`, `im:history`, and `app_mentions:read`; its bot events
   are `message.im` and `app_mention`. Enable the Messages tab and allow messages.
4. Copy **Basic Information → App Credentials → Signing Secret** into Preloop.
   Slack's bot token cannot reveal this secret. The draft accepts only signed
   provider verification challenges before completion, not chat commands.
5. Configure **Event Subscriptions → Request URL** with the displayed callback,
   verify it, and finish installation. DM `link CODE` to link your own identity.

The manifest is also useful when recreating an app: replace its callback with the
reserved URL from your draft and reinstall when Slack requests updated scopes.
Do not paste tokens into app names, workspace IDs, source code, or logs.

**Add to Slack** appears only when the deployment has all four settings:
`CHAT_SLACK_CLIENT_ID`, `CHAT_SLACK_CLIENT_SECRET`,
`CHAT_SLACK_SIGNING_SECRET`, and `CHAT_SLACK_REDIRECT_URI`. Register that fixed
redirect URI with Slack; never derive it from a request's Host header. The OAuth
state is random, expiring, single-use, and bound to the initiating account/actor.
Return to Preloop, refresh, confirm the discovered workspace, then finish while
signed in as that actor. Slack OAuth v2 does not advertise PKCE for this flow;
client-secret exchange and server-side state are used. The own-app path works
without centrally provisioned credentials. Reinstall through a new setup to rotate
credentials, then disable the old connection after relinking and checking delivery.
If Slack returns expiring credentials and a refresh token for the centrally configured OAuth app, the leased worker rotates both encrypted tokens before expiry. Failed/revoked refresh is visible as reconnecting and expired credentials are not used. Own-app manual tokens can be replaced by completing a new setup and disabling the old connection.

Slack `agent_view`/assistant events are not implemented. Standard DMs work without
those optional plan-specific capabilities. This integration does not claim native
agent thread context/status behavior.

## Discord setup

1. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications)
   and copy its bot token. Enter that token in Preloop.
2. Preloop reads `GET /applications/@me` and `/users/@me`, including the application
   ID, name, and `verify_key`. It lists installed servers from `/users/@me/guilds`;
   an application's support-server `guild_id` is not treated as an installation.
3. If no server appears, use **Install in Discord**, then refresh. The link uses
   only `bot` + `applications.commands`, with Send Messages permission. A user
   with server installation rights must approve it.
4. Select a server and explicitly choose **Finish and install /preloop**. Preloop
   configures the interactions callback using `PATCH /applications/@me`, then
   creates or updates only its own guild `/preloop` command with a required
   `message` string option. Other commands are preserved. An existing unrelated
   callback requires explicit confirmation before replacement.
5. Run `/preloop message:/link CODE` using the private linking code shown in
   Preloop. Replies arrive by DM; the interaction acknowledgement is ephemeral.
   Allow DMs from the bot/server so delivery can succeed.

A callback must be externally reachable and pass Discord's signed PING validation.
If Discord rejects it, check the displayed URL, TLS, and ingress routing, then retry
finishing the same setup. No public key, application ID, bot user ID, or raw server
ID is required in the wizard. Discovery is bounded to 200 installed servers.

## Commands and authorization

After linking, use ordinary text for read-only questions. Explicit commands are:

| Action | Slack / Mattermost / Teams personal message | Discord |
| --- | --- | --- |
| Run a flow | `ask ALIAS TEXT` (or `ask TEXT` with a default) | `/preloop message:/ask ALIAS TEXT` |
| List available flows | `help` | `/preloop message:/help` |
| Link account | `link CODE` | `/preloop message:/link CODE` |
| Session note | `note SESSION_ID TEXT` | `/preloop message:/note SESSION_ID TEXT` |
| Agent message | `message AGENT_ID SESSION_ID TEXT` | `/preloop message:/message AGENT_ID SESSION_ID TEXT` |
| Approve request | `approve REQUEST_ID` | `/preloop message:/approve REQUEST_ID` |
| Deny request | `deny REQUEST_ID` | `/preloop message:/deny REQUEST_ID` |

The bot rechecks identity, account status, resource visibility and permission
before actions and before delivering stored responses. Approval quorum and existing
policy checks still apply. Linking codes expire after ten minutes and are single
use. Use **Unlink my account** to revoke an identity. Account administrators can
disable the connection; delivery history makes failures and uncertain sends visible.

Mattermost Agents is complementary: it provides its own model/MCP agent layer and
is not required for this transport. No unverified gateway integration or
actor-specific permissions through a shared Agents credential are promised.

## Start a flow from chat

Edit a flow's **Trigger configuration → Chat trigger → Chat flow alias** to opt it in. The
alias is unique within the account, 1–40 lowercase letters, digits or hyphens.
An empty alias disables chat starts; existing flows stay off until configured.
The flow must also be enabled. In **Settings → Chat connections**, optionally
choose a **Default chat flow**. Only enabled, opted-in flows appear there.

Send `ask docs Where is the release checklist?` to the Slack or Mattermost bot.
Slack channel mentions also work: `@Preloop ask docs Where is the checklist?`.
On Discord use `/preloop message:/ask docs Where is the checklist?`.
The sender must link their own account and have both `execute_flows` (the manual
trigger permission) and `view_flows`, including resource-specific access. `help`
lists only aliases that sender may run and read. Unknown, disabled, unconfigured,
and inaccessible aliases are reported as unavailable without exposing hidden names.

If a connection has a default, `ask Where is the checklist?` uses it. An exact
first-word alias takes precedence; otherwise the whole question goes to the
default. This means a misspelled alias is part of the default flow's question.
Without a default, an unknown alias is refused. Plain text without `ask` continues
to ask the existing read-only account assistant.

Each question starts a new execution; thread replies do not resume prior runs.
The normal limits, budgets, approvals and account kill switch apply. The worker
allows five starts per user per minute and twenty per connection per minute,
across workers, and one unfinished chat run per user per flow. Refused/uncertain
start attempts count toward the minute limit. An uncertain start is never replayed.

The immediate acknowledgement links to the execution. The console labels it as a
chat run with the connection name; the audit entry records the linked user and
chat connection. The worker sends one notice if the run waits for approval, then
sends the final answer or a short failed/stopped/timeout message. Slack replies
stay in the original DM/thread; channel mentions receive replies in that channel's
original thread. Mattermost and Discord replies use the sender's same bot DM
(Discord command acknowledgements remain ephemeral).

For a flow with a result schema, the answer is the validated result's string
`text` field. For Codex and Claude Code without a result schema, explicit completed
assistant events supply the final answer. Arbitrary log tails and tool output are
never treated as answers. If no final text is available, the reply links to the
execution for inspection. Answers are truncated to 2,000 characters on Discord
and 8,000 on Slack/Mattermost, always preserving the full-result link. Identity,
account status, permissions and resource visibility are checked again before each
delivery. Unlinking or disabling a connection prevents pending answers from
being delivered. An interrupted external write remains uncertain, without replay.

### Example: answer questions from a repository of documents

1. Create a flow with your documents repository in its clone configuration and a
   model/agent configured through the normal flow setup. Enable it and set its chat
   alias to `docs`.
2. Use a prompt such as:

   ```text
   Answer the question only from the cloned documents repository.
   Cite the source filename and section for every factual claim.
   If the repository does not contain an answer, say so.
   Question: {{trigger_event.payload.chat.text}}
   ```

3. Optionally configure a result schema with a required string `text` field; have
   the flow put its answer and citations in that field. Native structured results
   use the same validated-result path. Otherwise use Codex or Claude Code's final
   assistant message.
4. Link your account and send `ask docs How do I request a review?`. Follow the
   acknowledgement link to inspect the run; the cited answer arrives in chat.

The payload also exposes `trigger_event.payload.chat.sender_display_name` and
`trigger_event.payload.chat.connection_name`. It contains no bot tokens or
provider user/channel IDs. The separate origin metadata is recorded by Preloop
for attribution and durable correlation. The repository clone supplies the
corpus; no retrieval or vector-search service is involved.

## Cloud documentation support

When the optional Cloud support service is configured, **Help → Ask Preloop
support** and the public Cloud home/pricing/signup launcher open a separate support
conversation. It uses public, versioned documentation sources with citations and
an independent Preloop support budget. **Inspect my setup** explicitly reads only
setup statuses allowed by your signed-in account permissions. It does not expose
raw audit logs or run arbitrary MCP tools.

Messages may be reviewed by a human. Each message or choice creates an ordered
private operator-thread entry; merely viewing or dismissing a prompt sends no
operator notification. Human takeover blocks pending assistant responses, and
operator thread replies return to the browser. Anonymous credentials authorize one
conversation, and supplying contact information never authorizes account inspection.
Self-hosted public sites do not load the Cloud widget by default. See your Cloud
operator guide for service configuration, retention, budgets and delivery recovery.


Discord installation persists its callback target before remote changes. If installation
fails, retry **Finish** with the same workspace; the saved connection supports that
retry. A command-installation failure restores the prior callback when it still
points to this attempt. An interrupted rollback retains a durable callback target.
Slack OAuth must complete in the browser that initiated setup; its short-lived
secure HttpOnly SameSite=Lax proof is never returned in the authorization URL.

## Teams setup

This is the **own-bot, single-tenant** path. Each installation supplies its own
Entra app registration and Azure Bot resource. **A Teams administrator must allow
the app** before people can use it. There is no shared Cloud bot or Store listing.
The setup verifies token acquisition; it does not prove the app is installed or
that Microsoft will deliver activities. Real-tenant behaviour remains unverified.

### Installation checklist and roles

| Step | Who does it | What blocks without that role |
| --- | --- | --- |
| Set a public HTTPS `PRELOOP_URL`, apply migrations, and run the chat worker. | Preloop operator | Microsoft cannot reach an intranet-only endpoint. There is no Teams socket transport here. |
| Register an Entra app with **Accounts in this organizational directory only (Single tenant)**. Copy its Application (client) ID, Directory (tenant) ID, and a client secret **value**, not its ID. | App registrant; Entra administrator if app registration is restricted | Without permission to register an app, setup cannot start. The tenant's default registration policy is unverified. |
| Create an Azure Bot resource, **Single Tenant**, using that app ID and tenant ID. Select the appropriate region and Free tier for Teams. | Azure subscription Contributor | An active Azure subscription and resource-creation permission are required. |
| Choose **Microsoft Teams** in Preloop Chat connections and enter the three values. Under **Advanced manifest URLs**, set developer, privacy and terms pages; blanks use the installation's own origin. | Preloop user with `manage_policies` | Users without connection-management permission cannot save setup or download its package. |
| Paste Preloop's callback URL into the Azure Bot messaging endpoint and enable its Microsoft Teams channel. Download the generated app package; select **Finish** in Preloop to activate the endpoint. | Azure Bot resource operator and Preloop connection manager | Draft callbacks accept no Teams activities. Without channel configuration, Teams cannot deliver messages. |
| Upload the zip through Teams admin center → Teams apps → Manage apps → Upload new app. | Teams Administrator or Global Administrator | Users may submit apps for approval, but an administrator must approve them. Sideloading also depends on administrator-controlled custom-app and setup policies. |
| Allow the app for intended users through app permissions or app-centric management. Optionally pre-install it with an app setup policy. | Teams Administrator | Users cannot find or open a blocked app. Catalog availability and policy changes can each take **a few hours** to propagate. |
| Open the app in Teams, select **Link my account** in Preloop, and send `link CODE` in its personal chat. | Each user | A token or Entra object ID alone does not link a Preloop identity. Proactive messages require the personal app installation. |
| Rotate expiring secrets and re-upload changed manifests. | App registrant, connection manager and Teams Administrator | An expired secret stops token acquisition and delivery. |

Microsoft documents a Teams-managed registration as an alternative that needs no
Azure subscription, but it is tied to its creator and is **not verified by this
implementation**. Use the Azure Bot path above for this release. Regional Bot
Service resources do not prove that the whole Teams message path remains in that
region; Microsoft warns that other services and channels can cross boundaries.

### Behaviour and network access

Personal chats support `link`, plain questions, `note`, `message`, `approve`,
`deny`, `ask ALIAS TEXT` and `help`, with the same permissions and durable broker
as Slack. For example, `ask docs Where is the policy?` returns its acknowledgement,
approval-wait notice and result in personal chat. Proactive approval notices use
the latest authenticated conversation address saved for the linked identity.
Only personal conversations can consume link codes. Unlinking or disabling the
connection is rechecked before any queued answer is sent.

Both tenant fields on every activity must match the connection's directory ID;
the recipient must be its bot. Connector authentication requires RS256, issuer,
audience, expiration, not-before, an `msteams`-endorsed key, and an exact
`serviceUrl` claim match. The bearer-token destination is checked both when an
activity arrives and immediately before delivery. There is no validation bypass.
The client secret is stored only in encrypted setup/connection credentials;
access tokens and signing keys have bounded in-memory caches.

Allow outbound HTTPS to `login.microsoftonline.com`, `login.botframework.com`,
and `smba.trafficmanager.net`. Inbound traffic reaches the displayed public
`/api/v1/chat/ingress/{connection_id}` endpoint. Requests do not follow redirects
or use environment HTTP proxies (`trust_env=False`). `CHAT_TEAMS_SERVICE_HOSTS`
is an operator-controlled comma-separated exact host allow-list, defaulting to
`smba.trafficmanager.net`; it must be the same on API and worker. Observed hosts
remain unverified: confirm any additional Microsoft-owned host before adding it.
Never add an arbitrary host to get past an authentication error.

Replies use `textFormat: plain` and the existing 8,000-character output bound.
Channel mentions, group chats, bot Adaptive Cards, files, SSO, Graph permissions,
message updates, streaming, government/sovereign clouds and proactive Graph
installation are outside scope. Workflows notices below use a separate card path.
HTTP 412/429 returns a delivery to pending, respects `Retry-After` (bounded to
one hour), and stops after three attempts. A 403 records the fixed message
“Teams refused delivery; the user must open the Preloop app in Teams”. Timeouts
and 5xx during sending remain **uncertain**, requiring inspection before retry;
no Connector idempotency facility was verified. Microsoft documents conversation
limits of 7 sends/second, 60/30 seconds and 1,800/hour, plus 50 requests/second per
app per tenant; actual throttling is unverified.

### Secret rotation

Before expiry, create a replacement Entra client secret and start a new Teams
connection using the same app and tenant IDs. Download its new package, update
the Azure Bot callback to the new connection, finish setup, and have the Teams
administrator replace/allow the package. Users must link to the new connection.
Verify worker health and a personal reply, then disable the old connection and
revoke the old secret. This release uses replacement setup for rotation; it does
not offer an in-place secret editor. Token caching includes the secret digest,
so a changed secret cannot reuse an old cached token. Keep the previous secret
only for the overlap needed to verify the replacement.

### First real tenant checklist

Local tests use synthetic documented activities, a local RSA key and fake HTTP;
**they do not prove any of the following**. The first installation must:

1. Record the observed **issuer and claim names/types** for the single-tenant bot;
   confirm `aud`, `exp`, `nbf` and `serviceUrl` pass validation. Do not report JWTs,
   secrets or full personal activity bodies.
2. Record the **service URL hostnames** and region path shape, and confirm ingress
   and replies work with the configured host allow-list.
3. Run the zip through the Developer Portal validator and Teams admin center;
   record acceptance/errors, manifest version and policy propagation time.
4. Test **proactive 403 before personal installation** and successful delivery
   after opening the app. Verify linking, questions, each command, `ask`, its
   approval-wait notice and final result, plus unlink/disable cancellation.
5. Exercise controlled **throttling**, record status and `Retry-After` shape,
   confirm pending retries and the three-attempt bound without duplicate actions.
6. Inspect **plain-text rendering on desktop and mobile**, especially links,
   mentions and markup. Inspect the Workflows card's single Review button and
   large-text truncation. Report client versions and anonymized results.

All six checks remain **unverified**, including the actual single-tenant issuer,
observed service hosts, package acceptance, before/after-install 403 behaviour,
429 behaviour and rendering. Manifest 1.30 is validated locally against
[Microsoft's published JSON schema](https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json),
vendored under `backend/tests/fixtures/teams/` for deterministic tests. Schema
validation does not substitute for Developer Portal or admin-center acceptance.

Sources: [Bot registration](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-quickstart-registration?view=azure-bot-service-4.0),
[registration choices](https://learn.microsoft.com/en-us/microsoftteams/platform/agents-in-teams/choose-agent-registration-type),
[Connector authentication](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication?view=azure-bot-service-4.0),
[tenant validation](https://learn.microsoft.com/en-us/microsoftteams/platform/agents-in-teams/agent-tenancy),
[custom app administration](https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings),
[permission propagation](https://learn.microsoft.com/en-us/microsoftteams/teams-app-permission-policies),
[proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages),
[rate limits](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/rate-limit),
[formatting](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/format-your-bot-messages),
[regionalization](https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-concept-regionalization?view=azure-bot-service-4.0).

## Teams Workflows approval notices

For notices without a bot, a team member can create a Workflows webhook with
**Send webhook alerts to a channel**, then select **Teams Workflows** as the
approval workflow type and paste its URL. Policy YAML can use
`channel_configs.teams.url`. This path needs no Entra app, Azure Bot resource or
Teams administrator to register a bot. Tenant Power Platform policies may still
restrict Workflows; that has not been verified on a real tenant.

The Adaptive Card has the approval headline, tool, status and one **Review**
link to the existing approval page. It does not accept chat commands or decisions
inside Teams, and posts to the workflow's configured channel/chat rather than to
each approver. Policy notify rules routed to the same workflow send an
informational card with the rule, redacted excerpt, an explicit statement that
this notify rule did not block the call, and one **See all notices** link. Those
cards have no approval decision actions. Both use the existing durable webhook
outbox and retry policy.
Display text is shortened to fit the 28 KB message limit; an oversized review
URL is rejected without queueing a broken link. Microsoft documents throttling
above four requests per second. The workflow belongs to its creator: assign a
co-owner and plan handover so it is not orphaned when that person leaves.

Source: [Microsoft incoming webhook and Workflows documentation](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook).
Rendering, tenant policy availability and throttling remain unverified here.
