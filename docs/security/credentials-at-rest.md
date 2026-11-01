# Credentials at rest

Every credential Preloop stores is either encrypted with the server's Fernet
key or kept only as a one-way hash. This page lists each stored column that
could hold one, how it is protected, and the migration that introduced that
protection. A test (`backend/tests/models/test_credentials_at_rest_doc.py`)
fails when a model gains a column whose name contains `secret`, `token`,
`key`, `password` or `credential` without an entry here.

## Key management

All encryption goes through `preloop.utils.encryption` (Fernet). The key is
`SECURITY__ENCRYPTION_KEY`, or, when unset, a key derived from
`SECURITY__SECRET_KEY`. There is one key for the whole deployment; changing it
makes stored credentials unreadable, so rotate it only with a re-encryption
plan.

Three forms of encryption are in use:

- **Fernet**: the column holds a Fernet token written by the owning service.
- **Reference**: the column points to a `secret_reference` row whose
  `encrypted_value` is a Fernet token (or an external secret manager
  reference).
- **Sealed**: the column type (`EncryptedText` or `SecretFieldsJSON` in
  `preloop.models.db.encrypted_types`) stores `enc:v1:<fernet token>` and
  decrypts on read, so application code sees plaintext.

## Sealing existing rows

Revision `20261119_credentials_at_rest` changes no data in the migration hook.
It registers one backfill per sealed column in `agent_usage_backfill`; the
sync scheduler seals existing rows after the deploy, 200 rows per committed
batch, from a cursor per column. Rows already sealed are skipped, so the
backfill can be stopped and rerun. Until it finishes, the application reads
both sealed and plaintext values.

Operators can run it by hand and get counts per column (never values):

```bash
python -m preloop.models.credential_encryption --dry-run   # count plaintext
python -m preloop.models.credential_encryption             # seal everything
```

The manual run scans every row, so it also seals values that a previous
release wrote during a rolling upgrade. Before rolling back to a release
without encryption at rest, run
`python -m preloop.models.credential_encryption --decrypt`; that release
cannot read sealed values.

## Credentials inside JSON documents

| Column | Fields sealed | Since |
| --- | --- | --- |
| `mcp_server.auth_config` | Every string under a secret key (`token`, `api_key`, `client_secret`, header values such as `Authorization`); endpoints, `client_id` and scopes stay readable | `20261119_credentials_at_rest` |
| `flow.webhook_config` | `webhook_secret` | `20261119_credentials_at_rest` |
| `approval_workflow.approval_config` | `webhook_secret` | `20261119_credentials_at_rest` |
| `policy_snapshot.snapshot_data` | `mcp_servers[].auth_config` secrets and any `webhook_secret` kept for rollback | `20261119_credentials_at_rest` |

Other encrypted columns whose names do not match the audit pattern:
`secret_reference.encrypted_value`, `support_conversation.contact_encrypted`,
`support_message.content_encrypted`,
`upstream_mcp_oauth_transaction.context_encrypted` and
`oauth_connection_transaction.pkce_verifier_encrypted` (all Fernet).

## Legacy columns not mapped by the application

`ai_model.api_key`, `api_key.key` and `tracker.jira_webhook_secret` stay in
the database for rolling upgrades but are no longer mapped. Revision
`20261110_credential_backfill` moved their values to `secret_reference` rows
or hashes; the release that drops them reruns that backfill first.

## Deferred

`approval_request.approval_token`, `user_invitation.token` and
`registration_token.token` are random link tokens stored in plaintext. They
are looked up by equality and re-sent in reminders, so encrypting them needs a
lookup hash next to a sealed copy rather than a column type change. They are
tracked as follow-up work.

## Column audit

