# Model price refresh

Preloop separates model discovery, price evidence, and current estimates. The
model-discovery scheduler adds provider model identifiers; it does not refresh
existing prices. The vendored catalog supplies default estimates. Missing models
can trigger a live LiteLLM/OpenRouter lookup, but that path does not periodically
update existing prices. Most providers' model-list endpoints do not return prices.
Alibaba Cloud Model Studio is an exception: native `GET /api/v1/models` includes
USD list tariffs. Preloop seeds Singapore International chat SKUs from the public
pricing page and refreshes that overlay when Fetch Models, Fetch price, or an
unpriced Alibaba usage row looks up the native catalog. The weekly review should
re-check the public pricing page and, when a native catalog dump is attached as
evidence, regenerate `services/data/alibaba_international_prices.json`.

## Reviewed prices without an application deployment

The optional reviewed-feed service runs in each API, dedicated gateway, and worker
process. After the initial code rollout and configuration, it fetches an
operator-controlled HTTPS JSON artifact every six hours. Publishing a new reviewed
artifact updates current estimates without restarting those processes.

Configure the same values for all serving processes:

```shell
MODEL_PRICE_REFRESH_URL=https://example.com/reviewed_model_prices.json
MODEL_PRICE_REFRESH_ALLOWED_MODELS='["example/model"]'
MODEL_PRICE_REFRESH_INTERVAL_SECONDS=21600
```

An empty URL (the default) disables polling. The allowlist contains exact existing
LiteLLM catalog keys, not provider or account IDs. Restrict write access to the
publication branch/bucket: this URL is a pricing trust boundary. Redirects are not
followed. An approved PR can publish the artifact through the organization's
existing branch or static-artifact hosting; no extra application deployment is
needed. This feature does not configure that hosting or activate a live schedule.
An invalid URL or an empty allowlist disables this optional refresh service and
logs a sanitized warning; it does not stop API, gateway, or worker startup. The
warning omits the configured URL and exception details, which may contain secrets.

Each feed must declare USD, a revision, publication and expiry timestamps, and
per-model source URL, verification time, effective date, and either flat input/output rates per token (with optional cache rates) or
a supported native DeepSeek UTC-band policy. Invalid, expired, future,
out-of-scope, unknown-model, or unsupported-policy feeds leave the last good prices
in place and log a refresh failure. Publication timestamps cannot move backwards
within a process. Feed validity is at most 31 days and evidence must be verified
within 14 days of publication. A process restart reloads the vendored baseline
until its first successful poll; cross-process refresh is eventually consistent.

The accepted batch replaces the process price-map reference once, after complete
validation. Account overrides, provider-reported costs, and dedicated provider
policies retain their existing precedence. The job never writes usage records or
re-prices historical costs. Retained last-good rates remain estimates if a feed
expires or becomes unreachable; operators should monitor the refresh failure log.
Rollback uses a newly reviewed revision with a later publication timestamp.

Runtime replacement currently depends on LiteLLM's private
`_invalidate_model_cost_lowercase_map` helper to clear cached model information.
LiteLLM upgrades must pass the warmed-price refresh regression tests. If that
helper is missing, not callable, or raises, refresh logs a compatibility warning
and retains the previous price map and accepted revision. It does not fall back
to changing the map without invalidating caches. A callable helper is checked
before publication; if it fails after publication, the map is restored and the
known model-info LRU caches are cleared. Polling continues so a compatibility
repair can recover without losing the last accepted feed.

The supported native DeepSeek policy uses UTC peak hours 01:00-04:00 and
06:00-10:00 Monday-Friday, separate peak/off-peak input/output/cache prices,
and dated effective revisions. Rates within this known structure can refresh
without deployment, including future-dated rates activated at request time.
Unspecified public-holiday exemptions remain an explicit estimate limitation.
Different time bands, holiday definitions, context tiers, or region rules require
an adapter and boundary tests in a code rollout; they cannot be flattened into
one feed price. Native DeepSeek keys reject flat-price feeds.

