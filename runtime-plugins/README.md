# Preloop Runtime Plugins

This directory contains standalone, open-source runtime plugins for external
agents. These are not Preloop EE server plugins and should not live under the
top-level `plugins/` directory.

Runtime plugins are responsible for the agent-side Preloop exposure contract:

- read the CLI-written `preloop.control` configuration
- keep the Agent Control WebSocket connected
- advertise runtime capabilities and presence
- route operator text or voice-transcript turns into the native agent runtime
- emit command results and status events back to Preloop

The packages here are intentionally structured so they can be published from
this repository first, then split into dedicated standalone repositories later
without changing their package names.

Packages:

- `hermes-preloop`: `preloop-hermes-plugin` (PyPI), in-process Hermes plugin
- `openclaw-preloop`: `@preloop-ai/openclaw-plugin` (npm), in-process
  OpenClaw plugin
- `claude-preloop`: `@preloop-ai/claude-plugin` (npm), sidecar daemon for
  Claude Code (no in-process plugin API; built on the Claude Agent SDK)
- `opencode-preloop`: `@preloop-ai/opencode-plugin` (npm), in-process
  OpenCode plugin (permission prompts bridged via OpenCode's plugin
  `event` hook)
