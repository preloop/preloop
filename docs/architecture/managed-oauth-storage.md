# Managed tracker OAuth storage

`preloop.models.crud.crud_managed_oauth` owns the provider-neutral storage
contract. Import model types through `from preloop.models import models`.
Provider services, callbacks, HTTP refresh clients and console flows are separate
work. Existing GitHub user tokens and pasted `oauth_token` trackers remain
unmanaged; the migration does not convert credentials.

## Configuration and connection

Create an account-scoped `OAuthProviderConfiguration` with provider, canonical
HTTPS instance (including a DC context path), context, client ID, optional client
secret, exact callback URI and selected permissions. Consumer secrets use dedicated tenant-owned encrypted `SecretReference` rows.
Grant tokens remain encrypted in `OAuthToken`, never copied into tracker
`api_key` or `SecretReference`. Configuration metadata excludes the secret reference and reports only a
`has_client_secret` readiness boolean.
Provider/instance/context are immutable consumer identity; use a new configuration
to change them. Credential/callback/permission replacement increments the version,
invalidates old handshakes (including completed callback retries), erases old grant
secrets, disables their trackers and requires reconnect. Omitted `enabled` preserves
the configuration's existing state. Unsupported tracker providers are rejected at
configuration creation.
It does not delete the provider consumer.

`begin_connection` returns metadata and a cryptographically random state once.
Storage contains hashes of state and session identity and an optional encrypted
PKCE verifier, with a ten-minute UTC expiry. The caller supplies an authenticated
account/user and stable login-session identity. `claim_callback` verifies that
binding and the exact callback URI, consumes the state once, and returns an
explicit internal-only `CallbackSecrets` value for provider I/O. PKCE is erased
when claimed. A failed exchange requires a new connection transaction.

After exchanging the code, call `store_pending_grant` with provider subject and a
`TokenPair`. Services derive timezone-aware UTC expiry from `expires_in`; naive
expiry is rejected. `complete_connection` creates a tracker or reconnects an
existing managed tracker. Same-owner completion retries return its original
tracker, including after expiry. Reconnect to an attached grant requires its
current rotation version and configuration identity. A disconnected tracker takes
a new grant; its old grant ID remains an unusable tombstone. Tenant equality is
also enforced by composite database foreign keys. Workspace identities never
need an OAuth app installation.

## Rotation and lifecycle

Every mutation creates and commits its own session; it cannot commit unrelated
caller work. Do not pass uncommitted caller-created records into this contract.
The lock order is configuration, connection transaction, tracker, grant. Ordinary
operations use a shared configuration row lock; replacement uses an exclusive
lock. Rotation locks and rereads the grant before invoking the provider callback,
then atomically persists both encrypted tokens and an incremented version.
`TokenPair.issued_at` records the provider response receipt anchor separately
from mutable row timestamps. Services compute bounded expiry skew from
`expires_at - issued_at` without inventing refresh-token lifetimes. This nullable
field preserves legacy consumer behavior, rotates with the pair, survives
metadata-only updates, and is erased on disconnect or configuration replacement.
Competing calls with the old version fail before provider I/O. Different grants
can rotate independently, including under one configuration.

`rotate` is synchronous: async services must dispatch it to a worker thread.
Its callback receives detached `TokenPair`/`CallbackSecrets` values and a transport
timeout (default ten seconds, maximum sixty). The storage wait has the same hard
bound. A late result has no database session and cannot persist credentials;
providers must honor the supplied transport timeout to release network resources.
Exceptions and timeouts roll back. A provider that invalidates a refresh token
before a local timeout/commit failure may require reconnect; storage cannot roll
back provider-side rotation. Never log provider inputs or results.

`disconnect` locks the grant, erases its secrets, increments its version, detaches
and disables its tracker. Configuration and consumer survive. Account deletion
cascades grants and handshakes. Run tenant-scoped `cleanup_expired` periodically to
remove unfinished transactions and their pending secrets. Completed transactions
retain the owner-bound retry mapping. All model `to_dict` implementations expose
metadata only. Plaintext token values never enter the tracker ORM credential cache.

The nullable schema upgrade preserves legacy tokens and interprets their naive
expiry timestamps as UTC. Rollback drops the new storage and erases managed grants;
managed connections require reconnect after a subsequent upgrade. Legacy GitHub
tokens and manual tracker credentials remain intact. PostgreSQL tests exercise
upgrade/rollback, tenant constraints, replay, concurrent completion/refresh,
configuration replacement, stale writes, ciphertext and rollback isolation.
