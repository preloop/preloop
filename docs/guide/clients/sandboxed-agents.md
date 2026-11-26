# Sandboxed and containerised agents

Editions: OSS, Cloud, Enterprise.

`preloop agents onboard` normally writes the agent's configuration for you. In a container or sandbox that is often impossible: the settings file is bind-mounted read-only from the host, owned by another user, or sits on a read-only root filesystem. This guide covers onboarding and offboarding in that situation.

## Who this is for

- You run Claude Code (or Codex CLI, or Cursor) in a Docker, Podman or Kubernetes container, a devcontainer, or another sandbox.
- The agent's configuration file is mounted read-only, or the agent's user may not write it.
- You want model traffic and tool approvals to go through Preloop anyway, and you want an honest answer about whether it worked.

What is supported today:

| Agent | Read-only configuration |
|-------|-------------------------|
| Claude Code | Supported: settings file, user configuration (`.claude.json`) and the Agent Control file |
| Codex CLI | Supported for the main configuration file (`config.toml`) |
| Cursor | Supported for the main configuration file |
| Other agents | Not supported yet. Onboarding stops before it changes anything in Preloop or on the machine, and says so |

## What onboarding checks

Before it changes anything in Preloop, `preloop agents onboard` tests every file it would write. It does not trust the permission bits. It opens each existing file for append and creates, then removes, a temporary file beside it. Nothing is written to the file itself. This detects:

- read-only bind mounts
- files owned by another uid
- files with an immutable flag
- a read-only root filesystem

When a file is not writable, the output says so, says when it runs inside a container, and names the host path a mounted file probably comes from (read from the mount table):

```text
Running inside a container (Docker).
Preloop cannot write these files from here:
  /home/jane/.claude/settings.json
    Claude Code configuration: read-only file system
    mounted read-only at /home/jane/.claude/settings.json
    on the host this is probably /home/jane/.claude/settings.json (from the mount table)
Onboarding continues in manual mode: Preloop prepares the agent and prints the exact change for you to make.
```

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured: when they are set, onboarding checks and reports the files under those directories.

To use manual mode on a machine where the files are writable, pass `--manual`.

## Worked example: Claude Code in Docker Compose

The setup: Claude Code runs as a non-root user, `~/.claude/settings.json` is bind-mounted read-only from the host, `~/.preloop` is a named volume, and the `preloop` binary is in the image.

```dockerfile title="Dockerfile"
FROM node:22-bookworm-slim
RUN npm install -g @anthropic-ai/claude-code

# The Linux preloop binary, downloaded from the releases page into the build context.
COPY preloop /usr/local/bin/preloop

RUN useradd --create-home jane \
 && install -d -o jane -g jane -m 700 /home/jane/.preloop /home/jane/.claude
USER jane
WORKDIR /workspace
```

```yaml title="compose.yaml"
services:
  agent:
    build: .
    stdin_open: true
    tty: true
    environment:
      PRELOOP_URL: https://preloop.example.com
    volumes:
      - ./workspace:/workspace
      # Read-only: the container cannot change the host's settings.
      - /home/jane/.claude/settings.json:/home/jane/.claude/settings.json:ro
      # Writable and persistent: Preloop state, resume state, hook credential.
      - preloop-state:/home/jane/.preloop

volumes:
  preloop-state:
```

Docker needs the host file to exist before it can mount it. If you have no settings file yet, create one that contains `{}`.

### 1. Run onboarding inside the container

```bash
docker compose up -d
docker compose exec agent preloop login --headless
docker compose exec agent preloop agents onboard "Claude Code"
```

Onboarding creates (or reuses) the agent and its key in Preloop, then prints the change instead of writing it. A shortened example (your block can hold more keys, and the directory name differs):

