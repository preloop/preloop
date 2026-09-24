# Codex CLI onboarding

`preloop agents onboard "Codex CLI"` enrolls Codex, routes model traffic
through the Preloop gateway, and can install approval hooks with
`--approvals`. Codex keeps `~/.codex/config.toml` for its own settings.
Agent Control does not write that file.

## Agent Control sidecar

Codex has no in-process plugin API for operator messages, so Agent Control
runs as a sidecar:

- package `@preloop-ai/codex-plugin`
- command `preloop-codex-plugin`
- source `runtime-plugins/codex-preloop`
- config `~/.codex/preloop-control.json`

Onboarding installs the package with npm when it is published, or from the
local source directory when that checkout is present. It writes the same
control keys the Claude sidecar uses (`enabled`, `protocol`, `runtime`,
`control_ws_url`, `bearer_token`, and the runtime identity fields). Nothing
Codex-specific is added to that file.

```bash
preloop agents onboard "Codex CLI"
preloop agents validate "Codex CLI"
preloop codex sidecar enable
preloop codex sidecar status
preloop codex sidecar disable
```

`validate` reports `control_config_written`, `control_plugin_installed`,
`control_plugin_verified`, and `control_channel_configured` separately.
`preloop codex sidecar run` execs `preloop-codex-plugin run` against the
control file. It is the command launchd and systemd start.

Offboard removes `~/.codex/preloop-control.json` and the sidecar service.
`~/.codex/config.toml` is restored from the onboarding backup and is not
rewritten by Agent Control.
