# Credential storage upgrade

This upgrade removes `ai_model.api_key`, `api_key.key`, and
`tracker.jira_webhook_secret`. Model and webhook credentials use the existing
`SecretReference` service. All Preloop API keys, including user and mobile keys,
use SHA-256 fingerprints and a lookup prefix. A generated token is returned only
in its issuance response; listing or retrieving a key cannot recover it.
Existing tokens retain their scopes, expiry, revocation state, ownership and
machine restrictions. This migration covers these three fields; it is not a
complete inventory or audit of every stored secret.

## Preparation

Rehearse with a disposable copy and take a restorable backup. Protect old backups
and WAL: they can retain plaintext after the live schema is upgraded. Apply your
backup retention policy to these copies.

Keep the deployment's existing `SECURITY__ENCRYPTION_KEY` (Helm
`security.encryptionKey`) available to the migration job, the init-db job (it
seeds the configured provider key as an encrypted model credential) and every app
role. The chart passes the same references to each of them.
If using the existing derived-key fallback, retain the same `SECRET_KEY`. Do not
generate a new key during this upgrade. The backfill rejects the built-in
development key when legacy encrypted credentials need migration.

Stop API, gateway, worker, scheduled job and script processes that can write
credentials. Old versions reference the removed columns and cannot run after
removal. This upgrade requires a maintenance window; mixed versions are not
supported across the removal step. Use the new release's migration code with
the deployment's database and encryption configuration. Do not put secrets on
the command line or enable SQL parameter logging.

## Backfill, then remove

From `backend/preloop/models` in the new release's environment:

```sh
alembic upgrade 20261110_credential_backfill
```

This revision reads at most 250 records per batch in one transaction. It encrypts
model and webhook values into tenant-owned references (global references for
global models), hashes API keys, and clears legacy columns. Existing references
win over stale plaintext; ownership and local ciphertext are checked before
clearing. External references are preserved without contacting the provider.
Conflicting fingerprints, invalid references or encryption failure abort the
transaction. The migration locks the affected tables; keep writers stopped
between both stages.

Inspect counts without selecting secrets:

```sql
SELECT count(*) FROM ai_model WHERE api_key IS NOT NULL;
SELECT count(*) FROM api_key WHERE key IS NOT NULL;
SELECT count(*) FROM tracker WHERE jira_webhook_secret IS NOT NULL;
```

All three must be zero. Then run:

```sh
alembic upgrade head
```

Revision `20261111_credential_columns` takes an exclusive lock, refuses any
reintroduced plaintext, and removes the columns. If its guard fails, keep writers
stopped, investigate the old writer, downgrade to `20261110_prune_api_usage_idx`,
and rerun both stages. The backfill downgrade never restores plaintext and the
upgrade is idempotent. Do not stamp past checks or discard populated columns.

Start the new application and verify synthetic key issuance/authentication,
provider access and webhook signature verification. An ordinary `alembic upgrade
head` also runs both stages in order; splitting them provides the inspection
point. Empty fresh installs need no credential backfill, but still require the
normal production security settings.

## Compatibility and rollback

HTTP creation payloads and one-time token responses retain their shape. CRUD
consumes `api_key` and `jira_webhook_secret` inputs and persists references.
`ApiKey.key` is a write-only compatibility input that hashes immediately; reads
return `None`. Use `crud_api_key.issue` to receive `(row, token)` for delivery.
Hash-based rotations may continue supplying `key=None`; CRUD discards that
obsolete field and uses the supplied hash and prefix.
`create_with_owner` returns only the persisted row; callers that need a token
must retain their supplied `key_value` or use `issue`. The AI model and tracker
legacy ORM properties return `None`; direct constructor writes are unsupported.
Use the CRUD credential methods.

Downgrading the removal revision restores empty nullable columns while keeping
references and hashes. It cannot reconstruct API keys. Rollback code must support
secret references and hashed authentication; plaintext-only code is incompatible.
Restoring a pre-upgrade backup is a separate recovery operation and reintroduces
that backup's credentials and state. Retain encryption keys with their backups.

Resolution stays in the existing secret service and authorization paths. No new
export endpoint, backend or runtime access to provider credentials is added.
Only deployment operators should run the migration job.
