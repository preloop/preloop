# Operating authenticator authentication

Authenticator apps and recovery codes are available in Preloop without an
edition plugin. Enrollment is optional per user. Organization-wide policies,
coverage reporting, and resetting another member from the console are separate
Preloop Enterprise features supplied through optional policy hooks.

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

## Restoring a database: keep SECRET_KEY

Authenticator secrets are encrypted with `SECURITY__ENCRYPTION_KEY`. When that
setting is empty, as in the release compose bundle, the key is derived from
`SECRET_KEY`. Restore a database next to the same `.env` it was backed up with.

If the key differs, the authenticator app can no longer be checked:

- A correct authenticator code is refused with `factor_unreadable` (HTTP 409).
  The sign-in screen says the encryption key changed and switches to a
  recovery code. The API log has one error line naming the user id.
- Recovery codes still work, because they are hashed, not encrypted.
- **Settings → Security** shows the authenticator app as needing to be set up
  again. The user removes it with a recovery code and sets it up again.
- The sign-in coverage table counts the member as having no working
  authenticator.

To recover, put the original `SECRET_KEY` (or `SECURITY__ENCRYPTION_KEY`) back
and restart; every authenticator works again. If the original key is lost,
each user signs in with a recovery code and sets the authenticator up again.
A user with no recovery code left needs a reset with `scripts/reset_mfa.py`
(see above) or, in Preloop Enterprise, **Reset sign-in** from an
administrator.

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

## Account policy and restricted sessions

An edition provider can require strong sign-in using
`register_sign_in_strength_policy`. Core stores `strong_sign_in_policy`
(`off`, `admins`, `all`, default `off`) and provides the restricted-session
boundary; without a provider, sign-in remains unchanged. Policy decisions run at
sign-in, refresh, account switch, grants, HTTP access and live-session checks.

A weak sign-in can receive `enroll_only: true`. It may read its profile and factor
status, enroll TOTP or a passkey, refresh, sign out, or switch accounts. Other
APIs and authenticated WebSockets refuse it. TOTP confirmation returns a normal
session; a registered passkey must be used for sign-in. Factor removal may return
`mfa_required_by_policy` until another usable factor is available.

Account enforcement can keep a reset user at enrollment even after
`reset_mfa.py`. If the last administrator loses every factor, a server operator
first disables the account policy with the edition's audited policy reset
script, then uses `reset_mfa.py` and, when needed, `reset_passkey_only.py`.
Administrative server access is sufficient; a browser session is not required.
Disabling `MFA_TOTP_ENABLED` does not disable an account's strong-sign-in policy.

Social browser sign-in carries a short-lived challenge to the second-step screen
in a URL fragment, which is removed immediately. No session token is minted
before proof. CLI social sign-in returns the completed token pair only to a
validated loopback callback after the second factor succeeds.


CLI social sign-in binds the loopback callback from validated OAuth state into the
signed MFA challenge. Verification rejects a changed callback before consuming
the code, and returns only the signed callback for browser delivery. The fragment
address is a hint, never the authority for where completed tokens are sent.