```text
Manual setup for Claude Code
------------------------------------------------------------------------
The agent and its key now exist in Preloop. Nothing works until the
change below is in place. The key is shown here once: Preloop cannot
show it again, only issue a new one.

1. /home/jane/.claude/settings.json
   Claude Code configuration
   Not writable from here: read-only file system
   Edit it on the host, probably at: /home/jane/.claude/settings.json
   Merge these keys into the file and keep everything else:

{
  "env": {
    "ANTHROPIC_API_KEY": "agt_...",
    "ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic"
  }
}

   Or replace the file with the complete result: /home/jane/.preloop/manual-setup/<agent>/settings.json

Alternative A: environment variables instead of the settings file
export ANTHROPIC_API_KEY='agt_...'
export ANTHROPIC_BASE_URL='https://preloop.example.com/anthropic'
   The same values as a Docker Compose env_file: /home/jane/.preloop/manual-setup/<agent>/preloop.env

Alternative B: load the block per session
claude --settings '/home/jane/.preloop/manual-setup/<agent>/settings.merge.json'

What this setup and each form does not enable:
  - Environment variables route model traffic through Preloop and nothing else: no approval hooks, no session hooks and no managed MCP server entry, because those live in settings files.
  - `claude --settings <file>` applies the whole block, hooks included, but only to sessions started with the flag. A plain `claude` stays unmanaged.

When the change is in place, finish with:
  preloop agents onboard --resume "Claude Code"
Guide: https://docs.preloop.ai/guide/clients/sandboxed-agents/
------------------------------------------------------------------------
Saved (mode 0600, contains the agent key): /home/jane/.preloop/manual-setup/<agent>
Delete that directory once the change is in place.

Waiting for local configuration: Claude Code is NOT onboarded yet.
  Managed agent: <agent id>
  This pending setup expires 2026-11-29T12:00:00Z; after that Preloop removes the key and what this run created.
  Cancel instead: preloop agents onboard --cancel "Claude Code"
```

What to read in that output:

- **Each file** gets its own numbered entry. For a file that exists you get the smallest merge (only the keys to add or change). For a file that does not exist you get the whole file.
- **Keys to remove** are listed on a `Remove these keys if present:` line when the file holds something that must go.
- **The key is shown once.** Preloop stores only a placeholder. If you lose it, cancel and onboard again.
- In a terminal, onboarding then waits: `Press Enter when the change is in place (or type 'later' to stop here and resume another time)`. Without a terminal it stops here with exit status 3.

The same content is saved, mode 0600, in a private directory (mode 0700):