Dynamic catalog entries store `preloop_price_policy` and
`preloop_price_policy_history` (a list of `{policy, provenance}` records). The
builder exports these as `price_policy` and `price_policy_history` under a manifest
model with `policy: deepseek_utc_bands`. A policy contains `kind`, `effective_from`,
`peak` and `off_peak` rate objects (`input_per_1m`, `output_per_1m`,
`cached_input_per_1m`), `peak_hours_utc: [[1,4],[6,10]]`,
`peak_weekdays: [0,1,2,3,4]`, and `public_holidays: unspecified`.
Use an existing native key such as `deepseek/deepseek-v4-flash` in the allowlist.
The dedicated estimator resolves the current Flash alias to that native policy.

For example, this is the native policy shape in a catalog entry (illustrative
dates and rates; verify official evidence before publication):

```json
{
  "litellm_provider": "deepseek",
  "preloop_price_policy": {
    "kind": "deepseek_utc_bands",
    "effective_from": "2026-09-12T00:00:00Z",
    "peak": {"input_per_1m": 0.3, "output_per_1m": 1.2, "cached_input_per_1m": 0.006},
    "off_peak": {"input_per_1m": 0.15, "output_per_1m": 0.6, "cached_input_per_1m": 0.003},
    "peak_hours_utc": [[1, 4], [6, 10]],
    "peak_weekdays": [0, 1, 2, 3, 4],
    "public_holidays": "unspecified"
  },
  "preloop_price_policy_history": []
}
```

Its manifest model entry uses the same `effective_from`, `source_url` pointing
to the official provider publication, fresh `verified_at`, and
`policy: "deepseek_utc_bands"`. The builder copies the policy and history from
the catalog. On the next tariff change, append the previous `preloop_price_policy`
and its `preloop_price_provenance` as a `{ "policy": ..., "provenance": ... }`
history record before replacing the current policy. Feed provenance includes
`revision`, `published_at`, `expires_at`, `source_url`, `verified_at`, and
`effective_from`; preserve it unchanged for that historical record.

Each new publication must carry prior reviewed policies and their provenance,
so a restarted process can still price requests that began under an older tariff.
Running processes also retain previous reviewed revisions (maximum 100 per key)
and reject changing the rates at an already-reviewed effective instant. Read-back
exposes selected tariff provenance; historical usage records remain unchanged.

## Build and review the publication artifact

Keep provider evidence in `docs/pricing/reviews/<date>.md`. A manifest selects only
the catalog keys actually reviewed; it does not duplicate their rate numbers:

```json
{
  "schema_version": 1,
  "currency": "USD",
  "revision": "example-review-1",
  "published_at": "2026-09-12T12:00:00Z",
  "expires_at": "2026-09-19T12:00:00Z",
  "models": {
    "example/model": {
      "policy": "flat_per_token",
      "source_url": "https://example.com/pricing",
      "verified_at": "2026-09-12T11:00:00Z",
      "effective_from": "2026-09-01T00:00:00Z"
    }
  }
}
```

The example is illustrative, not a production price feed. After reviewing the
catalog and recording actual provider evidence, generate the artifact:

```shell
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend python scripts/build_reviewed_model_prices.py \
  --catalog backend/preloop/services/data/model_prices.json \
  --manifest docs/pricing/reviewed-price-manifest.json \
  --output backend/preloop/services/data/reviewed_model_prices.json
```

The builder validates provenance and copies rates and supported policies directly
from the reviewed catalog. Include catalog changes, manifest, generated artifact, source excerpts,
and relevant tests in the PR. Renew evidence and feed expiry even when prices have
not changed; do not relabel failed retrievals as fresh verification.

The enterprise factory template `factory/loops/model-price-review.yaml` prepares a
Monday 06:00 UTC audit and isolated PR publication. It is disabled and has explicit
repository/model binding placeholders. Configure those bindings, inspect its
verification profile, and review a manual run before enabling the schedule. It
checks official provider pricing, uses public APIs where they expose prices,
reports unsupported policies, and generates the artifact above. PR review/merge
controls publishing; the agent has no permission to merge or deploy.

The template's isolated pricing gate runs unit coverage without an account
database. It explicitly excludes the two native gateway/repricing integration
test functions requiring `db_session` and `test_user`; repository integration CI
must still run those with its configured test database. This does not disable
them in pytest or remove their coverage requirement.
