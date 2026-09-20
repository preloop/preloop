# Hermes: onboarding, rollback, and systemd recovery

Hermes is a supported managed agent. This page is the operational playbook
for connecting it to Preloop, undoing that connection, and recovering when
tools stay gated after a config change. The systemd-first kill order comes
from [issue #828](https://github.com/preloop/preloop/issues/828).

## Onboarding

```bash
export PRELOOP_DISABLE_TELEMETRY=true
preloop login
preloop agents onboard Hermes
preloop agents validate Hermes
```

`preloop agents onboard Hermes` discovers `~/.hermes/config.yaml` or
`~/.hermes/config.yml` (or an installed-but-unconfigured Hermes tree),
backs up the file under the Preloop config directory, writes a
`preloop.control` block plus a managed MCP server, and can rewrite model
settings to Preloop's OpenAI-compatible gateway. It then installs
`preloop-hermes-plugin` into Hermes' virtualenv and runs
`hermes gateway restart` so the running process reloads what was written.

Use `--skip-live-validate` only when you cannot reach the control plane.
`--model` selects which account model the gateway rewrite should pin.

Plugin-only (native tool approvals and the Talk channel, no model or MCP
rewrite):

```bash
preloop agents install-plugin Hermes
```

That command now restarts the Hermes gateway the same way onboarding does.

## What onboarding writes and restarts

On disk, onboarding updates the discovered Hermes YAML:

- `preloop.control` with `control_ws_url`, the runtime bearer token, and
  principal identity
- a managed `preloop` entry under `mcp_servers`
- optional `model` rewrite to Preloop's `/openai/v1` gateway

The CLI writes `~/.hermes/config.yaml` when it has to create a file. If a
`.yml` sibling already exists, that is the file it edits.

The running Hermes process does not pick that up by magic. Onboarding
therefore runs `hermes gateway restart`. Offboard, `preloop agents restore
Hermes`, and `preloop agents install-plugin Hermes` call the same helper.
If `hermes` is not on `PATH`, the command warns and continues; run
`hermes gateway restart` yourself.

When systemd user units named `hermes-*` are present (Linux only, best
effort), the CLI prints them and the exact restart command, for example:

```text
Found systemd user units that can respawn Hermes: hermes-gateway.service, hermes-matrix-monitor.service
Restart with: systemctl --user restart hermes-gateway.service hermes-matrix-monitor.service
Stop them before killing processes: systemctl --user stop hermes-gateway.service hermes-matrix-monitor.service
```

## Offboarding and restore

Offboard is reversible. It restores the backed-up Hermes config, archives
the managed agent in Preloop, and restarts the gateway so the live process
drops the control block.

```bash
preloop agents offboard Hermes --yes
```

The config backup is not next to `config.yaml`. Onboarding stores it under
the Preloop config directory:

```text
~/.preloop/agents/backups/<runtime-principal-id>/<timestamp>-config.yaml
```

(`PRELOOP_CONFIG_DIR` relocates that tree.) Offboard restores from that
path. To restore the backup without archiving the managed agent:

```bash
preloop agents restore Hermes --yes
```

That is the CLI command. There is no `preloop agents onboarding restore`
alias.

Moving `~/.preloop` aside is an extra operator step, not something
offboard does. If you do that, keep a copy so you can move it back before
`preloop agents restore Hermes`.

Uninstalling the plugin package is also optional and separate:

```bash
# use the Hermes virtualenv pip, not system pip
"$HERMES_VENV/bin/pip" uninstall -y preloop-hermes-plugin
```

## Why a running gateway must be restarted

The plugin re-reads YAML on each failed control-block lookup. It does not
keep a stale "missing `preloop.control`" result in memory. Two other facts
still make a live process ignore a file you just edited:

1. **Discovery can pick a different file than onboarding wrote.** The
   plugin searches `$HERMES_HOME/config.yaml` and `config.yml`, then
   `~/.hermes/config.yaml` and `config.yml`. A systemd user unit often
   has a different `HERMES_HOME` (or a `.yml` sibling) than your
   interactive shell. Until recently the plugin did not prefer the file
   that actually contains `preloop.control`, so a gateway started by
   systemd could fail closed with `missing preloop.control config block`
   while `preloop agents validate Hermes` against the onboarded file
   looked fine. That matches "intermittent, then consistent" as systemd
   respawns win over a shell-started Hermes.
2. **Successful settings are cached for the life of the process.** After
   a control block loads, later tool calls reuse it. Stripping or
   rewriting the file does not unload that process. Restart the gateway
   (and any systemd unit that will spawn another copy).

A liveness probe inside the plugin cannot see that systemd is about to
respawn a sibling with a different environment. Restart the process that
will actually serve tools.

## Stop systemd before killing processes

Killing `hermes_cli.main` or the desktop GUI first is not enough when
user units are enabled. systemd starts them again in seconds, still with
the unit's environment.

Order that works (Linux, credited to issue #828):

```bash
export PATH="$HOME/.local/bin:$PATH" PRELOOP_DISABLE_TELEMETRY=true

preloop agents offboard Hermes --yes

# 1) Stop the respawners first
systemctl --user stop hermes-gateway.service hermes-matrix-monitor.service
sleep 2

# 2) Then kill leftover processes and confirm they are gone
pkill -f "hermes_cli.main" || true
pkill -f "Hermes" || true
sleep 2
ps -eo pid,args | grep -E 'hermes_cli|Hermes' | grep -v grep || echo ALL_GONE

# 3) Start one fresh process so it reads the restored config
hermes gateway restart

# 4) Bring the user units back if you still want Matrix/messaging on boot
systemctl --user restart hermes-gateway.service hermes-matrix-monitor.service
```

Unit names vary. Use the names the CLI printed, or:

```bash
systemctl --user list-units 'hermes-*' --all --no-legend
```

## Prove the plugin sees the right config

```bash
preloop agents validate Hermes
preloop-hermes-plugin verify
```

If tools still gate, the fail-closed message now names the file that was
read and whether `HERMES_HOME` / `HOME` were set, for example:

```text
Preloop approval unavailable: missing preloop.control config block
(read /home/you/.hermes/config.yaml; HERMES_HOME=<unset>, HOME=/home/you).
run `preloop agents validate Hermes`; if the path differs from the one
onboarding wrote, restart the Hermes gateway and any systemd user units
(`systemctl --user restart hermes-gateway.service`)
```

Compare that path with the file onboarding reported. If they differ,
restart the gateway and the systemd units so the live process uses the
same discovery environment you just validated. The gate still fails
closed; only the message changed.

## Model routing versus tool routing

Two layers, installed differently:

- **Tool routing (the plugin).** Native Hermes tools (`terminal`,
  `read_file`, and the rest) go through `pre_tool_call` to Preloop
  approvals. MCP tools are a different path: they need the managed MCP
  server from a full onboard.
- **Model routing (full onboard).** `preloop agents onboard Hermes`
  points Hermes' model client at Preloop's gateway. That is what
  produces per-agent spend, budgets, and allowed-model lists. It can
  also change which model feels "fast" if the gateway pin is not the
  model you used directly.

Plugin-only / MCP-proxy-only keeps direct model calls. Full onboard
routes models through the gateway. Offboard restores the backed-up
`model` block so direct routing returns.

## Informational startup lines

These lines are not errors. They show up during install or when a user
unit already exists:

- `install stamp:` records which Hermes build launched. Useful when
  comparing a GUI binary with a gateway unit.
- `DEP0180` is a Node.js runtime deprecation notice from a dependency.
  It does not mean Preloop onboarding failed.
- `UnitExists` from systemd means `hermes-gateway.service` (or another
  `hermes-*` unit) is already installed. Enable/start still works;
  `systemctl --user restart` is the reload you want after a config
  change.

If tools are gated, ignore these lines and use the verify path above.
