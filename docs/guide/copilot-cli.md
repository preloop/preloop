# `preloop copilot`: GitHub Copilot CLI through the Preloop gateway

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

`preloop copilot` starts the GitHub Copilot CLI (`copilot`) with BYOK
environment variables pointed at the Preloop model gateway. Interactive
mode is a TTY passthrough: stdin, stdout, and stderr stay attached, so the
session behaves like a direct `copilot` launch.

Model traffic goes through Preloop. GitHub-hosted models are not used for
that path. Missing `copilot` on `PATH`, a missing Preloop credential, or a
missing model alias exits with a named error and does **not** start
Copilot, because launching without the BYOK variables would fall through to
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
Preloop flags (`--token`, `--url`, `-v`) belong **before** the
`copilot` subcommand. Preloop reads them there and does not forward them to
`copilot`, so a `--token` value never appears in the Copilot process
arguments. Anything after `copilot` (other than `--model` and `--provider`)
is passed through unchanged, even if it looks like a Preloop flag.

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
shows "Not gateway metered" for these runs, and the run's
premium-request count is not a token ledger or a price. Dollars for the
seat come from the [premium-request import](copilot-usage-import.md),
per user and per day, so this path has no ticket-level cost. What it
governs, records and does not do is collected in
[Copilot coverage](copilot.md).

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
         "deny_tools": ["shell(git push)"],
         "allow_checkout": true
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

### Review and implementation flows

A host profile can run a PR Reviewer or an implementation flow end to end:

- **Checkout.** When the flow enables git clone, the control plane sends
  the runner a checkout plan with each lease: repository URL, branch, the
  pinned commit (for a pull request, its head commit plus the refs that
  reach it, such as `pull/5/head`), the relative path and a read
  credential from the flow's tracker. The runner clones only when the
  profile sets `"allow_checkout": true`; otherwise the run fails with
  `host_checkout_not_allowed` before anything is written. Repositories land
  in the per-run directory (`workspace` for a trigger project,
  `workspace-1`, `workspace-2` or `workspace/<path>` for configured
  repositories), and the prompt starts with a short note listing them. The
  credential is passed to `git` as a request header scoped to that
  repository URL for the clone and fetch only. It is never written to the
  remote URL, the git config or `pending_job`, and it is only sent over
  https (plain http is accepted only for a loopback tracker). Clone
  `setup_commands` are
  refused.
- **Publication (one Bitbucket Cloud repository, opt-in).** A flow whose
  `git_clone_config` sets `create_pull_request` runs on a Copilot profile
  only when that profile sets both `"allow_checkout": true` and
  `"allow_publish": true`. The runner then advertises `host_publication`
  for the profile. After Copilot exits successfully the runner stages and
  commits any changes (repository hooks are disabled), and pushes the
  commit to the managed branch `preloop/issue-<KEY>-<execution>` with the
  same URL-scoped header credential it cloned with. It never force-pushes
  and pushes to the planned URL, not the `origin` remote. The control
  plane then opens the pull request through the bound Bitbucket tracker
  and binds it to the execution; the agent never supplies a pull request
  URL. Saving such a flow, or starting it, fails with the original
  "cannot publish pull requests" error when no runner in the pool
  advertises `host_publication` (an older CLI or a profile without
  `allow_publish`), and those runners never take the lease. Isolated
  publication, more than one repository and Cursor profiles stay refused.
  Feedback continuation (resuming the Copilot session from a review
  comment) is not part of this release step.
- **MCP tools.** When the flow allows MCP tools or servers, the runner adds
  a `preloop-flow` MCP server to this run only
  (`--additional-mcp-config`, written to a `0600` file in the run
  directory and removed afterwards). Its token is scoped to the execution,
  lists only the flow's tools and is revoked when the run completes.
  Unless the profile sets `allow_all_tools`, the runner also passes
  `--allow-tool=preloop-flow`, so the flow's tools run without a prompt;
  profile `deny_tools` rules still apply. The PR Reviewer reads the diff
  and posts its review through these tools. The runner user's own
  `~/.copilot/mcp-config.json` servers stay available, and are governed
  only where they point at Preloop's `/mcp/v1`.
