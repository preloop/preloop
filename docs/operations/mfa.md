# Operating authenticator authentication

Authenticator apps and recovery codes are available in Preloop without an
edition plugin. Enrollment is optional per user. Organization-wide policies,
coverage reporting, and resetting another member from the console are separate
Preloop Enterprise features; the core change does not implement those policies.

## Lost phone and recovery codes

Confirm the person's identity out of band. From the API installation, run:

```sh
python scripts/reset_mfa.py --email jane@example.com --reason "lost device"
```

The command deletes the authenticator factor and recovery codes for each
membership with that address, ends its non-SSO sessions, and writes one
`auth.mfa.reset` event per membership with the operator's reason. It exits 1 if
no user holds the address. It preserves passkey-only sign-in; use the separate
[passkey recovery procedure](../guide/users/passkeys.md) if that is also needed.
Never put a secret or recovery code in the reason.

This works on single-user installations. It requires administrative access to
the server, not a working browser session for the locked-out user.

## Storage, limits, and rollback

Back up the database and its encryption key together. TOTP secrets use the same
Fernet encryption utility and key configuration as the secret service. Recovery
codes use the password hasher and cannot be retrieved after issuance.

Verification, replay protection, recovery-code consumption and challenge use
share a database transaction under row locks. MFA budgets use the shared
`rate_limit_bucket` table: five failures per challenge lifetime and ten failures
per user per fixed hour. The fifth failure returns `invalid_code`; subsequent
attempts on that challenge return `challenge_locked`. A challenge expires after
five minutes. These mandatory MFA limits do not accept the generic limiter's
disable/override settings and do not fall back to process-local budgets during
a database outage. Other limiter rules retain their existing behavior.

`MFA_TOTP_ENABLED=false` is an emergency rollback switch. It hides enrollment
and skips the second sign-in step **for every enrolled user**, reducing password
sign-in to one factor. Keep this period short and restore the default `true`
after resolving the problem. Existing factors remain stored. Passkey-only
sign-in is still enforced.

The migration adds the two MFA tables and an account `step_up_window_minutes`
column defaulting to zero. Downgrade deletes the factors and recovery hashes.
Coordinate rollback with application code and take a backup before downgrading.

## Step-up policy extension

Core always uses a ten-minute window for passkey removal and passkey-only
settings. The account window is inert without an edition provider. A provider
registered with `account_hooks.register_step_up_policy` may return a freshness
window for approval decisions and API-key creation/deletion. The
`register_factor_removal_policy` guard may refuse removal of a required factor.
Providers run in the caller's transaction and must not commit.

Approvals made through mobile apps, watches, Slack or Mattermost do not carry
a console session and are not subject to console step-up. They remain governed
by device pairing and channel identity. Any edition UI exposing account
step-up settings must display this limitation alongside the setting.
