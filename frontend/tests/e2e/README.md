# Browser integration tests

The shared Playwright configuration has an `e2e-ci` project. Its existing
`npm run test:e2e:ci` command requires a running backend and PostgreSQL; the
custom-agent journey additionally needs the fake upstream described in its spec.
The main CI workflow runs frontend unit tests and the CSP sweep, rather than
automatically starting the full browser integration stack.

## Local TOTP journey

`.github/workflows/totp-e2e.yml` runs the TOTP journey on relevant pull requests
(including stacked PRs), pushes to main, and manual dispatch. It starts isolated
PostgreSQL and NATS, migrates the database, builds the CLI, and runs:

```sh
PRELOOP_DISABLE_TELEMETRY=true npx playwright test --config playwright.totp.config.ts
```

For a local run, use a disposable PostgreSQL database and NATS instance. From the
repository root, create a clone-local venv and install the app with its dev extra.
Export `PYTHONPATH="$PWD/backend"`, `DATABASE_URL`, `NATS_URL`, a synthetic
`SECRET_KEY`, `TESTING=true`, `REGISTRATION_ENABLED=true`,
`PRELOOP_DISABLE_TELEMETRY=true`, `MODEL_PRICE_LIVE_LOOKUP_ENABLED=false`,
`WEBAUTHN_RP_ID=localhost` and `WEBAUTHN_ORIGIN=http://localhost:5173`.
Create a private temporary directory and export its absolute path as
`PRELOOP_E2E_DIR`. Create `outbox/` and `cli-home/` inside it and initialize `clock`
with a Unix timestamp rounded down to a 30-second boundary. Then run:

```sh
.venv/bin/python scripts/init_db.py --force
make -C cli build
PRELOOP_E2E_PYTHON="$PWD/.venv/bin/python" \
  .venv/bin/python -m tests.e2e_support.totp_server serve
# In another terminal with the same environment:
cd frontend
npm ci
npx playwright install chromium
PRELOOP_E2E_PYTHON="$(cd .. && pwd)/.venv/bin/python" \
  npx playwright test --config playwright.totp.config.ts
```

The configuration extends the existing browser harness and starts a local Vite
server with the instance's `/oauth` proxy for CLI sign-in. For unused local ports,
set `PRELOOP_E2E_FRONTEND_PORT`, `WEBAUTHN_ORIGIN`, `PRELOOP_E2E_API_URL` and
`VITE_API_PROXY_TARGET`, and pass the API port to `totp_server serve --port`.
Stop the server and dispose of this test stack afterward.

The standalone test server replaces the MFA and request-limiter clocks with a
file reader and SMTP delivery with a private outbox. The driver advances time explicitly and
generates proofs with the independent `pyotp` library, including guaranteed
invalid codes. It never waits for a TOTP rollover, an hour boundary, or a ten-minute
step-up window. JWT expiry still runs normally. There are no test control routes,
and production server code is unchanged. Test-only approval fixtures use CRUD;
email-link proofs and operator recovery use existing core helpers/scripts.

Coverage includes enrollment/QR/recovery display, actual console sign-out, both
password transports, wrong codes, replay, challenge and user lockouts, next-hour
recovery, one-time/replaced recovery codes, factor step-up, email/password reset,
real CLI OAuth, challenge token typing, API-key and approval-link exemptions,
member isolation, disable, operator reset, revocation, and audit event/privacy
assertions. WebAuthn uses Chromium's software authenticator; there are no live
model providers, external identity providers or edition policy plugins.

One narrowly scoped expected failure records a current defect: enabling
passkey-only sign-in after password plus OTP returns a token pair immediately
refused with `password_login_disabled`, despite the documented promise that the
current session stays signed in. Its setup remains ordinary assertions; only the
final current-session assertion is marked as an expected failure. A fix produces
an unexpected pass, prompting removal of the marker. D1 is documented in
[the test PR](https://github.com/preloop/preloop/pull/2343), with the originating
feature in [#2299](https://github.com/preloop/preloop/pull/2299). No issue was
opened under this test brief, and no product fix is included.

Screenshots mask secrets, QR images, recovery lists and proof inputs. Traces,
videos and automatic screenshots are disabled for these tests; CI uploads only
the explicitly masked PNG evidence, never the private outbox or CLI HOME.
Helper and startup failures report traceback locations and exception classes;
raw messages and bound parameters are omitted because they can contain proofs.
