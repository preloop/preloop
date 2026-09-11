# Alibaba Cloud Model Studio

Choose **Alibaba Cloud Model Studio (Qwen)** when adding an AI model. Existing
configurations keep the provider identifier `qwen`; no migration is needed.

Set the API URL for the region of your Model Studio API key:

| Region | Compatible-mode base URL |
| --- | --- |
| Beijing, the default when omitted | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Singapore International | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| Singapore workspace | `https://{workspace}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| US | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` |

Keys are regional. Use a Model Studio API key, not an Alibaba RAM AccessKey.
See Alibaba's [API key instructions](https://www.alibabacloud.com/help/en/model-studio/get-api-key).

**Fetch Models** queries a live provider catalog. The classic Singapore URL
uses the documented native catalog, with compatible-mode listing as a fallback;
workspace URLs stay on their configured host. Native page numbers and compatible
cursor pages are bounded to 50 pages and 1,000 model IDs, with an overall timeout.
A live listing describes the provider catalog; account entitlements and model
availability still determine whether a completion succeeds. Failed listing has
an explicit reason and does not substitute guessed model IDs.

The picker covers chat and agent models, including third-party models served by
Model Studio. Selecting DeepSeek, GLM, or Kimi there keeps Alibaba as the upstream.
Image/video generation, dedicated audio, translation, and separate Qwen-VL lines
are outside this picker. Listing a model is not an end-to-end compatibility test
of every capability it advertises.

## Chat and agent controls

Chat Completions supports streaming, tool calls, and provider token usage.
Responses requests use the chat adapter, with `reasoning.effort` translated to
Model Studio's reasoning control. Model-specific opaque reasoning-history
continuity is not guaranteed for every hosted model; validate an agent's tool
round trip when adopting a new model.

For supported models, send `enable_thinking` as a boolean and `thinking_budget`
as a non-negative integer. `reasoning_effort` also accepts the provider's
documented levels. Use effort or budget, not both; individual models can reject
controls they do not support. Arbitrary `extra_body` fields cannot override the
gateway's model, endpoint, or governed messages.

Explicit caching supports up to four `cache_control: {"type": "ephemeral"}`
markers on system/user text content blocks. This path is restricted to text
blocks; mixed image/file content with explicit markers is rejected. The gateway
preserves validated markers through its OpenAI-compatible adapter and records
the cache mode separately from provider usage. Cache hits are never guaranteed.

Legacy AI-driven approval workflows configure their model separately from saved
gateway models. Set `approval_config.provider` to `qwen` and provide
`approval_config.api_endpoint` for a Singapore/workspace key, including when the
model identifier belongs to a third-party family hosted by Alibaba.

## Cost reporting

Token counts come from provider usage when available. Reasoning tokens are a
breakdown of completion tokens and are not charged a second time. Cache-read
and cache-creation counts retain the provider's detailed usage.

Catalog dollar values are **estimates**, not invoices. Preloop uses only verified
model, region, tier and currency combinations. It does not substitute a native
DeepSeek, Z.ai or Moonshot price for an Alibaba-hosted model. Unknown combinations
remain unpriced unless an operator supplies an intentional override.

The supported catalog currently covers specific Singapore International standard
text tariffs, including `qwen3.8-max` and its exact `qwen3.8-max-0902` snapshot.
See [Alibaba's model pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing)
for published rates. Other regions, unverified tiers, and ambiguous cache tariffs
(including Qwen 3.8 Max cache tariffs without confirmed currency) remain unpriced.
Trial credits, negotiated discounts, asynchronous discounts, cache storage fees,
and the final account bill are not inferred from token counts or a model listing.
Use Alibaba's billing console for actual charges and credit balances.
Cost reconciliation shows unpriced requests and tokens; aggregate estimated
spend totals include only known prices and can understate total spend.