| File | Content |
|------|---------|
| `INSTRUCTIONS.txt` | The text printed above |
| `settings.merge.json` | Only the keys to merge |
| `settings.json` | The complete resulting file, ready to copy over the host file (only when the file already exists) |
| `preloop.env` | The environment-variable form, usable as a Compose `env_file` |
| `resume-token.txt` | The resume token (see [When ~/.preloop is read-only too](#when-preloop-is-read-only-too)) |

The directory is `~/.preloop/manual-setup/<agent>/` when `~/.preloop` is writable. `--manual-output <dir>` chooses another directory.

### 2. Put the block in the host file

Do one of these on the host.

Copy the complete result over the host file:

```bash
docker compose cp agent:/home/jane/.preloop/manual-setup/<agent>/settings.json ./settings.preloop.json
cp /home/jane/.claude/settings.json /home/jane/.claude/settings.json.before-preloop
cat ./settings.preloop.json > /home/jane/.claude/settings.json
rm ./settings.preloop.json
```

Or merge by hand: open `/home/jane/.claude/settings.json` on the host, add the keys from the printed block, keep everything else, and delete any key listed under `Remove these keys if present`.

!!! note "Single-file bind mounts"
    Docker mounts a single file by its inode. A tool that replaces the file (many editors save this way, and so does `mv`) leaves the container looking at the old content until it is restarted. Writing into the existing file, as `cat ... >` does above, avoids that. Mounting the directory instead of the file avoids it too.

### 3. Resume

```bash
docker compose exec agent preloop agents onboard --resume "Claude Code"
```

Running `preloop agents onboard "Claude Code"` again does the same: a pending setup is resumed, and no second key is issued.

Resume reads the effective configuration back and lists exactly what is missing. Nothing is reported as onboarded on trust:

```text
Checking /home/jane/.claude/settings.json
  ✗ env.ANTHROPIC_BASE_URL is missing

Not onboarded yet. Fix the lines above, then run: preloop agents onboard --resume "Claude Code"
```

A JSON syntax error is reported with its line and the likely slip:

```text
Checking /home/jane/.claude/settings.json
  ✗ invalid JSON at line 6, column 3: trailing comma before "}"; remove the comma at the end of line 5
```

### 4. What success looks like

When the change is in place, resume makes one real request through the gateway with that configuration. Only then does it print `Onboarded`:

```text
Checking /home/jane/.claude/settings.json
  ✓ the change is in place
Sending one real request through the gateway with this configuration... ok.
✓ Onboarded Claude Code
  Managed agent: <agent id>
  Gateway validation: passed (one real request, recorded in the console)
  Configuration applied by you: /home/jane/.claude/settings.json
  Delete the saved instructions, they contain the agent key: /home/jane/.preloop/manual-setup/<agent>
  Undo: preloop agents offboard "Claude Code"
```

Delete the saved instructions afterwards. They contain the agent key.

If the real request fails, the agent is reported as `NOT onboarded`, the pending setup is kept, and you can fix the cause and resume again.

!!! note "Files inside the container"
    Files that were writable (for example `~/.claude.json` with the managed MCP server entry, and the Agent Control file) are written by onboarding as usual. In this example they live in the container's own filesystem, so they are lost when the container is recreated. Put them on a volume if you need them to last. When one of them is read-only, it is printed as a further numbered entry. Resume checks those entries and reports them as optional: model routing does not depend on them.

## Alternative: environment variables

If you cannot or do not want to change the settings file, set the variables in the environment the agent starts in. Copy `preloop.env` out of the container, keep it private, and reference it:

```bash
docker compose cp agent:/home/jane/.preloop/manual-setup/<agent>/preloop.env ./preloop.env
chmod 600 ./preloop.env
```

```yaml title="compose.yaml"
services:
  agent:
    env_file:
      - ./preloop.env
```

Or inline, with the key supplied from the host environment rather than written into the file:

```yaml title="compose.yaml"
services:
  agent:
    environment:
      ANTHROPIC_BASE_URL: https://preloop.example.com/anthropic
      ANTHROPIC_API_KEY: ${PRELOOP_AGENT_KEY}
```

Recreate the container so it picks the variables up, then resume with `--from-env`. It checks the environment of the shell it runs in:

```bash
docker compose up -d
docker compose exec agent preloop agents onboard --resume "Claude Code" --from-env
```

Without `--from-env`, resume checks the settings file first. If the file is unchanged but the variables are set in that shell, it says so and uses them.

Trade-offs:

- Environment variables route model traffic through Preloop and nothing else. No approval hooks, no session hooks and no managed MCP server entry, because those live in settings files.
- The key sits in the container's environment and in your Compose configuration. Do not commit `preloop.env`.
- Only processes started with those variables are routed. A `claude` started from a shell without them is unmanaged.

## Alternative: `claude --settings`

Claude Code can load an extra settings file per session:

```bash
claude --settings /home/jane/.preloop/manual-setup/<agent>/settings.merge.json
```

Then tell resume which file to check:

```bash
preloop agents onboard --resume "Claude Code" \
  --settings-file /home/jane/.preloop/manual-setup/<agent>/settings.merge.json
```

This applies the whole block, hooks included, but only to sessions started with the flag. A plain `claude` stays unmanaged. If you use this form, keep the file (do not delete the saved instructions directory, or copy the file somewhere private first).

`--settings-file` and `--from-env` cannot be combined, and both require `--resume`.

## When `~/.preloop` is read-only too

`~/.preloop` holds the CLI login, the backup of your original configuration, the resume state and the approval hook credential. A writable, persistent `~/.preloop` (a named volume) is the recommended setup. When it is not writable:

- **Sign-in:** pass the token and URL through the environment (`PRELOOP_TOKEN`, `PRELOOP_URL`) or the `--token` and `--url` flags.
- **Instructions:** pass `--manual-output <dir>` to save them in a directory you choose (created with mode 0700). In a terminal, onboarding asks for a directory. Otherwise the instructions are printed only, so copy them from the output.
- **Resume:** onboarding prints a resume token and the full command. The token starts with `plr1.`. It identifies the pending setup and is not a secret: the key is not in it.

    ```bash
    preloop agents onboard --resume --resume-token plr1.<token>
    ```

    Re-running `preloop agents onboard "Claude Code"` also finds the pending setup on the server and checks it.

- **No backup:** no local backup of the original configuration is kept, so `preloop agents offboard` prints the removal instead of restoring a backup.
- **No approval hooks:** see [Approval hooks in a container](#approval-hooks-in-a-container).

## How the Claude login reaches the container

This section applies when Claude Code uses a Claude subscription login. Onboarding imports that login into Preloop, and the gateway uses it for the agent's model calls. An Anthropic API key (`ANTHROPIC_API_KEY`) is read first when one is set, and then there is no login to carry.

| Way | Supported | Notes |
|-----|-----------|-------|
| Sign in inside the container (`claude auth login --claudeai`), which writes `~/.claude/.credentials.json` (or the same file under `CLAUDE_CONFIG_DIR`) | Yes | The container holds its own login. Keep the file on a volume that only this container uses |
| `CLAUDE_CODE_OAUTH_TOKEN` holding a long-lived token from `claude setup-token` | Yes | Preferred over a stored login when both are present, exactly as Claude Code itself prefers it. Preloop never rotates it, and offboarding has nothing to write back |
| The macOS Keychain, when the CLI runs on a Mac host | Yes | Not reachable from inside a Linux container |
| One credentials file shared between the host and the container (mounted or copied) | No | A subscription refresh token works once. The second holder to refresh it ends the first one's login. This is the likely cause of a login that is "rejected on first use" |
| Bedrock or Vertex credentials in the container | Unchanged | This work does not change how they are handled |
| `apiKeyHelper` scripts | No | Not read |

For a container, the simplest reliable choice is a token from `claude setup-token` passed as `CLAUDE_CODE_OAUTH_TOKEN`:

```yaml title="compose.yaml"
services:
  agent:
    environment:
      CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN}
```

Preloop does not report a login as working without using it. When the real request is rejected, the output names the store the login was read from (the credentials file path, the macOS Keychain item, or the `CLAUDE_CODE_OAUTH_TOKEN` environment variable), says that it looks stale, and gives the command that imports the live one:

```text
✗ Claude Code is NOT onboarded: the real request failed: ...
  Anthropic rejected the Claude subscription login Preloop imported.
  It was read from the credentials file /home/jane/.claude/.credentials.json.
  That copy looks stale: a subscription refresh token works once, so a login copied or mounted from another machine stops working as soon as the original is refreshed there.
  Import the live one: sign in where this agent runs (`claude auth login --claudeai`), then run: preloop agents reconnect "Claude Code" --from-local
```

After `preloop agents reconnect "Claude Code" --from-local`, run the resume command again.

If you skip the real request (`--skip-live-validate`) on an ordinary, writable onboarding with a subscription login, the result does not read `Onboarded`. It reads `Configured Claude Code; the subscription login was NOT verified`, with the command to verify it: `preloop agents validate "Claude Code" --live`.

A manual resume run with `--skip-live-validate` checks the file but makes no request, so it does not print `Onboarded` either. It prints `Configured Claude Code; the change is in place but the route and any subscription login behind it were NOT verified`, with the same `validate --live` command.

See also [subscription recovery](subscription-recovery.md).

## Approval hooks in a container

`--approvals` installs hooks that send Claude Code's native tool calls to Preloop for a decision. In a container:

- **The hooks block lives in `settings.json`.** When that file is read-only and you onboard with `--approvals`, the hooks are part of the printed merge. Apply them with the rest of the block.
- **The hook runs the `preloop` binary inside the container.** The hook command is `<path to preloop> agents permission-hook --source claude_code`, written with the absolute path of the binary that ran onboarding. That binary must exist at the same path in every container that uses the settings file. Install it in the image, as in the example above. A host that shares the same settings file needs `preloop` at that path too, or its hooks fail.
- **The hook reads its credential from `~/.preloop`.** That directory must be writable at onboarding time and persistent afterwards. A named volume works.
- **When `~/.preloop` is read-only, hooks are not installed,** and onboarding says so: `Approval hooks were not installed: the hook reads its credential from ~/.preloop, which is not writable here.` Make `~/.preloop` writable and rerun onboarding with `--approvals`.
- **The environment-variable form never enables hooks.**

When the hook cannot reach Preloop, or finds no credential, it **fails closed**: the tool call is denied with a reason that starts with `Preloop approval hook denied by default`. A call that your own Claude Code permissions already deny stays denied. The hook command has a `--fail-open` flag that allows calls on connection failures, timeouts and HTTP 5xx responses only (never on authentication errors or a returned deny), but onboarding does not set it.

If a container is stuck because every tool call is refused, run `preloop agents pause` inside it. See [If approvals cannot reach Preloop](../cli.md#if-approvals-cannot-reach-preloop).

## In the console

While the setup waits, the agent's page shows **Waiting for local configuration**, with the expiry time, the resume command, and a **Cancel setup** button.

The **Set up manually** panel on the same page shows the instructions again, with the placeholder `<AGENT_KEY>` where the key goes. Preloop does not keep the key, so it cannot show the one the CLI printed. The panel can issue a new key, shown once and filled into the instructions while you stay on the page.

!!! note "Which key a pending setup accepts"
    `preloop agents onboard --resume` expects the key the CLI printed for that setup. A different key for the same agent, such as one issued from the console panel, is accepted only when the real request made by resume succeeds with it. With `--skip-live-validate`, or for an agent with no model route, nothing can prove a different key, and resume reports that the key in place is not the one issued for this setup. If you lost the printed key, issue one in the console and resume without skipping the request, or cancel the setup and onboard again.

## Expiry and cancel

A pending setup does not wait forever. It expires 72 hours after it was created. A self-hosted operator can change that with `PRELOOP_PENDING_ENROLLMENT_TTL_HOURS` on the server (hours, minimum 1). Expired setups are removed by an hourly job.

When a setup expires, Preloop removes the key, the models that onboarding created and, if onboarding created it and it was never used, the agent. Resume then says so:

```text
the pending setup for Claude Code expired and Preloop removed its key. Run onboarding again: preloop agents onboard "Claude Code"
```

To do the same immediately:

```bash
preloop agents onboard --cancel "Claude Code"
```

```text
Cancelled the pending setup for Claude Code.
  Preloop: removed 1 agent key(s) and 1 model(s) it created; agent deleted.
  This machine: /home/agent/.claude/settings.json does not point at Preloop; nothing to change.
```

When `~/.preloop` is not writable, add `--resume-token <token>` so the CLI knows which setup to cancel.

A failed onboarding cleans up in the same way. It removes what it created in Preloop, restores the local files it changed, and reports the two separately:

```text
  Local cleanup: previous configuration restored.
  Server cleanup: removed 1 agent key(s) and 1 model(s) this run created; agent deleted. Nothing half-made is left.
```

## Offboarding

```bash
docker compose exec agent preloop agents offboard "Claude Code"
```

When the configuration is read-only, offboarding does not pretend to have changed it. It prints the exact removal and changes nothing in Preloop until the block is gone:

```text
Manual removal for Claude Code
------------------------------------------------------------------------
/home/jane/.claude/settings.json
   Not writable from here: read-only file system
   Edit it on the host, probably at: /home/jane/.claude/settings.json
   Remove these keys:
     env.ANTHROPIC_BASE_URL
   Set these keys (to what they were before Preloop):

{
  "env": {
    "ANTHROPIC_API_KEY": "<your previous value>"
  }
}

   <your previous value> stands for a credential of yours; its value is in the backup: <backup path>
   Or replace the file with the complete result: /home/jane/.preloop/manual-setup/<agent>-offboard/settings.json
Guide: https://docs.preloop.ai/guide/clients/sandboxed-agents/#offboarding
------------------------------------------------------------------------
Nothing has been changed in Preloop yet: the agent keeps working until you remove the block.
To remove the Preloop side now and edit the file later, add --server-only.
```

- **Your own credentials are never printed.** A placeholder points at the backup that holds the value.
- In a terminal, offboarding waits (`Press Enter when the block is removed`), reads the file again, and names anything still there (`✗ still present: env.ANTHROPIC_BASE_URL`). Without a terminal it stops with exit status 3. Run the same command again after editing the file.
- `Offboarded` is printed only for a file that no longer points at Preloop. Preloop and the machine are reported on separate lines:

```text
✓ Offboarded Claude Code
  Preloop: agent <agent id> archived and its key revoked.
  This machine: you removed the Preloop block from /home/jane/.claude/settings.json (checked).
```

Two flags cover the cases where the machine cannot be put back fully:

| Flag | Effect |
|------|--------|
| `--server-only` | Removes the Preloop side even though the file still holds the Preloop block. The result reads `NOT offboarded on this machine; the Preloop side was removed`, and the command exits non-zero. The key in the file no longer works, so the agent fails until you remove the block |
| `--no-login-restore` | Skips writing the live subscription login back to this machine. Use it when the credentials file is read-only or shared. The login Preloop holds is discarded: sign in again afterwards with `claude auth login --claudeai` |

By default, offboarding writes the live subscription login back before it removes the Preloop side, and stops if it cannot. A login imported from `CLAUDE_CODE_OAUTH_TOKEN` needs no restore: Preloop never rotated it, and the token in your environment is still the live one.

If you used the environment-variable form, also remove `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` (or the `env_file` entry) from your Compose configuration and recreate the container.

## Troubleshooting

| What you see | What it means | What to do |
|--------------|---------------|------------|
| `invalid JSON at line 6, column 3: trailing comma before "}"; remove the comma at the end of line 5` | The last entry of an object or array ends with a comma | Remove that comma |
| `invalid JSON at line 4, column 5: missing comma; add one at the end of line 3` | Two entries follow each other without a comma | Add the comma |
| `expected a key in double quotes here; check for a missing quote`, `a string is not closed; check for a missing double quote on this line`, or `missing colon or closing quote after this key` | A quote or colon is missing | Fix the named line |
| `the file ends early; a closing quote, } or ] is missing` | The paste was cut off | Paste the whole block, or copy the complete result file |
| `env.ANTHROPIC_BASE_URL is missing` (or another key) | That key from the block is not in the file resume read | Check that you edited the file the container mounts, and that the container sees the new content (see the single-file bind mount note) |
| `... still holds the placeholder <AGENT_KEY>; put the agent key there` | The block was copied from the console, which shows a placeholder | Put in the key the CLI printed |
| `... does not hold the key issued for this setup` | The file holds a different key: from an earlier run, or issued from the console | Put in the key printed by this setup (it is in the saved instructions), or a key issued in the console and resume without `--skip-live-validate` |
| `... is still present; remove it` | A key listed under `Remove these keys if present` is still in the file | Delete it |
| `the pending setup for ... expired and Preloop removed its key` | The setup waited longer than the expiry time | Run `preloop agents onboard` again and apply the new block. The old key no longer works |
| `no manual setup is recorded on this machine for ...` | `~/.preloop` is not writable or not persistent, so the resume state is gone | Pass `--resume-token <token>`, or re-run `preloop agents onboard "<agent>"` |
| `Anthropic rejected the Claude subscription login Preloop imported.` | The imported login is stale, usually because it is shared with another machine | See [How the Claude login reaches the container](#how-the-claude-login-reaches-the-container), then `preloop agents reconnect "Claude Code" --from-local` and resume |
| `... configuration is not writable from here and manual setup is not available for this agent yet` | Read-only configuration is not supported yet for that agent | Nothing was changed. Make the file writable for the onboarding run |
| Exit status 3 | Not a failure: the command is waiting for a manual step (apply the block, or remove it when offboarding) | Make the change, then run the command that was printed. A resume that finds problems, and any real failure, exits with status 1 |

## A rig that reproduces these setups

[`examples/sandboxed-agent-rig`](https://github.com/preloop/preloop/tree/main/examples/sandboxed-agent-rig) reproduces these setups against a local Preloop stack with a mocked model provider. Nothing in it reaches a real provider.

## Related

- [Claude Code reference](claude-code.md)
- [Subscription credential recovery](subscription-recovery.md)
- [CLI reference](../cli.md#manual-setup-for-read-only-configuration)
- [Troubleshooting](../troubleshooting.md)
