# Preloop Hermes Plugin

`preloop-hermes-plugin` exposes Hermes to Preloop without requiring the Preloop
CLI to be present at runtime.

The plugin reads the `preloop.control` block from `~/.hermes/config.yaml`,
connects Hermes to Preloop over the Agent Control WebSocket, advertises runtime
capabilities, routes operator prompts or voice transcripts into Hermes, and
gates native tool calls through Preloop mobile/watch approvals via the
`pre_tool_call` hook (fail-closed by default).

Set `preloop.control.tool_approval.enabled: false` to disable the gate, or
`preloop.control.tool_approval.fail_open: true` / `PRELOOP_TOOL_APPROVAL_FAIL_OPEN=1`
to allow tools when Preloop is unreachable.

## Install

```bash
pip install preloop-hermes-plugin
```

If your Hermes build wraps PyPI installs:

```bash
hermes plugins install preloop-hermes-plugin
```

For local development from this repository:

```bash
cd preloop/runtime-plugins/hermes-preloop
python -m pip install -e .
```

## Configure

The plugin does not require the Preloop CLI at runtime. Existing Preloop users
can still let the CLI provision `preloop.control`:

```bash
preloop agents onboard hermes
preloop agents install-plugin hermes
preloop agents validate hermes
```

Restart Hermes after installation. When this plugin connects and advertises
capabilities, Preloop marks Agent Control verified and Talk appears for the
agent in web and mobile clients.

When installed from the Hermes marketplace or UI, the plugin should prompt for
Preloop login or signup if that config block is missing. The standalone helper
uses Preloop's browser OAuth flow to create a runtime bearer token and writes the
config automatically; users should not hand-author `bearer_token` values.

The generated `~/.hermes/config.yaml` contains:

```yaml
preloop:
  control:
    enabled: true
    protocol: preloop.agent_control.v1
    runtime: hermes
    control_ws_url: wss://staging.preloop.ai/api/v1/agents/control/ws
    bearer_token: agt_...
    runtime_principal_id: hermes-...
    runtime_principal_name: Hermes
```

## Manual Test Without Preloop CLI

```bash
pip install preloop-hermes-plugin
preloop-hermes-plugin login --config ~/.hermes/config.yaml
preloop-hermes-plugin verify --config ~/.hermes/config.yaml
preloop-hermes-plugin run --config ~/.hermes/config.yaml
```

`login` should open Preloop login/signup and write the runtime control config.
`verify` checks the config shape and confirms the plugin can load the Preloop
runtime client. `run` opens the Agent Control WebSocket and advertises
capabilities. In Preloop, the agent should become `control_online=true` and the
web/mobile clients should show Talk.

To test a prompt manually, open the Preloop web UI, choose the Hermes agent,
click Talk, and send a short message. The plugin should forward the message to
Hermes and send a command result back to Preloop.

## Verify

```bash
preloop-hermes-plugin verify --config ~/.hermes/config.yaml
```

## Publishing status

The package is published to PyPI as
`preloop-hermes-plugin`. Before publishing, run:

```bash
python -m build
python -m twine check dist/*
```

See `../PUBLISHING.md` for the full PyPI release checklist. Hermes discovers plugins via the `hermes_agent.plugins` entry point after install.