| Column | Protection | Since |
| --- | --- | --- |
| `account_signing_key.key_id` | Not a secret: identifier, dedupe or lookup key | `20260910_audit_chain_signing` |
| `account_signing_key.public_key` | Not a secret: public key | `20260910_audit_chain_signing` |
| `account_signing_key.private_key_encrypted` | Fernet | `20260910_audit_chain_signing` |
| `account_sso_connection.oidc_client_secret_ref_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20261112_enterprise_sso` |
| `account_sso_connection.saml_sp_signing_key_ref_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20261112_enterprise_sso` |
| `account_verified_domain.challenge_token` | Not a secret: published in DNS for domain verification | `20261112_enterprise_sso` |
| `activity_rollup.prompt_tokens` | Not a secret: count, type or timestamp | `20261031_live_activity` |
| `activity_rollup.completion_tokens` | Not a secret: count, type or timestamp | `20261031_live_activity` |
| `agent_usage_total.prompt_tokens` | Not a secret: count, type or timestamp | - |
| `agent_usage_total.completion_tokens` | Not a secret: count, type or timestamp | - |
| `agent_usage_total.total_tokens` | Not a secret: count, type or timestamp | - |
| `agent_usage_total.cache_read_tokens` | Not a secret: count, type or timestamp | - |
| `agent_usage_total.cache_write_tokens` | Not a secret: count, type or timestamp | - |
| `agent_usage_total.covered_prompt_tokens` | Not a secret: count, type or timestamp | - |
| `ai_model.credentials_secret_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260308_add_secret_references_for_ai_models` |
| `anthropic_import_connection.secret_reference_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20261010_anthropic_import` |
| `anthropic_import_connection.key_hint` | Not a secret: last four characters | `20261010_anthropic_import` |
| `anthropic_import_connection.gateway_key_names` | Not a secret: key names | `20261010_anthropic_import` |
| `api_key.key_hash` | SHA-256 hash; plaintext column removed from the mapping by `20261110_credential_backfill` | `20260307_add_hashed_api_key_support` |
| `api_key.key_prefix` | Not a secret: display prefix | `20260307_add_hashed_api_key_support` |
| `api_key.credential_type` | Not a secret: metadata | `20261004_ci_principal` |
| `api_key.credential_version` | Not a secret: metadata | `20261004_ci_principal` |
| `api_usage.api_key_id` | Not a secret: foreign key | `20260309_add_gateway_usage_fields` |
| `api_usage.prompt_tokens` | Not a secret: count, type or timestamp | `20260309_add_gateway_usage_fields` |
| `api_usage.completion_tokens` | Not a secret: count, type or timestamp | `20260309_add_gateway_usage_fields` |
| `api_usage.total_tokens` | Not a secret: count, type or timestamp | `20260309_add_gateway_usage_fields` |
| `api_usage.cache_read_tokens` | Not a secret: count, type or timestamp | `20260712_add_api_usage_accuracy_columns` |
| `api_usage.cache_creation_tokens` | Not a secret: count, type or timestamp | `20260712_add_api_usage_accuracy_columns` |
| `api_usage.reasoning_tokens` | Not a secret: count, type or timestamp | `20260712_add_api_usage_accuracy_columns` |
| `approval_request.api_key_id` | Not a secret: foreign key | `20260906_approval_api_key` |
| `approval_request.approval_token` | **Plaintext, deferred**: bearer token for approval links; looked up by equality and re-sent in reminders, so it needs a lookup hash plus a sealed copy | `20251105_initial_schema` |
| `audit_chain_checkpoint.signing_key_id` | Not a secret: identifier, dedupe or lookup key | `20260910_audit_chain_signing` |
| `billing_operation.operation_key` | Not a secret: identifier, dedupe or lookup key | `20260912_billing_ops` |
| `callback_key_binding.signing_key_digest` | Not a secret: identifier, dedupe or lookup key | `20261010_callback_receipt` |
| `chat_connection.credentials_encrypted` | Fernet | `20261002_chat_connections` |
| `chat_listener.lease_token` | Not a secret: lease ownership marker | `20261027_chat_support` |
| `chat_work.lease_token` | Not a secret: lease ownership marker | `20261002_chat_connections` |
| `ci_principal.credential_version` | Not a secret: metadata | `20261004_ci_principal` |
| `cli_clients.token_fingerprint` | One-way hash (fingerprint) | `20260710_add_cli_clients` |
| `copilot_import_connection.secret_reference_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260927_copilot_import` |
| `copilot_import_connection.enterprise_secret_reference_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260927_copilot_import` |
| `desktop_endpoint.created_by_api_key_id` | Not a secret: foreign key | `20261011_desktop_views` |
| `desktop_view.view_token_hash` | One-way hash | `20261011_desktop_views` |
| `desktop_view.token_expires_at` | Not a secret: count, type or timestamp | `20261011_desktop_views` |
| `fleet_finding.subject_key` | Not a secret: identifier, dedupe or lookup key | `20261021_device_fleet` |
| `fleet_finding.device_key` | Not a secret: identifier, dedupe or lookup key | `20261021_device_fleet` |
| `flow_execution.initiating_ci_key_id` | Not a secret: foreign key | `20261004_ci_principal` |
| `flow_execution.webhook_delivery_key` | Not a secret: identifier, dedupe or lookup key | `20260908_webhook_delivery_key` |
| `flow_execution.total_tokens` | Not a secret: count, type or timestamp | `20251118_add_execution_metrics_to_flow_execution` |
| `flow_feedback.event_key` | Not a secret: identifier, dedupe or lookup key | `20260906_flow_feedback` |
| `flow_runner.token_hash` | One-way hash | `20260817_add_flow_runners` |
| `flow_thread.lease_token` | Not a secret: lease ownership marker | `20260906_flow_feedback` |
| `gateway_identity_provider.api_key_id` | Not a secret: foreign key | `20261010_gateway_idp` |
| `gateway_identity_provider.max_token_lifetime_seconds` | Not a secret: count, type or timestamp | `20261010_gateway_idp` |
| `gateway_subject.api_key_id` | Not a secret: foreign key | `20261009_gateway_subject` |
| `governance_session_digest.input_tokens` | Not a secret: count, type or timestamp | `20261016_governance_review` |
| `governance_session_digest.output_tokens` | Not a secret: count, type or timestamp | `20261016_governance_review` |
| `hosted_credit_ledger.operation_key` | Not a secret: identifier, dedupe or lookup key | `20261018_hosted_credit` |
| `hosted_spend_reservation.operation_key` | Not a secret: identifier, dedupe or lookup key | `20260912_hosted_spend` |
| `issue.key` | Not a secret: identifier, dedupe or lookup key | `20251105_initial_schema` |
| `issue_cost_execution.pr_key` | Not a secret: identifier, dedupe or lookup key | `20260927_issue_cost_rollup` |
| `issue_cost_execution.total_tokens` | Not a secret: count, type or timestamp | `20260927_issue_cost_rollup` |
| `issue_cost_pull_request.pr_key` | Not a secret: identifier, dedupe or lookup key | `20260927_issue_cost_rollup` |
| `issue_cost_rollup.issue_key` | Not a secret: identifier, dedupe or lookup key | `20260927_issue_cost_rollup` |
| `issue_cost_rollup.total_tokens` | Not a secret: count, type or timestamp | `20260927_issue_cost_rollup` |
| `managed_agent_ai_model_binding.config_key` | Not a secret: identifier, dedupe or lookup key | `20260406_managed_agent_model_bindings` |
| `managed_agent_credential.api_key_id` | Not a secret: foreign key | `20260311_managed_agent_identity` |
| `managed_agent_credential.credential_type` | Not a secret: metadata | `20260311_managed_agent_identity` |
| `managed_agent_credential.key_prefix` | Not a secret: identifier, dedupe or lookup key | `20260311_managed_agent_identity` |
| `managed_agent_enrollment.adapter_key` | Not a secret: identifier, dedupe or lookup key | `20260311_managed_agent_identity` |
| `model_price_overrides.prepaid_token_balance` | Not a secret: count, type or timestamp | `20260408_add_model_price_override_terms` |
| `notification_preferences.mobile_device_tokens` | Not a secret: push routing tokens, useful only with the server push credentials | `20251105_initial_schema` |
| `oauth_mcp_access_token.token_hash` | One-way hash | `20260213_add_oauth_mcp_tables` |
| `oauth_mcp_client.client_secret_hash` | One-way hash | `20260213_add_oauth_mcp_tables` |
| `oauth_mcp_client.client_secret_expires_at` | Not a secret: identifier, dedupe or lookup key | `20260213_add_oauth_mcp_tables` |
| `oauth_mcp_client.token_endpoint_auth_method` | Not a secret: count, type or timestamp | `20260213_add_oauth_mcp_tables` |
| `oauth_mcp_refresh_token.token_hash` | One-way hash | `20260213_add_oauth_mcp_tables` |
| `oauth_provider_configuration.client_secret_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20261002_managed_oauth` |
| `oauth_token.access_token_encrypted` | Fernet | `20260113_add_github_app_oauth_tables` |
| `oauth_token.refresh_token_encrypted` | Fernet | `20260113_add_github_app_oauth_tables` |
| `oauth_token.token_type` | Not a secret: count, type or timestamp | - |
| `oauth_token.refresh_token_expires_at` | Not a secret: count, type or timestamp | `20261002_managed_oauth` |
| `observation_verification.subject_key` | Not a secret: identifier, dedupe or lookup key | `20261028_observation_verification` |
| `organization.webhook_secret` | Sealed (`EncryptedText`) | `20261119_credentials_at_rest` |
| `owner_consent.request_key` | Not a secret: identifier, dedupe or lookup key | `20261022_owner_consent` |
| `person.hashed_password` | Password hash | `20261012_identity_backfill` |
| `person_password_credential.hashed_password` | Password hash | `20261012_identity_backfill` |
| `presence_state.device_key` | Not a secret: identifier, dedupe or lookup key | `20261031_live_activity` |
| `provider_billing_connection.secret_reference_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260712_add_provider_billing` |
| `provider_billing_snapshot.provider_api_key_id` | Not a secret: identifier, dedupe or lookup key | `20260712_add_provider_billing` |
| `provider_billing_snapshot.uncached_input_tokens` | Not a secret: count, type or timestamp | `20260712_add_provider_billing` |
| `provider_billing_snapshot.cached_input_tokens` | Not a secret: count, type or timestamp | `20260712_add_provider_billing` |
| `provider_billing_snapshot.cache_creation_tokens` | Not a secret: count, type or timestamp | `20260712_add_provider_billing` |
| `provider_billing_snapshot.output_tokens` | Not a secret: count, type or timestamp | `20260712_add_provider_billing` |
| `provider_billing_snapshot.dedup_key` | Not a secret: identifier, dedupe or lookup key | `20260712_add_provider_billing` |
| `readiness_job.lease_token` | Not a secret: lease ownership marker | `20261010_ticket_readiness` |
| `record_signature.signing_key_id` | Not a secret: identifier, dedupe or lookup key | `20260910_audit_chain_signing` |
| `registration_token.token` | **Plaintext, deferred**: one-time registration token looked up by equality; needs a lookup hash | `20251116_add_registration_token_table` |
| `resource_tag.key` | Not a secret: identifier, dedupe or lookup key | `20260928_share_tag_rule` |
| `runtime_session_activity.api_key_id` | Not a secret: foreign key | `20260310_runtime_session_activity` |
| `runtime_session_replay_run.input_delta_tokens` | Not a secret: count, type or timestamp | `20260705_add_runtime_session_replay_run` |
| `scim_token.token_hash` | One-way hash | `20261112_enterprise_sso` |
| `secret_reference.secret_kind` | Not a secret: metadata | `20260308_add_secret_references_for_ai_models` |
| `security_maintenance_item.identity_key` | Not a secret: identifier, dedupe or lookup key | `20260907_security_maintenance` |
| `security_maintenance_item.product_key` | Not a secret: identifier, dedupe or lookup key | `20260907_security_maintenance` |
| `security_maintenance_item.release_key` | Not a secret: identifier, dedupe or lookup key | `20260907_security_maintenance` |
| `security_maintenance_release.product_key` | Not a secret: identifier, dedupe or lookup key | `20260907_security_maintenance` |
| `security_maintenance_release.release_key` | Not a secret: identifier, dedupe or lookup key | `20260907_security_maintenance` |
| `service_cursor.lease_token` | Not a secret: lease ownership marker | `20261027_chat_support` |
| `session_search_document.api_key_id` | Not a secret: identifier, dedupe or lookup key | `20260915_session_search_document` |
| `support_outbox.lease_token` | Not a secret: lease ownership marker | `20261027_chat_support` |
| `tag_key_policy.key` | Not a secret: identifier, dedupe or lookup key | `20260928_share_tag_rule` |
| `telemetry_ingest_dedup.dedup_key` | Not a secret: identifier, dedupe or lookup key | `20261010_otlp_telemetry_ingest` |
| `telemetry_metric_series.series_key` | Not a secret: identifier, dedupe or lookup key | `20261010_otlp_telemetry_ingest` |
| `tracker.api_key` | Sealed (`EncryptedText`); legacy column, new writes use `credentials_secret_id` | `20261119_credentials_at_rest` |
| `tracker.credentials_secret_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260712_tracker_credentials_secret` |
| `tracker.webhook_secret_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20260712_tracker_credentials_secret` |
| `upstream_mcp_oauth_connection.secret_id` | Reference: value lives in `secret_reference.encrypted_value` (Fernet) | `20261110_upstream_mcp_oauth` |
| `user.hashed_password` | Password hash | `20251105_initial_schema` |
| `user_invitation.token` | **Plaintext, deferred**: invitation link token; looked up by equality and re-sent, same redesign as `approval_token` | `20251105_initial_schema` |
| `webauthn_credential.credential_id` | Not a secret: public credential identifier | `20260730_add_webauthn_credentials` |
| `webauthn_credential.public_key` | Not a secret: public key | `20260730_add_webauthn_credentials` |
| `webhook.secret` | Sealed (`EncryptedText`) | `20261119_credentials_at_rest` |
| `webhook_endpoint.initiating_ci_key_id` | Not a secret: foreign key | `20261004_ci_principal` |
| `webhook_endpoint.secret_encrypted` | Fernet | `20260908_event_webhooks` |
| `webhook_endpoint.secret_hint` | Not a secret: last characters shown in the console | `20260908_event_webhooks` |
