# Recover a Claude subscription enrollment

If native Claude works but the gateway reports `invalid_grant`, keep the
existing enrollment and run:

```sh
preloop agents reconnect "Claude Code" --from-local
```

This command exists in CLI v1.0.2. It updates the credential shared by the
model families without changing the agent identity or policies. Make one normal
Claude request afterwards. If a previous onboarding attempt reverted model
routing, rerun `preloop agents onboard "Claude Code"` after reconnecting.

If native Claude also cannot authenticate, run `claude auth login --claudeai`
first. Do not repeatedly retry a rejected refresh token. Keep credential files
private; support only needs the error message.

Current onboarding rejects expired local subscription imports before changing
configuration. Discovery cannot verify a login from Keychain entry presence
alone. A pending or failed gateway probe is not successful onboarding. Unpinned
Claude family defaults do not require a custom-model menu entry for validation.

A server receiving only a reference to a failed shared credential cannot import
the fresh local login. It rejects that link with the reconnect command. A client
that presents a fresh payload can repair the shared secret in place. Healthy
shared credentials remain unchanged when attaching families, and consumed refresh
tokens cannot replace the live lineage.

Offboard must recover the current credential before deleting enrollment state.
Use a CLI that records the selected local credential store. An explicitly selected
file login can be restored over SSH while Keychain is locked. An unknown or
unavailable store stops offboard before credential export and server cleanup;
unlock Keychain or explicitly select the file with
`preloop agents sync-credentials "Claude Code"`, then retry. Configuration backups
alone do not restore a refresh token that has already rotated.
