# Model price refresh

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop separates model discovery, price evidence, and current estimates. The
model-discovery scheduler adds provider model identifiers; it does not refresh
existing prices. The vendored catalog supplies default estimates. Missing models
can trigger a live LiteLLM/OpenRouter lookup, but that path does not periodically
update existing prices. Most providers' model-list endpoints do not return prices.
Alibaba Cloud Model Studio is an exception: native `GET /api/v1/models` includes
USD list tariffs. Preloop seeds Singapore International chat SKUs from the public
pricing page. Fetch Models, Fetch price, and an unpriced usage lookup can fetch
native prices into a process-local cache with a 24-hour freshness limit. The
reviewed feed below distributes verified regional prices to every serving process,
including already-priced models. Model discovery alone does not do this.

Some provider models have a name the catalog cannot know: an Azure OpenAI
deployment name or a Bedrock application inference profile ARN. Set a base
model or a price override for those; see the [Azure OpenAI](providers/azure-openai.md)
and [Amazon Bedrock](providers/bedrock.md) guides.

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

An empty URL (the default) disables polling. The allowlist contains exact existing LiteLLM catalog keys or exact Alibaba keys
`alibaba/<region>/<model_identifier>`, not account IDs. An operator can opt into
new Alibaba SKUs within a supported region using
`alibaba/singapore-international/*` or `alibaba/united-states/*`. These are the
only accepted wildcards and only authorize `alibaba_regional_tokens` policies;
they cannot authorize generic prices, another provider, CNY, or another region. Restrict write access to the
publication branch/bucket: this URL is a pricing trust boundary. Redirects are not
followed. An approved PR can publish the artifact through the organization's
existing branch or static-artifact hosting; no extra application deployment is
needed. This feature does not configure that hosting or activate a live schedule.
An invalid URL or an empty allowlist disables this optional refresh service and
logs a sanitized warning; it does not stop API, gateway, or worker startup. The
warning omits the configured URL and exception details, which may contain secrets.

Each feed must declare USD, a revision, publication and expiry timestamps, and
per-model source URL, verification time, effective date, and either flat input/output rates per token (with optional cache rates) or
a supported native DeepSeek UTC-band policy, or region-scoped Alibaba token tiers. Invalid, expired, future,
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

The public preset `backend/presets/015-weekly-model-price-review.yaml` prepares a
Monday 06:00 UTC audit and isolated PR publication using Preloop's existing model
and repository credentials. It inventories the generic and dedicated regional
catalogs, records unresolved providers and cache policies, and verifies the exact
candidate before publication. Human PR merge controls feed publication. No
additional AI service key or private factory configuration is required.

The template's isolated pricing gate runs unit coverage without an account
database. It excludes six named gateway/repricing integration functions that
require `db_session` and `test_user`; repository integration CI must still run
those with its test database. The Alibaba pricing, native catalog, discovery,
gateway unit tests and publication tooling all run in the isolated gate.


## Alibaba regional reviewed prices

An Alibaba manifest entry has policy `alibaba_regional_tokens` and the usual
`source_url`, `verified_at`, and `effective_from`; its key is
`alibaba/singapore-international/qwen3.8-flash`, for example. The builder copies
rates from the dedicated seed, including each input-length tier and optional
`implicit_read`, `explicit_read`, and `creation` rates in USD per million tokens:

```shell
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend python scripts/build_reviewed_model_prices.py \
  --catalog backend/preloop/services/data/model_prices.json \
  --manifest docs/pricing/reviewed-price-manifest.json \
  --alibaba-catalog singapore-international=backend/preloop/services/data/alibaba_international_prices.json \
  --output backend/preloop/services/data/reviewed_model_prices.json
```

