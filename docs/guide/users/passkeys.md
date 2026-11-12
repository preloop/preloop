# Passkeys and passkey-only sign-in

Editions: all (Preloop, Preloop Cloud, Preloop Enterprise).

A passkey signs you in with your device's screen lock, fingerprint or
security key instead of a password. Preloop asks the device to verify you
(PIN or biometric) every time, so a passkey sign-in is two factors: the device
and you.

## Add a passkey

Open **Settings > Security** and choose **Add passkey**. Your browser or
operating system asks you to confirm with your PIN, fingerprint or face. A
security key without a PIN is refused with "This security key did not verify
you. Set a PIN or biometric on it, or register a different passkey."

## Require a passkey to sign in

With **Require a passkey to sign in** turned on, your account accepts only
passkey and single sign-on (SSO) sign-in. Password, email-link, chat and
social sign-in stop working for you, so a stolen password no longer opens
your account.

1. Add at least one passkey (two, on different devices, is safer).
2. Sign out and sign back in **with your passkey**. The switch can only be
   changed within 10 minutes of a passkey sign-in; in a password session it is
   shown but disabled.
3. On **Settings > Security**, turn on **Require a passkey to sign in**.

Turning it on signs out every other console and CLI session of yours; the
session you are using stays signed in. Turning it off needs a recent passkey
sign-in too.

While it is on:

- The password form answers "This account signs in with a passkey" and moves
  focus to **Sign in with passkey**. A wrong password still gets the ordinary
  error.
- You cannot remove your last passkey.
- A password reset still changes your password, but does not sign you in and
  does not turn the setting off.
- API keys and runner tokens keep working. The CLI signs in through the
  browser, so `preloop auth login` uses your passkey.

## Lost every passkey

There is no self-service recovery by email: that would make the account one
factor again. Ask an administrator of your Preloop server to confirm your
identity and run, in the API container:

```bash
python scripts/reset_passkey_only.py --email you@example.com \
  --reason "lost laptop, identity confirmed by phone"
```

The script turns the setting off for every membership with that address, ends
your non-SSO sessions and writes an `auth.passkey_only.reset` audit event with
the reason (first 128 characters). It exits non-zero when no user has the
address. You can then sign in with your password (or reset it), add a new
passkey and turn the setting on again.

## For operators

If requiring user verification locks out people whose security keys have no
PIN, set `PASSKEYS_REQUIRE_USER_VERIFICATION=false` on the API. Passkeys then
fall back to `preferred` verification, and nobody can newly turn on
passkey-only sign-in until it is set back. Users who already turned it on stay
enforced and keep signing in with their passkey.

Details: [Security architecture, passkey sign-in](../../architecture/security.md#passkey-sign-in).
