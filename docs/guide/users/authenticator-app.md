# Authenticator app and recovery codes

Preloop includes authenticator-app two-factor authentication in self-hosted and
hosted installations, including installations with a single user.

## Set up an authenticator app

1. Open **Settings → Security → Authenticator app**.
2. Select **Set up authenticator app**. Scan the QR code in an authenticator app,
   or enter the displayed secret manually. The QR code is generated in your
   browser; it is not sent to an external QR service.
3. Enter the six-digit code to confirm. Your other non-SSO sessions end.
4. Copy or download the ten recovery codes and keep them somewhere private,
   separate from your phone. Select **I saved them**, then **Done**.

After password sign-in, enter the current authenticator code. Each code can be
used only once. If you just used one to enroll or change a setting, wait for the
next code. Preloop accepts one 30-second step of clock drift in either direction.

Passkeys and single sign-on do not ask for an additional authenticator code.
Requiring a passkey still disables password sign-in, even when an authenticator
app is enrolled. API keys and runner credentials are unaffected.

## Recover access

On the second sign-in step, select **Use a recovery code** and enter one of your
saved codes. Each recovery code works once. In Security settings you can see
how many remain and replace the entire set by entering a fresh authenticator
code. Replacing codes invalidates every old code.

If you lose both the phone and the recovery codes, contact the administrator of
your Preloop server. There is no self-service reset by email. See the
[operator recovery procedure](../../operations/mfa.md).

## Confirm sensitive changes

Removing a passkey or changing passkey-only sign-in requires strong
authentication within the last ten minutes. Preloop prompts for a fresh
authenticator code or passkey assertion when needed, then retries the action
once. For an old single sign-on session, continue through your identity provider.
If the retry is refused, the error is shown; it does not loop.

Turning off the authenticator app and replacing recovery codes each take their
own fresh code. They do not ask for a separate step-up code first. Turning off
the app also deletes all recovery codes.

## CLI sign-in

`preloop auth login` completes sign-in in your browser, including the second
step, before returning to the CLI. No interactive terminal MFA prompt is needed.
Older clients that request a password token directly receive HTTP 401 for an
enrolled user; use browser sign-in instead.
