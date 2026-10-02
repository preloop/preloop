# Chat connections

Preloop can receive authenticated Slack events, Mattermost slash commands, and
Discord interactions. Replies and approval notices go to a verified recipient's
direct messages. The assistant uses the linked human's current permissions and
visible account default model through the existing model gateway. It can read
agents, sessions, flows, models, and spend; it has no CLI, shell, or write tools.

Apply database migrations, configure the provider in **Settings → Chat
connections**, and run the durable worker as a separate supervised process:

```sh
python -m preloop.services.chat_worker
```

The worker uses the `chat` service role: full API routes plus request-governance
plugin initialization and NATS connectivity, without the API's unrelated
execution, webhook, and notification schedulers. Startup fails if required
request-governance initialization fails. Use the same database, encryption key,
JWT signing configuration, plugins, and NATS configuration as the API deployment.
Provider credentials are encrypted at rest and never returned in connection
responses or supplied to the model. Creating/disabling connections requires
`manage_policies`; ordinary members can view their own linking and delivery state.

## Provider configuration

The connection's `ingress_url` is its public HTTPS provider callback. Connection
IDs identify the destination; they do not authenticate requests.

- **Slack:** configure an Events API subscription for `message.im` and
  `app_mention`. Supply the signing secret, bot token, team ID, and bot user ID.
  Grant `chat:write` and the scopes needed for receiving those subscriptions and
  `conversations.open` (`im:write`). Preloop verifies the signed raw body and
  timestamp, rejects other teams, ignores bot/subtype events and ordinary public
  channel messages, and answers through `conversations.open` followed by DM
  `chat.postMessage`. App mentions require the configured bot user ID for command
  normalization.
- **Mattermost:** configure a `/preloop` slash command pointing to the callback.
  Supply its command verification token, team ID, bot token, bot user ID, and the
  server's HTTPS origin. The token authenticates the immutable command identity;
  trigger/post IDs provide durable replay deduplication. Responses acknowledge
  privately and the bot opens a direct channel to deliver the answer.
- **Discord:** register `/preloop` with one string option named `message`, and set
  its interactions endpoint to the callback. Supply the application Ed25519
  public key, bot token, and guild/server ID. Preloop verifies the signature and
  timestamp, accepts only that guild's `/preloop` interactions, acknowledges
  ephemerally, and opens a direct message. Members must permit the bot's DMs.
  This adapter handles slash interactions, not Discord Gateway member/channel
  subscriptions; use the employee event bridge for those subscriptions.

Preloop does not provision provider applications, register slash commands, or
send setup/test messages automatically. Provider secrets can be replaced by
creating a new connection and disabling the old one; the current management API
supports enabling/disabling connections, not in-place credential rotation.

## Link a human identity

Generate a code in the connection card while signed into Preloop. Codes expire
in ten minutes and are consumed once by an authenticated provider event. New
codes invalidate previous outstanding codes; unlinking invalidates outstanding
codes too. The durable receipt stores only the code digest, not the plaintext.
An external identity already linked to another user cannot be claimed; unlink
first from the authenticated dashboard.

Use the provider's supported input:

| Provider | Linking command |
| --- | --- |
| Slack | Send the plain DM `link CODE` (not a Slack slash command). |
| Mattermost | Run `/preloop /link CODE`. |
| Discord | Run `/preloop` with `message` set to `/link CODE`. |

Slack also accepts a bot mention followed by a recognized plain command, but
linking must use a DM. Unlinked users receive only fixed linking guidance.
Every action and protected delivery reloads the connection, active account,
active user, identity, and current role/resource authorization. A deleted user's
saved reply is cancelled even though its user foreign key becomes null.

## Ask and act

Ask natural-language questions through the same provider input. Lists expose at
most twenty authorized entries and never an unfiltered account count. Spend
uses a fixed rolling thirty-day window captured on the first read, including
today; the window survives saved-reply validation. Account aggregate spend is
unavailable when a resource-specific authorizer is installed, because the
existing aggregate endpoint cannot prove that hidden-resource spend was removed.

Effectful actions require explicit human commands. They are never model tools:

| Action | Internal command |
| --- | --- |
| Steer a session at its next boundary | `/note SESSION_UUID TEXT` |
| Send a prompt to a selected session | `/message AGENT_UUID SESSION_UUID TEXT` |
| Vote on an approval | `/approve REQUEST_UUID` or `/deny REQUEST_UUID` |

For Slack, omit the leading slash in plain DMs. For Mattermost, prepend
`/preloop ` to the internal command. For Discord, put the internal command in
`/preloop`'s `message` option. These actions use the existing operator-note,
Agent Control prompt, and approval decision APIs with a short-lived internal
user JWT. The model never receives that JWT or provider credentials. Notes keep
the human author and record `source=chat` with the work UUID; prompts record the
same source/correlation metadata. Caller-provided provenance is informational,
never an authorization grant. Approval votes additionally check current workflow
user/team eligibility and preserve existing quorum, expiry, and vote deduplication.
The worker routes existing eligible approval recipients to verified linked DMs.

## Delivery and recovery

Provider acknowledgments mean persisted receipt, not task completion. Ingress
is bounded to 64 KiB, messages to 8,000 characters, assistant runs to four model
turns, five tool calls per turn, 700 output tokens per turn, 48,000 context
characters, and ninety seconds for generation/actions. Independent questions do
not share model history. Tool data is projected before entering model context.

The PostgreSQL queue deduplicates `(connection, provider event ID)`, claims with
row locks and a lease token, and fences stale workers. Safe processing can resume
after an expired lease; abandoned processing stops after three attempts. A saved
reply rechecks every scoped read and target resource before disclosure. If data
or access changed, the reply fails or is cancelled and the user should ask again.
Provider rejection is visible as `failed`. A timeout/crash during an effectful
POST or outbound send becomes `uncertain` and is never automatically replayed.
Inspect the existing action/provider history before resubmitting; ambiguous
writes do not have a universal provider idempotency guarantee. Slack client IDs,
Mattermost pending-post IDs, and Discord nonces are supplied as additional
provider deduplication hints.

The connection card shows the current user's last fifty delivery outcomes. The
backend does not yet expose an operator retry/repair endpoint or automatic queue
history retention. Supervise the worker and monitor failed/uncertain work in
operations tooling; do not equate a running API with an active chat worker.

Disable a connection and stop its worker to roll back chat ingress/delivery.
Existing histories remain available. Database rollback drops the four additive
chat tables and should only be used after preserving any required history.

## Local verification

Use an isolated synthetic PostgreSQL database, run Alembic to the current head,
and set `CHAT_TEST_DATABASE_URL` and `DATABASE_URL` to that database:

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend pytest --noconftest \
  backend/tests/services/test_chat_backend.py \
  backend/tests/services/test_chat_providers.py
```

These tests exercise real persistence, leases, provider signature validation,
JWT-authenticated ASGI routes, resource denial, default-model selection,
approval vote/quorum behavior, identity revocation/deletion, and private delivery.
Provider sends and model generation use synthetic mocks; no real-user messages
are delivered. They do not establish real provider connectivity or exercise a
complete live multi-process production rollout.
