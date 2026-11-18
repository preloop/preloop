# Authenticator app and recovery codes

Preloop includes authenticator-app two-factor authentication in self-hosted and
hosted installations, including installations with a single user.

## Set up an authenticator app

1. Open **Settings → Security → Authenticator app**.
2. Select **Set up authenticator app**. Scan the QR code in an authenticator app,
   or enter the displayed secret manually. The QR code is generated in your
   browser; it is not sent to an external QR service.
3. Enter the six-digit code and select **Confirm code**. Your other non-SSO
   sessions end.
4. Copy or download the ten recovery codes and keep them somewhere private,
   separate from your phone. Select **I saved them**, then **Done**.

After password sign-in, the **Confirm your sign-in** step asks for the current
authenticator code; enter it and select **Verify**. Each code can be used only
once. If you just used one to enroll or change a setting, wait for the
next code. Preloop accepts one 30-second step of clock drift in either direction.

Passkeys and single sign-on do not ask for an additional authenticator code.
Requiring a passkey still disables password sign-in, even when an authenticator
app is enrolled. API keys and runner credentials are unaffected.

## Recover access

On the **Confirm your sign-in** step, select **Use a recovery code**, enter one
of your saved codes and select **Verify**. Each recovery code works once. In Security settings you can see
how many remain and replace the entire set by entering a fresh authenticator
code. Replacing codes invalidates every old code.

If you lose both the phone and the recovery codes, contact the administrator of
your Preloop server. There is no self-service reset by email. See the
[operator recovery procedure](../../operations/mfa.md).

## Confirm sensitive changes

Removing a passkey or changing passkey-only sign-in requires strong
authentication within the last ten minutes. Preloop opens **Confirm it’s you**
and asks for a fresh authenticator code (**Verify code**) or a passkey
(**Use a passkey**) when needed, then retries the action
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

## When your account requires strong sign-in

Your administrator may require an authenticator app, passkey or single sign-on.
If you arrive at **Protect your sign-in**, add an authenticator app (**Set up
authenticator app**) or, where offered, a passkey (**Add a passkey** or **Use an
existing passkey**) before you can use the console. After **Confirm code**, save
the recovery codes and select **Done**. For a passkey, complete registration and
sign in with it. Until then, ordinary console pages and APIs are unavailable.
**Use another sign-in method** and **Sign out** leave this screen without
enrolling.

The account can prevent removal of your last usable factor. Add another factor
before removing one, or ask an administrator to reset your sign-in after verifying
your identity. A reset ends sessions and requires enrollment again.

An administrator resets a member from the member sign-in coverage table with
**Reset sign-in**. The dialog asks for a reason. If the administrator's last
authenticator code or passkey is older than the step-up window (at most ten
minutes), **Confirm it’s you** asks for a fresh one first. If you are the
only administrator and lose all factors, the server operator can use the
[server-side recovery procedure](../../operations/mfa.md).

Social sign-in also shows the authenticator or recovery-code step, including when
you started from the CLI. Keep that browser tab open until sign-in completes.
