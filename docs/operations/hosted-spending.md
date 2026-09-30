# Hosted spending rollout

Editions: Cloud.

Hosted spending applies to operator-paid built-in models. BYOK traffic keeps its
existing gateway, approval, firewall and budget behavior. Included balances are
separate from retained usage rows: deleting analytics never restores credit.
Extra spending is disabled. There is no consented spending-cap or provider-charge
recovery implementation to enable it yet.

The Free grant is a single lifetime allowance. Paid allowances use UTC calendar
months, including subscriptions billed annually. Reservations reduce available
balance before each physical provider attempt. An ambiguous failed attempt keeps
its reservation even when the gateway retries. Streaming cancellation without
final complete usage also keeps the reservation. Verified settlement releases
unused headroom; outstanding amounts remain visible in account status. Never
expire these holds solely because a request is old or no usage row survived.

## Supported model contract

A system model (`account_id = NULL`) may opt into the text gateway adapter using
operator-controlled `meta_data.hosted_metering`. Required fields:

| Field | Required evidence |
| --- | --- |
| `text_context_bound_verified: true` | The deployed adapter enforces the stated total input-context and output bounds, including framing/tool tokens, with a fixed USD tariff. |
| `fixed_model_identifier` | Exact deployed model identifier; automatic model routers are unsupported. |
| `fixed_provider` | Exact provider name, matching the system model. |
| `fixed_api_base` | Exact reviewed upstream base, matching the deployed model and final dispatch route. |
| `max_input_tokens` | Positive integer upper bound on all chargeable input tokens. |
| `max_output_tokens` | Positive integer enforced by the adapter for output including reasoning tokens. |
| `input_usd_per_million` | Verified nonnegative finite USD rate for all supported input tokens. |
| `output_usd_per_million` | Verified nonnegative finite USD rate for all supported output tokens. |
| `request_usd` | Verified nonnegative finite fixed USD charge per physical attempt, explicitly zero when none applies. |

These values must come from the actual operator/provider contract. No default or
example dollar rate is a deployment recommendation. Validate the model's context
limit, output limit and rates against its deployed version and prove the adapter
honors them using an isolated controlled upstream before enabling the flag.
Unknown request options and prices refuse hosted dispatch. Only allowlisted
text options, function/custom tools, and the default service tier are supported.
All `extra_body` extensions, provider-billed built-in tools (including search),
audio/media, cache-write markers (including in tools), custom routing headers,
and provider/fallback routing overrides are refused. Do not attest this contract for an adapter with hidden
cache-write charges, dynamic routing, additional paid operations, or retries that
cannot be disabled. LiteLLM retries are set to zero and parameter dropping is
disabled so the output ceiling cannot disappear; gateway retries each reserve
again. Final transformed request kwargs are checked before dispatch. The current
reservation uses the full verified input-context bound, so conservative headroom
can exceed the likely cost of a short prompt.

Chat and Messages through LiteLLM are metered. Responses requests already
configured for chat transcoding are metered through that existing adapter.
Native Responses reserves inside each physical HTTP attempt and observes the
existing JSON response or SSE terminal frames. It preserves native instructions,
reasoning, tool state, and stream frames; only the output ceiling is enforced in
the native body. Native OAuth/Codex and Anthropic OAuth hosted passthrough remain
refused until those adapters have durable settlement support. The selected
protocol is never changed merely to reach the meter.

Settlement requires independently verified input and output counters in the
terminal phase. Intermediate usage followed by EOF is insufficient, and a
terminal snapshot missing output cannot inherit output from intermediate frames.
Explicit split counters may merge within the terminal phase. Missing or invalid
terminal counters leave the reservation held for recovery.
This does not restrict customer-owned keys.

## Transactions and workers

HTTP replay/optimization/interaction-summary entry points explicitly own their
sessions. The optimization job executor and continuous-optimization worker grant
ownership only when they open the session themselves. Internal shared-session
callers default to no ownership and cannot commit unrelated writes; hosted calls
from such callers are refused. Owned requests finish their preparation, then use
the same session sequentially for a short reservation transaction, a provider
wait without a checked-out database connection, and settlement. A second engine
checkout while the caller holds a connection is not used.

Workers must discover the billing plugin's `hosted_spend` and
`analysis_model_authorizer` services even without ASGI startup. API and dedicated
gateway roles must install the same request policies. Verify the actual process
configuration, including proprietary-plugin loading, before activation.

## Activation and rollback prerequisites

1. Apply additive migrations through `20260914_pricing_merge` before rolling
   application images. This joins the existing control/repricing and hosted
   spending chains without rewriting either history. Keep the history
   migration's old-purger replacement requirements.
2. Deploy compatible API, gateway and worker binaries together with flags off.
   Confirm every model-serving process discovers the services.
3. Configure and verify each real hosted model's tariff/adapter contract. No
   production hosted-model configuration was established by the implementation
   tests; their rates and providers are isolated test fixtures.
4. Reconcile existing accounts against reliable lifetime/current-month billing
   evidence. Missing wallets are unknown, never fresh credit. A proven monthly
   baseline may coexist with an unknown lifetime balance; it cannot grant Free
   lifetime credit. New accounts created with metering enabled are initialized
   atomically with account creation. There is no public balance-reset endpoint.
5. Exercise admitted/denied, retry, stream cancel, annual subscription, worker,
   BYOK and recovery-held balance cases on the fully assembled deployment. Keep
   pricing activation disabled until these checks and subscriber reconciliation
   pass. PAYG remains disabled.

After activation, rolling back must preserve metering and durable tables. Turning
metering off permits unrecorded hosted calls, invalidating trusted coverage; do
not re-enable it against an old baseline without reconciling that gap. Never
replace the durable ledger with a sum over retained analytics. Recovery supplies
verified actual cost through the account-scoped idempotent settlement CRUD; a
missing response is not proof of zero cost.
