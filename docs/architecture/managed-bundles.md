# Managed bundle registry and control expectation storage

Core provides storage and authenticated registration. Fleet evidence generation,
drift evaluation, settings integration and finding resolution are extension work.
Nothing in this registry claims installation, attestation or effective control.

## Registration and revision bytes

The CLI emits `policy_revision_hash` as lowercase SHA-256 over exactly the
`managed-settings.json` UTF-8 artifact bytes, including its two-space indentation
and trailing LF. Only the rendered policy is hashed. `manifest.json` and
`preview.txt` are excluded. Identical exports have identical hashes.

Export stays offline unless `--register` is supplied. That flag writes the bundle
first, then posts the manifest to `POST /api/v1/agents/managed-config/bundles`
with the selected CLI authentication. There is no policy-content upload. The API
rejects unknown fields, including account overrides and credential fields.
`manage_agents` is required for POST; `view_agents` for GET. Core's normal OSS
account-member permission behavior and optional authorizer/RBAC apply. The account
always comes from the authenticated membership.

`RegisteredBundleRevision` stores immutable metadata per
`(account_id, app_id, platform, policy_revision_hash)`. Only one revision is active
per account/app/platform. An identical registration is idempotent. New registration
retires the prior revision and its active bundle-origin expectations in one
transaction. Other platforms, accounts and manual rows are untouched. Changed
metadata for an existing hash or replaying a retired revision raises a conflict;
there is no implicit rollback through replay. Historical rows retain `retired_at`. Registration audit entries include the
authenticated `user_id` and `config_type="managed_bundle"` for timeline filtering;
trusted system callers may omit the optional CRUD `user_id`.

GET returns active `bundles`, their `generated_count`, and an account-wide
`generated_count`. Counts include all active `origin="bundle"` rows, without a
500-row settings/display cap. POST returns the registered bundle and its count.
Neither endpoint generates expectations. In core alone, the count stays zero.

## Expectation mapping

OSS previously had no `ControlExpectation` model. The new table maps the drift
identity directly to these columns:

| Drift identity | OSS column |
| --- | --- |
| Authenticated evidence source | `source_ref` (UUID) |
| Workstation fingerprint | `workstation_fingerprint` (SHA-256 hex) |
| Evidence application kind | `agent_kind` |
| Hashed configuration path | `config_path_hash` (SHA-256 hex) |
| Control axis | `axis` (discovery `ControlAxis` vocabulary) |
| Policy revision | `policy_revision_hash` (SHA-256 hex) |

`account_id` and `origin` namespace this tuple. Separate `bundle` and `manual`
rows may coexist for an identical tuple so regeneration never takes ownership
of manual data. `required_scope` carries the scope requirement. Bundle rows also
have `bundle_revision_id`; a composite foreign key enforces the same owning
account. Manual rows cannot reference a bundle. `retired_at` preserves lifecycle
history without requiring settings JSON updates.

Manifest app `claude-code` maps to evidence `agent_kind="claude_code"` (the
existing CLI evidence reporter spelling). This bundle declares only
`axis="native_tool_hook"`, `required_scope="managed_config"`. No other axis or
application is inferred from its metadata.

## Extension generator contract

Use `from preloop.models import models` and import the following instances from
`preloop.models.crud`. All calls require an explicit trusted `account_id` and
share the caller's transaction; CRUD flushes but does not commit.

1. Reconcile on registered-revision changes, new/changed evidence contexts and
   scheduled evaluation. Core does not invoke a generator or use user enrollments
   as generation input. Call `crud_bundle_revision.list_active(db, account_id=...)`
   to obtain the registered app/platform/revision and its ID. Administrative API
   clients use POST/GET `/api/v1/agents/managed-config/bundles`.
2. Read the account's observations with
   `crud_discovery_observation.list_for_account(...)`. Page or partition reads as
   appropriate; its default result limit is not the fleet's size. Match the
   registered platform to `evidence.os_family`, and the app to an application in
   `evidence.applications`. Use that report's source UUID, workstation fingerprint,
   evidence agent kind and configuration path hash. User-config enrollment from
   `agents onboard` is insufficient.
3. Build `ControlExpectationInput` from the tuple above and call
   `crud_control_expectation.upsert(db, account_id=..., expectation=...,
   origin="bundle", bundle_revision_id=...)`. CRUD independently checks the
   current registration and persisted account/source/workstation/application/
   platform/context evidence, plus the declared axis/scope. A stale generator
   cannot write a superseded revision. Repeated eligible writes update the same
   row, and can revive a retired context while its revision is still current.
   If an identical tuple moves between matching platform registrations, its
   bundle reference follows the latest eligible write.
4. Read `crud_control_expectation.list_active(db, account_id=..., origin="bundle")`
   and combine these with manual settings expectations for evaluation. Keep the
   settings PUT confined to manual settings; generated rows live outside that
   document. Do not accept generated row edits through a settings endpoint.
5. When a context is retired, call
   `crud_control_expectation.retire_bundle_scope(db, account_id=..., source_ref=...,
   workstation_fingerprint=..., agent_kind=..., config_path_hash=...)`. This retires
   only that context's bundle rows. Revision registration already retires the
   superseded revision's rows. Reconcile open findings against active tuples and
   resolve removed/superseded scopes in the extension's finding lifecycle.
6. Expose `crud_control_expectation.generated_count(db, account_id=...,
   bundle_revision_id=...)` in extension settings responses. The optional bundle
   filter can be omitted for the account total. Commit generator changes and
   finding transitions together, or roll back together on failure.

`origin="manual"` is available to trusted CRUD callers without bundle eligibility;
core does not migrate existing manual settings into this table. Registration and
bundle writes serialize using the same account lock as observation ingestion, so
concurrent registration cannot leave active rows for a retired revision. There is
no public expectation-write endpoint.
