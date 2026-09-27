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

Codex refreshes its ChatGPT login on its own, even when model traffic goes
through Preloop, and the Preloop gateway refreshes the copy it stores. That
login uses a single-use refresh token, so two holders of one grant revoke
each other when either refreshes with a stale token. The Codex permission
hook keeps both copies on the same lineage. It pushes the local bundle
(`~/.codex/auth.json`, or the macOS Keychain entry when Codex keeps its login
there) when it is newer than the stamp in the local enrollment state. About
every two minutes it also reads Preloop's rotation marker, which carries no
tokens, and when Preloop's copy is newer it writes that bundle back into the
same place Codex reads it. When both copies changed since the last sync, the
one with the later `last_refresh` wins and replaces the other. A pull only
happens when the local login and Preloop's copy name the same ChatGPT
account. A failed push or pull is logged once, leaves the local login and the
stamp as they were, and does not change the permission decision. A host with
no local login never gets one written back. When the hook is not installed, run
`preloop agents sync-credentials "Codex CLI"`; it reconciles in both
directions and prints which one ran.

For headless hosts, a single holder is still the recommendation: import the
login into Preloop, delete the local `auth.json`, and keep
`requires_openai_auth = false` (the default) on the Preloop model provider in
`~/.codex/config.toml`, so only Preloop refreshes the grant.

To push a Codex login through the API yourself, send `PUT /api/v1/ai-models/{id}`
with `credential_type: "oauth_openai_codex"` and a `credential_payload` in
Preloop's shape, not the key names from `auth.json`:

```json
{
  "access": "<access token>",
  "refresh": "<refresh token>",
  "account_id": "<ChatGPT account id>",
  "expires": 1893456000000
}
```

`access`, `refresh`, and `account_id` must be non-empty strings. `expires` is
the access-token expiry as an integer in epoch milliseconds. The server checks
the payload when you write it and answers 422 with the missing or invalid keys,
without storing anything. `access_token`, `refresh_token`, and `expires_at` are
rejected with a hint that names the expected key, and an `expires` in epoch
seconds or microseconds is rejected too. This is the same shape
`POST /api/v1/ai-models/{id}/credentials/export` returns.
