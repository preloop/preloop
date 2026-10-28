# Plugin attention sources

Editions: OSS. Contributor documentation for this repository.

An enabled plugin can implement `Plugin.get_attention_sources()` and return
`AttentionSource(id, permission, provider)` objects. The async provider receives
`(db, user)` and returns `list[ExtensionAttentionItem]`. Read through core CRUD and
scope every read to `user.account_id`. Providers run on the API event loop and must
offload synchronous CRUD using `run_db_off_loop`. Contribution requests use the
existing bounded heavy-route pool. The endpoint checks permission before invoking
the provider; account owners and superusers also qualify. Restricted credentials
cannot read attention items.

`GET /api/v1/attention/extension-items` merges authorized contributions, isolates
provider failures and assigns each source a `plugin-name:source-id` namespace
(at most 255 characters). Each provider has a two-second async timeout; failures
and timeouts roll back the session and allow the next source to run. Providers
must cooperate with cancellation and keep synchronous CRUD off the loop.
With the default `include_delivery_reports=false`, no providers means `[]`.
Item fields are `id`, `source`, `title`, `detail`, `href`, `severity` (`action` or
`info`) and timezone-aware `opened_at`. Destinations must be local `/console`
paths. Providers own wording, policy and finding lifecycle.
The core owns presentation only. Action items use the existing warning treatment;
information uses neutral low severity. Both use existing account dismissals, with
SHA-256 digests of the source/item identity and incident/content fingerprint. Both
stay within the dismissal limits even at the maximum item lengths. A changed
incident or displayed content returns after dismissal.

The console requests `GET /api/v1/attention/extension-items?include_delivery_reports=true`
so pending Delivery Report proposals arrive in the same read as plugin items.
Core adds `core:delivery_report` items only for a user who can view flows. Each
item carries a `delivery_report` payload with the report's pending proposals;
the console turns that payload into its Delivery Report attention kind and uses
the report's proposal fingerprint for dismissals. These items can appear even
when no plugin provides attention. The candidate read is bounded to 100 reports
and shared with the standalone `GET /api/v1/delivery-reports/attention` API.
Plugin providers should leave `delivery_report` unset; it is core-owned data.

Optional evidence processors can use the inert `ObservationVerification` model and
`crud_observation_verification` store. A trusted server verifier success binds one
source and subject digest to policy/evidence digests and an expiring time window.
There is no public or collector write endpoint, and core runs no verifier. Optional
processors own interpretation and invalidation. Missing records remain unverified.
Finding changes and verification changes retain evidence in the existing audit
store, subject to the deployment's audit retention policy. Repeated identical
finding evaluations do not append duplicate history.
