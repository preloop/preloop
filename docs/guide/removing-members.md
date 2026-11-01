# Removing a member

Removing or deactivating a member of an account revokes all of that member's
access to the account in one database transaction. The console's remove and
deactivate actions, bulk removal and `PATCH /api/v1/users/{id}` with
`"is_active": false` all use the same operation.

## What is revoked

- The membership is marked inactive.
- The member's browser sessions and access and refresh tokens for this
  account stop working on their next use.
- CLI sessions are revoked.
- MCP OAuth authorization codes, access tokens and refresh tokens are revoked.
- Team memberships in this account are removed.
- The member's personal API keys are deactivated. Runtime keys are
  deactivated and their runtime sessions end.
- Runners the member registered are removed, which revokes their tokens.
  Executions a removed runner was holding are stopped.

An `access.revoked` audit event records the reason, a count for each item
above, and the IDs of the machine credentials that were kept.

If the same person belongs to other accounts, those memberships, sessions and
keys are not affected.

## What is kept

Machine credentials that belong to the account keep working: CI identity keys
and managed-agent credentials. They are not transferred or deleted. To review
the ones a member created, call:

```http
GET /api/v1/users/{user_id}/machine-credentials
```

The response lists each credential's key ID, name, kind (`ci_principal` or
`managed_agent`), the identity it belongs to, and whether it is active. Rotate
or revoke them from the CI identity or agent settings if needed.

## Repeating and reactivating

Removing a member who is already removed changes nothing and writes no second
`access.revoked` event.

Reactivating a member (`"is_active": true`) re-enables the membership only,
subject to seat capacity. Revoked keys, sessions and runners stay revoked: the
member signs in again and creates new keys. An `access.restored` audit event is
written.

## Permissions

Removing a member and listing their machine credentials require the
`manage_users` permission and work only within the caller's account.