- **Sessions and usage.** The runner exports `PRELOOP_FLOW_EXECUTION_ID`
  and `PRELOOP_FLOW_ID` to Copilot. The Preloop usage hook forwards the
  execution id, so the Copilot session and its hook events are linked to
  the execution. Events a hook pushed without it are linked when the run
  completes, by Copilot session id. The premium requests from the `result`
  event are stored as one subscription row. The execution page shows
  "N premium requests, not metered by the gateway" and lists the linked
  Copilot sessions. No gateway usage row is written for the run. Those
  sessions, hook events and that count are execution bookkeeping; they
  are not token, cost or replay parity with a gateway path.

Tool permissions are local to the profile:

- `allow_tools` and `deny_tools` take Copilot permission rules such as
  `write`, `shell(git:*)` or `github(get_file_contents)`. With no rules,
  any tool that needs permission, such as editing files or running shell
  commands, is denied because the run cannot ask. These are Copilot
  native-tool rules and are separate from MCP governance.
- `allow_all_tools: true` passes `--allow-all-tools`. The runner refuses it
  unless the Preloop approval hook is installed
  (`preloop agents onboard "Copilot CLI" --approvals`), so every tool call
  still goes through Preloop policy.
- `force_writes`, `--allow-all`, `--yolo`, `--model`, `--agent`, prompt,
  resume and MCP flags cannot be set in profile `argv`. That includes
  `--additional-mcp-config`: the operator cannot supply or replace the
  flow's MCP server, and the runner passes the flag itself for every job
  that has one.

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
| `host_checkout_not_allowed` | The flow clones repositories but the profile does not set `allow_checkout`. |
| `host_checkout_failed` | `git` could not clone or check out the planned commit. The error carries git's last line; the credential is never logged. |
| `host_publication_not_allowed` | The flow opens a pull request but the profile does not set `allow_publish` (and `allow_checkout`), or is not a Copilot profile. |
| `publication_failed: push_conflict` | The managed branch already exists on the remote with other commits. Nothing was force-pushed; the commit stays in the run directory. |
| `publication_failed: credential_rejected` | The repository refused the tracker credential for the push. Reconnect the code-host tracker with write access and run again. |
| `publication_failed: repository_config_unsafe` | The checkout's `.git/config` rewrites remote URLs (`url.*`), so the runner refused to push. |
| `publication_missing` | A publishing run ended without a pushed branch, for example because Copilot made no changes. |
| `git_not_installed` | The flow clones repositories and `git` is not on the runner's `PATH`. |
| `copilot_hooks_unavailable` | Preloop could not install or read its own hooks file under `~/.copilot/hooks` (or `$COPILOT_HOME/hooks`). The run fails before Copilot starts. |

This path does not run custom commands or clone setup commands, and
does not resume sessions. Pull requests are opened only through the
opt-in managed publication above (one Bitbucket Cloud repository, legacy
mode). Other requests are refused before the run with a message naming
the missing capability; they are not silently dropped. See
[host execution profiles](runners/quickstart-linux.md#host-execution-profiles-opt-in-private-only)
for the shared rules, and
[#1069](https://github.com/preloop/preloop/issues/1069) for the open work
on Bitbucket publication and feedback continuation, which this page does
not describe as shipped.

Copilot plan terms govern how a seat may be used. A developer running
flows on their own machine with their own seat is ordinary use. Check
your organization's Copilot Business or Enterprise terms before sharing
one seat across automated flows for several people.

## Related

- [Copilot coverage](copilot.md): what each surface governs and meters
- [`preloop cursor`](cursor-cli.md): Cursor Agent launcher pattern
