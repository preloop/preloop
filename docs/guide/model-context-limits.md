# Context window and output ceiling

A coding harness that does not know how much context its model accepts uses
its own conservative default. On a model with a large window that shows up as
early compaction: the harness drops what it just read and then reads it again,
which costs tokens and loses detail.

Preloop tells the harness the two numbers when it knows them. Codex receives
`model_context_window` and `model_max_output_tokens` in `config.toml`,
OpenCode receives `limit.context` and `limit.output` in its provider model
entry. Nothing is guessed: an unknown limit is left out and the harness keeps
its own default.

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
