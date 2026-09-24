# `preloop copilot`: GitHub Copilot CLI through the Preloop gateway

`preloop copilot` starts the GitHub Copilot CLI (`copilot`) with BYOK
environment variables pointed at the Preloop model gateway. Interactive
mode is a TTY passthrough: stdin, stdout, and stderr stay attached, so the
session behaves like a direct `copilot` launch.

Model traffic goes through Preloop. GitHub-hosted models are not used for
that path. Missing `copilot` on `PATH`, a missing Preloop credential, or a
missing model alias exits with a named error and does **not** start
Copilot — launching without the BYOK variables would fall through to
GitHub-hosted models.

MCP onboarding for Copilot CLI (`~/.copilot/mcp-config.json`) is separate.
The cloud coding agent on GitHub.com is a different surface and is not
started by this command.

## Install Copilot CLI

```bash
npm install -g @github/copilot
```

Confirm it is on your `PATH`:

```bash
copilot --version
```

## Usage

```bash
preloop login --token <token>
preloop copilot --model openai/gpt-5
preloop copilot --model anthropic/claude-sonnet-4-5 --provider anthropic
preloop --url https://preloop.example.com --token "$PRELOOP_TOKEN" \
  copilot --model openai/gpt-5
```

Arguments after Preloop's own flags are passed through to `copilot`. Global
Preloop flags (`--token`, `--url`) belong **before** the `copilot`
subcommand, same as `preloop cursor`.

### `--model`

Gateway model alias to set as `COPILOT_MODEL`. Required when no enrolled
Copilot CLI managed agent has recorded a `latest_model_alias` (for
example before `preloop agents onboard "Copilot CLI"` finishes, or when
onboarding has not pinned a model). When an enrolled alias exists, it is
used unless `--model` overrides it.

### `--provider`

Forces `COPILOT_PROVIDER_TYPE` to `openai` or `anthropic`. When omitted,
an alias whose normalized form starts with `anthropic/` (after stripping
an optional `preloop/` prefix) selects Anthropic; everything else
defaults to OpenAI. The launcher does not infer the family from product
names alone.

## Environment contract

| Variable | OpenAI-family | Anthropic-family |
| -------- | ------------- | ---------------- |
| `COPILOT_PROVIDER_TYPE` | `openai` | `anthropic` |
| `COPILOT_PROVIDER_BASE_URL` | `{PRELOOP_URL}/openai/v1` | `{PRELOOP_URL}/anthropic` |
| `COPILOT_PROVIDER_API_KEY` | Preloop bearer credential | same |
| `COPILOT_MODEL` | gateway alias | gateway alias |

`COPILOT_PROVIDER_API_KEY` is a Preloop bearer, never a raw upstream
provider key. An explicit `--token` or `PRELOOP_TOKEN` wins. Otherwise
the launcher uses the enrolled Copilot CLI durable credential (the same
key the permission hook uses), then the saved login token.

Auth and API URL follow the rest of the CLI: `--token` / `PRELOOP_TOKEN`
/ config, and `--url` / `PRELOOP_URL` / config / `https://preloop.ai`.

## Related

- [`preloop cursor`](cursor-cli.md) — Cursor Agent launcher pattern