The feed installs these rates atomically into the dedicated estimator's regional
store. It does not insert unscoped Alibaba prices into LiteLLM. Every configured
API, gateway and worker reads the same publication independently; no deployment
is required for new rates or newly reviewed SKUs within an opted-in region.
The approved reviewed publication takes precedence until its expiry, so a native
refresh cannot silently replace reviewed prices. Expired native/reviewed data is not treated as
fresh. Historical requests cannot consume a tariff before its effective date.
Unsupported currencies and unknown cache rates stay unpriced. Time-banded
Singapore International token SKUs use Model Studio night hours
(22:00-08:00 UTC+8, idle) versus daytime (busy); a native row with only one
band stays unpriced. Do not substitute native DeepSeek weekday UTC bands.
A reviewed Alibaba feed must carry `time_bands` for SKUs whose seed is
banded; flattening idle/busy into a single `tiers` list is rejected.
The public Flash cache-hit table points to the console. Native catalog cache
rows are used as list prices when present. A generic
Qwen discount must never replace model-specific console evidence.

To regenerate the Singapore seed from an explicitly supplied native dump, combine
all pages under `output.models` with `output.total`, and attach `_meta` containing
`currency: USD`, `region: singapore`, `service_site: international`, `complete: true`,
`source_url: https://dashscope-intl.aliyuncs.com/api/v1/models`, and the original
UTC `retrieved_at` timestamp. Then run:

```shell
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend python scripts/update_alibaba_prices.py --from-native verified-dump.json
```

The builder rejects incomplete pages, duplicate identifiers, wrong regions,
empty supported catalogs, and evidence older than fourteen days or in the future.
It preserves the original retrieval date. Running the builder does not verify
an old dump again. The scheduled agent does not fetch authenticated catalogs;
public evidence, an explicitly attached verified dump, or a dated operator-confirmed
console quote for the exact regional tariff is required. Old console quotes must
not be marked freshly verified merely because the weekly review runs again.

## Install the recurring review

Obtain the AI model, tracker and project IDs from your own Preloop account. Bind
the repository containing these scripts. This command renders the complete
validated flow locally and does not contact the API:

```shell
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend python scripts/install_model_price_review.py \
  --ai-model-id "$REVIEW_MODEL_ID" --tracker-id "$REVIEW_TRACKER_ID" \
  --project-id "$REVIEW_PROJECT_ID" --repository-url "$REVIEW_REPOSITORY_URL"
```

Set `PRELOOP_API_TOKEN` and supply `--api-url https://your-preloop-host` plus
`--apply` to create a disabled bound flow. Repeat the same command to update it;
the script identifies its managed flow by repository binding, refuses duplicate
or unmanaged collisions, and never triggers a run. Review one manual run in
Preloop and then repeat with `--apply --enable` to arm Monday 06:00 UTC. Omitting
`--enable` when applying sets the managed flow back to disabled.

The review agent needs an execution environment with the repository's development
dependencies and pre-commit installed. Its isolated publication uses the bound
tracker's existing repository credentials; only the control plane publishes the
verified PR. Enable the platform scheduler/worker normally. Model discovery's
`MODEL_CATALOG_SYNC_SCHEDULED_ENABLED` flag is independent of this schedule.

The initial checked-in manifest and feed contain two Singapore entries reviewed
on 2026-09-15: `qwen3.7-flash` and `qwen3.8-flash`. Flash uses the operator-confirmed
Singapore console rates per million tokens: $0.15 input, $0.47 output, $0.016
implicit read, $0.016 explicit read, and $0.20 cache creation. These token tariffs
do not include separate provider tool charges.
See `docs/pricing/reviews/2026-09-15-alibaba.md` for evidence and limits. Effective
dates conservatively start at each evidence confirmation because no earlier
effective date was established. Flash uses its console confirmation at
2026-09-15T16:26:50Z; the public Qwen3.7 review retains its original date. This is not a fresh audit of all 92 seed models.
The initial feed expires 2026-09-29; renew the review before activation if expired.
Host the approved branch's `backend/preloop/services/data/reviewed_model_prices.json`
artifact on trusted HTTPS, configure `MODEL_PRICE_REFRESH_URL` and the same
`MODEL_PRICE_REFRESH_ALLOWED_MODELS` on every serving process, and merge a reviewed
publication. The default poll interval is six hours. For automatic new Singapore
SKUs use `["alibaba/singapore-international/*"]`.
A regional publication should retain all reviewed keys in that configured scope;
removed entries leave the reviewed store. Monitor audit failure/coverage and feed
expiry rather than treating a partially verified provider as complete support.
