# OpenRouter

Use OpenRouter to reach a model through a choice of hosting providers. A model's
publisher and the host serving a request can be different organizations.

## Configure provider routing

In **Models**, add or edit an OpenRouter model and open **Provider routing**:

- **Only these providers** limits eligible hosts to the provider slugs you enter.
- **Never these providers** excludes hosts.
- **Deny data collection** sends `data_collection: "deny"`.
- **Zero data retention only** sends `zdr: true`.
- **Allow fallbacks** lets OpenRouter try another eligible host when needed.
  Fallbacks stay within the allowlist, exclusions and retention restrictions.

Use slugs from the model's provider list on OpenRouter. A base provider slug can
match its endpoint variants; use the full endpoint slug to select a particular
variant. Restrictions can leave no eligible host, in which case the request fails.
`order` alone is a preference, not an allowlist: use `only` to restrict hosts.

**OpenRouter account privacy settings and guardrails also apply.** In particular,
OpenRouter currently allows Safety Retention endpoints with `zdr: true` unless
your OpenRouter privacy settings or guardrail select **ZDR only**. Configure that
setting if no prompt retention is acceptable. Data collection and zero retention
are separate controls. Provider selection does not itself guarantee a geographic
region; consult OpenRouter's in-region routing documentation for regional endpoints.

See [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[zero data retention](https://openrouter.ai/docs/guides/features/zdr), and
[in-region routing](https://openrouter.ai/docs/guides/privacy/in-region-routing).

## API and advanced preferences

Save the object at `model_parameters.provider` using the model create or update API:

```json
{
  "model_parameters": {
    "provider": {
      "only": ["azure"],
      "ignore": ["openai"],
      "allow_fallbacks": false,
      "data_collection": "deny",
      "zdr": true,
      "require_parameters": true
    }
  }
}
```

Updates replace `model_parameters`; preserve any other parameters you want to keep.
The console and CLI preserve unrelated parameters when editing routing.

The supported routing fields are `order`, `only`, `ignore`, `allow_fallbacks`,
`require_parameters`, `data_collection`, `zdr`, `enforce_distillable_text`,
`quantizations`, `sort`, `preferred_min_throughput`, `preferred_max_latency`, and
`max_price`. Sorting accepts a strategy or an object with `by` and `partition`.
Performance preferences accept numbers or `p50`, `p75`, `p90`, and `p99` cutoffs;
they are preferences, not hard guarantees. Price limits support `prompt`,
`completion`, `request`, `image`, and `audio`, with nonnegative numeric or decimal
string values. Preloop rejects unknown fields, including nested misspellings, and
invalid types on save. Arbitrary provider-specific `options` and
`model_parameters.extra_body.provider` are not supported; use the routing object
above. This keeps routing policy separate from provider-specific request content.

The saved object wins as a whole over any client `provider` or
`extra_body.provider`. A client cannot remove or weaken saved restrictions.
Remove `model_parameters.provider` to return routing control to client requests
and OpenRouter defaults. An empty saved object still overrides client preferences.

Preloop sends the policy to OpenRouter on chat completions and Responses-to-chat
translation, including streaming and continued sessions. It also applies to embeddings and
OpenRouter auxiliary calls. OpenAI-compatible models whose endpoint is OpenRouter
receive the same treatment. These preferences are never forwarded to other
providers. Ordinary sampling parameters retain their existing precedence rules.

## CLI

```sh
preloop models routing MODEL_ID
preloop models routing MODEL_ID --only azure --ignore openai \
  --deny-data-collection --zdr --allow-fallbacks=false
preloop models routing MODEL_ID --clear
```

Flags you omit keep their saved values. Boolean flags accept `=false`; comma-separated
provider lists also accept endpoint slugs. Use `--only=''` or `--ignore=''` to clear
a list. `--clear` removes the whole saved routing object.

## Inspect what happened

Expand a model request in its session or execution to see **Effective upstream
request** beside the captured client request. For every inference provider, it
records the upstream model string, output format (`none`, `json_object`, or
`json_schema` with its strict flag), whether a schema moved into instructions,
reasoning controls, output token cap when set, tool count, and routing preferences
when applied. This summary contains no message text, schemas, tool definitions,
credentials, or headers. It remains available when content capture is disabled.
The same safe controls are available in restricted execution timing projections.

For OpenRouter, **Served by** records the host reported in the response's `provider`
field. The **Request ID** is the OpenRouter generation ID, including for translated
Responses requests whose client-facing ID is different. If OpenRouter omits the
host, it remains unknown; Preloop never guesses from `order` or `only`. Existing
rows are not backfilled. OpenRouter's generation metadata API exposes the equivalent
`provider_name` for manual investigation using a generation ID.

On **Gateway traffic**, **Export usage** downloads the selected date range as CSV.
It includes `upstream_provider`, `upstream_request_id`, and `upstream_parameters`
alongside usage totals. The authenticated API is
`GET /api/v1/account/gateway-usage/export?start_date=...&end_date=...`; account scope,
`view_cost` permission, and reporting history limits apply. Exports over 10,000
requests ask you to select a shorter range instead of silently truncating.
