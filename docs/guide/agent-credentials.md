# Agent credentials in the console

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Open **Agents**, select an agent, and use its **Credentials** card to manage
keys. The card lists each key's name, prefix, creation time, last use, expiry,
and status. Settings > API keys links to the agent list.

Choose **Create key**, enter a name, and select an expiry. Copy the full key
from the result dialog and store it securely before closing it. The full key
is shown only at creation and cannot be retrieved later. To stop a key from
working, choose **Revoke** and confirm. Revocation cannot be undone.

Viewing credentials requires `view_agents`. Creating and revoking requires
`manage_agents`, and only active agents can receive new keys. Shared agents
are managed from their owning account. The server enforces these rules.

## Reusing the create action

Console integrations can call `AgentDetailView.openCreateCredential()` or
`AgentCredentialsCard.openCreate()` to open the same dialog. The promise
resolves to `ManagedAgentCredentialCreateResult` after successful creation,
or `null` on cancellation or when the action is unavailable. The result
contains the credential metadata and the one-time `token`. Keep that token
only in the active setup view's memory and clear it on dismissal or navigation.
Do not put it in local storage, URLs, logs, or DOM events.

The card uses the existing agent credential API. No new scopes or permissions
are added, and the key is bound to the selected agent by the server.
