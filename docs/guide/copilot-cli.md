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

## Run Copilot CLI from flows (private runner host profile)

A flow can run `copilot` directly on a private runner, using the GitHub
Copilot login and seat of the OS user that runs `preloop runner fg`. This
is a separate path from `preloop copilot`: model traffic goes to GitHub
under that seat, not through the Preloop gateway, so it is billed as the
seat's premium requests and is not gateway metered. The execution page
shows "Not gateway metered" for these runs.

1. Install Copilot CLI and sign in once as the runner user
   (`copilot`, then `/login`, or set `COPILOT_GITHUB_TOKEN` in the runner's
   environment).
2. Add a profile to `~/.preloop/runner-host-profiles.json`:

   ```json
   {
     "profiles": [
       {
         "name": "copilot-review",
         "executable": "copilot",
         "workspace_root": "/home/example/src",
         "timeout_seconds": 1800,
         "model_map": {"team-default": "claude-sonnet-4.6"},
         "allow_tools": ["shell(git:*)"],
         "deny_tools": ["shell(git push)"]
       }
     ]
   }
   ```

3. Restart `preloop runner fg`. On the flow, choose **Copilot CLI (private
   runner host profile)**, pick the private runner pool, and enter the
   profile name. The optional **Copilot model** is an alias from the
   profile's `model_map`; blank uses the Copilot default.

The runner starts
`copilot --prompt=<prompt> -s --no-ask-user --output-format=json` in a fresh
per-run directory, adds `--model=<mapped model>` when a model is requested,
and adds one `--allow-tool` / `--deny-tool` per profile rule. Success
requires exit zero and exactly one Copilot `result` event with `exitCode`
0. The result records the Copilot session id, the model Copilot reported
and the premium request count.

Tool permissions are local to the profile:

- `allow_tools` and `deny_tools` take Copilot permission rules such as
  `write`, `shell(git:*)` or `github(get_file_contents)`. With no rules,
  any tool that needs permission, such as editing files or running shell
  commands, is denied because the run cannot ask.
- `allow_all_tools: true` passes `--allow-all-tools`. The runner refuses it
  unless the Preloop approval hook is installed
  (`preloop agents onboard "Copilot CLI" --approvals`), so every tool call
  still goes through Preloop policy.
- `force_writes`, `--allow-all`, `--yolo`, `--model`, `--agent`, prompt,
  resume and MCP flags cannot be set in profile `argv`.

The Copilot environment is built from an allowlist: a per-OS system
baseline, `COPILOT_*`, `GH_*` and `GITHUB_TOKEN` (so the seat login is
preserved), proxy and TLS variables, and any names the profile lists in
`pass_env`. `COPILOT_PROVIDER_*`, `COPILOT_OFFLINE` and `COPILOT_ALLOW_ALL`
are removed on top of that, so a host profile always uses the seat, never a
BYOK endpoint, and the operator's unrelated environment never reaches the
run. The runner also installs the Preloop usage hooks in
`~/.copilot/hooks/preloop.json` (or `$COPILOT_HOME/hooks`) before each run,
leaving other hook files untouched; hook entries use the `bash` command form
on POSIX and `powershell` on Windows. An unchanged hooks file is not
rewritten, and a changed one is replaced atomically, so concurrent runs
never read a partial file.

Host profiles run on Linux, macOS and Windows runners; see the
[Windows quickstart](runners/quickstart-windows.md) for npm `.cmd` shim
handling and command-line limits.

Named errors:

| Error | Meaning |
| ----- | ------- |
| `copilot_not_installed` | `copilot` is not on the runner's `PATH`. |
| `copilot_not_logged_in` | The runner user has no Copilot login. |
| `copilot_model_unavailable` | The seat does not offer the mapped model. The error lists the profile's `model_map` aliases; Copilot CLI has no non-interactive way to list the seat's models. |
| `copilot_approval_hook_missing` | `allow_all_tools` is set but the approval hook is not installed. |
| `copilot_hooks_unavailable` | Preloop could not install or read its own hooks file under `~/.copilot/hooks` (or `$COPILOT_HOME/hooks`). The run fails before Copilot starts. |

Like Cursor host profiles, this path does not clone repositories, open
pull requests, run custom commands or resume sessions, and flow MCP tool
settings do not apply: Copilot uses the runner user's own
`~/.copilot/mcp-config.json`. See
[host execution profiles](runners/quickstart-linux.md#host-execution-profiles-opt-in-private-only)
for the shared rules.

Copilot plan terms govern how a seat may be used. A developer running
flows on their own machine with their own seat is ordinary use. Check
your organization's Copilot Business or Enterprise terms before sharing
one seat across automated flows for several people.

## Related

- [Copilot coverage](copilot.md): what each surface governs and meters
- [`preloop cursor`](cursor-cli.md): Cursor Agent launcher pattern
