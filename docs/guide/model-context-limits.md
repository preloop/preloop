# Context window and output ceiling

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A coding harness that does not know how much context its model accepts uses
its own conservative default. On a model with a large window that shows up as
early compaction: the harness drops what it just read and then reads it again,
which costs tokens and loses detail.

Preloop supplies known limits using each harness's supported configuration.
Codex receives `model_context_window`. For gateway-enabled runs, its upstream
output allowance is enforced by the model gateway; direct custom-provider runs
rely on Codex/provider defaults. Codex 0.153.4 does not support a
`model_max_output_tokens` configuration key. OpenCode receives `limit.context`
and `limit.output` in its provider model entry. An unknown provider limit is
not inferred from a similar model name.

## Where each number comes from

Per field, in order:

1. **The model row.** `model_parameters` on the model, which an operator owns
   and can edit.
2. **The vendored price catalog.** The snapshot Preloop ships, looked up by
   the model identifier, then by the identifier without its provider prefix,
   then by the `provider/identifier` spelling.

The two fields resolve independently, so a model row that sets only the
window still gets its output ceiling from the catalog.

## Keys read from `model_parameters`

Context window, first one present wins:

- `context_window`
- `max_input_tokens`
- `model_context_window`

Output ceiling, first one present wins:

- `max_output_tokens`
- `model_max_output_tokens`
- `max_tokens`

Several spellings are accepted because operators wrote them before this
feature existed.

## What is ignored

- A context window below 1024 tokens and an output ceiling below 256 tokens.
  A number that small is a typo, or a per-request cap stored in the wrong
  field, and a harness told its window is 500 tokens cannot work.
- Anything that is not a whole number: text, booleans, infinities, nulls.
- A model the catalog does not list, when the model row says nothing either.
  One INFO line records that neither source knew, naming the model.

Ignoring is silent by design in the sense that the run continues normally;
the harness simply keeps its own default for that field.

## Hosted Codex custom-model metadata

The pinned hosted Codex CLI (0.153.4) also receives a local `model_catalog_json`
entry for an unknown custom model. Scalar configuration alone does not register
a model: without metadata, this CLI warns and applies its generic profile,
including a 272,000-token context ceiling that can clamp larger explicit windows.
The generated entry uses the exact selected model identifier and sets both
context fields when a model-row or catalog window is known.

When the provider window is unknown, Preloop preserves this version's generic
272,000-token **CLI fallback**, not a claim about the provider's capacity. Set an
accurate registered `context_window` for a model not in the vendored catalog.
The `PRELOOP_CODEX_MODEL_METADATA` startup line records the CLI version, resolved
CLI window, source, and whether the provider context is known. Output allowance
remains separate; gateway-enabled runs expose its effective settings in gateway
request diagnostics. Direct provider runs have no gateway-enforced ceiling.

The local entry preserves the pinned CLI's generic instructions and tool schemas.
Hosted runs explicitly retain the default disabled `update_plan` tool; the prompt
omits its checklist guidance exactly as the pinned CLI does without a catalog.
The custom catalog contains only the selected model, so the subagent tool no
longer advertises bundled OpenAI model overrides for that custom provider. The catalog accepts the pinned CLI's standard reasoning-effort values
(including role overrides such as `none` and `xhigh`) so Codex can validate subagent/role settings. These are
pass-through inputs, not inferred upstream capabilities, and no default effort
is imposed. Arbitrary provider-specific effort strings are not registered by this
generic catalog. Provider support is still required. It does not borrow another model's instructions or assert new provider
capabilities. Built-in model profiles, including the CLI's supported single
namespace aliases, remain untouched. Removing the metadata warning does not
prove a review will finish within its flow timeout or that a provider supports
every tool or modality in the generic CLI profile.

The profile and schema are version-pinned. A `codex_cli_version` override to an
unvalidated release keeps that CLI's existing metadata behavior and reports the
unsupported profile version in the service log. When upgrading the hosted pin,
validate the catalog with the new CLI and refresh the upstream fallback
instructions and known-model list together. The copied fallback instructions
are from `openai/codex` tag `rust-v0.153.4`, under Apache-2.0.

See the official [Codex model catalog specification](https://learn.chatgpt.com/docs/model-catalogs)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
