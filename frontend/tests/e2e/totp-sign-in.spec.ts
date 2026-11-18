/**
 * Real browser + PostgreSQL MFA journey. See README.md in this directory.
 * Proof time and email delivery are controlled by tests.e2e_support.totp_server;
 * the authentication routes, cryptography, transactions and audits are real.
 * No physical authenticator, external IdP, model provider or edition policy.
 */
import {
  test,
  expect,
  type APIResponse,
  type BrowserContext,
  type Page,
  type Response,
  type TestInfo,
} from '@playwright/test';
import { spawnSync, type SpawnSyncReturns } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const directory = process.env.PRELOOP_E2E_DIR;
const python = process.env.PRELOOP_E2E_PYTHON || join(root, '.venv/bin/python');
const apiOrigin = process.env.PRELOOP_E2E_API_URL || 'http://127.0.0.1:8000';
const cli = process.env.PRELOOP_E2E_CLI || join(root, 'cli/build/preloop');
const childEnv = {
  ...process.env,
  PYTHONPATH: join(root, 'backend'),
  PRELOOP_DISABLE_TELEMETRY: 'true',
};

interface User {
  username: string;
  email: string;
  password: string;
}
interface Tokens {
  access_token: string;
  refresh_token: string;
}
interface Enrollment {
  secret: string;
  recovery: string[];
  oldSession: string;
}
interface Mail {
  to: string;
  subject: string;
  text: string;
}

/** Preserve traceback locations without leaking SQL parameters or mail proofs. */
function helperFailure(label: string, result: SpawnSyncReturns<string>): Error {
  const diagnostic = spawnSync(
    python,
    ['-m', 'tests.e2e_support.totp_server', 'diagnose'],
    { cwd: root, env: childEnv, input: result.stderr || '', encoding: 'utf8' }
  );
  const systemCode = (result.error as NodeJS.ErrnoException | undefined)?.code;
  return new Error(
    `${label} failed (exit ${result.status}, ${systemCode || 'subprocess'}):\n` +
      (diagnostic.status === 0
        ? diagnostic.stdout.trim()
        : 'Traceback sanitizer unavailable')
  );
}

/** Execute a private helper without placing proofs on command lines or logs. */
function driver(command: string, payload: object): string {
  const result = spawnSync(
    python,
    ['-m', 'tests.e2e_support.totp_server', command],
    {
      cwd: root,
      env: childEnv,
      input: JSON.stringify(payload),
      encoding: 'utf8',
    }
  );
  if (result.status !== 0) throw helperFailure('The local MFA helper', result);
  return result.stdout.trim();
}

/** Advance only the server's proof clock; JWT expiry still uses real time. */
function advance(seconds = 30): number {
  const file = join(directory!, 'clock');
  const now = Number(readFileSync(file, 'utf8')) + seconds;
  writeFileSync(file + '.next', String(now), { mode: 0o600 });
  renameSync(file + '.next', file);
  return now;
}

/** Generate an interoperable pyotp code, with an explicitly selected step. */
function code(secret: string, fresh = true, invalid = false): string {
  const time = fresh
    ? advance()
    : Number(readFileSync(join(directory!, 'clock'), 'utf8'));
  return driver('totp', { secret, time, invalid });
}

/** Read a token without ever using an assertion that would print its value. */
async function session(page: Page): Promise<string> {
  const token = await page.evaluate(() => localStorage.getItem('accessToken'));
  if (!token) throw new Error('Expected a console session');
  return token;
}

/** Store only genuine token pairs issued by the running backend. */
async function store(page: Page, tokens: Tokens): Promise<void> {
  await page.evaluate((data) => {
    localStorage.setItem('accessToken', data.access_token);
    localStorage.setItem('refreshToken', data.refresh_token);
  }, tokens);
}

/** Call an actual authentication route with the chosen session or challenge. */
async function request(
  page: Page,
  path: string,
  method = 'GET',
  body?: object,
  token?: string
): Promise<APIResponse> {
  const bearer = token ?? (await session(page));
  return page.request.fetch('/api/v1' + path, {
    method,
    ...(body === undefined ? {} : { data: body }),
    headers: bearer ? { Authorization: `Bearer ${bearer}` } : {},
  });
}

/** Attach browser evidence with one-time material masked even on failure. */
async function evidence(
  page: Page,
  info: TestInfo,
  name: string
): Promise<void> {
  const path = info.outputPath(name + '.png');
  await page.screenshot({
    path,
    fullPage: true,
    mask: [
      page.locator('sl-input input'),
      page.locator('mfa-card code'),
      page.locator('mfa-card img'),
      page.locator('.manual-code-box'),
    ],
  });
  await info.attach(name, {
    path,
    contentType: 'image/png',
  });
}

/** Find captured mail without returning credentials to Playwright assertions. */
function mail(user: User, subject: RegExp): Mail | undefined {
  const outbox = join(directory!, 'outbox');
  return readdirSync(outbox)
    .filter((name) => name.endsWith('.json'))
    .map((name) => JSON.parse(readFileSync(join(outbox, name), 'utf8')) as Mail)
    .find((item) => item.to === user.email && subject.test(item.subject));
}

/** Follow a local captured verification/reset link through the console. */
async function followMail(page: Page, user: User, kind: string): Promise<void> {
  const subject = kind === 'verify-email' ? /verif/i : /reset/i;
  await expect.poll(() => !!mail(user, subject)).toBe(true);
  const link = mail(user, subject)!.text.match(
    new RegExp(`https?://[^\\s<>"']+/${kind}\\?token=[^\\s<>"']+`)
  );
  if (!link) throw new Error('Expected a bound local email link');
  await page.goto('/' + kind + new URL(link[0]).search);
}

/** Create and verify a unique password account entirely through the UI. */
async function register(page: Page): Promise<User> {
  const username = 'mfa' + randomUUID().replaceAll('-', '');
  const user = {
    username,
    email: username + '@example.com',
    password: 'Synthetic-browser-password-42!',
  };
  await page.goto('/register');
  for (const name of ['username', 'email', 'password'] as const) {
    await page.locator(`sl-input[name="${name}"] input`).fill(user[name]);
  }
  const response = page.waitForResponse('**/auth/register');
  await page
    .getByRole('button', { name: 'Create account', exact: true })
    .click();
  expect((await response).status()).toBe(201);
  await followMail(page, user, 'verify-email');
  await page
    .getByRole('link', { name: 'Go to the console', exact: true })
    .click();
  await page.waitForURL('**/console**');
  return user;
}

/** Use the console's actual sign-out control. */
async function signOut(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'User Menu', exact: true }).click();
  // auth-change can briefly route to /login before the awaited server logout
  // finishes and assigns /. Wait for that final document navigation.
  await Promise.all([
    page.waitForNavigation({
      url: (url) => url.pathname === '/',
      waitUntil: 'load',
    }),
    page.getByRole('menuitem', { name: 'Sign out', exact: true }).click(),
  ]);
  expect(await page.evaluate(() => !!localStorage.getItem('accessToken'))).toBe(
    false
  );
}

/** Submit the password and demand the real second-factor form. */
async function password(page: Page, user: User): Promise<void> {
  await page.goto('/login');
  await page.locator('sl-input[name="username"] input').fill(user.username);
  await page.locator('sl-input[name="password"] input').fill(user.password);
  await page.locator('sl-button[type="submit"]').click();
  await expect(
    page.locator('sl-input[label="Authenticator code"]')
  ).toBeVisible();
  expect(await page.evaluate(() => !!localStorage.getItem('accessToken'))).toBe(
    false
  );
}

/** Submit either proof through the browser, leaving failures on the form. */
async function verify(
  page: Page,
  proof: string,
  recovery = false
): Promise<Response> {
  if (recovery) {
    await page
      .getByRole('button', { name: 'Use a recovery code', exact: true })
      .click();
  }
  await page.locator('login-view sl-input input').fill(proof);
  const response = page.waitForResponse('**/auth/mfa/verify');
  await page.getByRole('button', { name: 'Verify', exact: true }).click();
  return response;
}

/** Acknowledge one-time recovery material using the actual native checkbox. */
async function saved(page: Page): Promise<void> {
  const card = page.locator('mfa-card');
  await card.locator('sl-checkbox input').check({ force: true });
  await card.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(card).toContainText('10 recovery codes remaining');
}

/** Enroll from the displayed secret, then retain the ten private recovery codes. */
async function enroll(page: Page, info: TestInfo): Promise<Enrollment> {
  const oldSession = await session(page);
  await page.goto('/console/settings/security');
  const card = page.locator('mfa-card');
  await card
    .getByRole('button', { name: 'Set up authenticator app', exact: true })
    .click();
  await expect(card.locator('img')).toBeVisible();
  expect(
    (await card.locator('img').getAttribute('src'))?.startsWith('data:image/')
  ).toBe(true);
  const secret = (await card.locator('code').textContent())!.trim();
  await evidence(page, info, 'enrollment-secret-and-local-qr');
  await card.locator('sl-input input').fill(code(secret, false));
  await card.getByRole('button', { name: 'Confirm code', exact: true }).click();
  await expect(card.locator('li code')).toHaveCount(10);
  const recovery = await card.locator('li code').allTextContents();
  await evidence(page, info, 'one-time-recovery-codes');
  await saved(page);
  expect(
    (
      await request(page, '/auth/users/me', 'GET', undefined, oldSession)
    ).status()
  ).toBe(401);
  return { secret, recovery, oldSession };
}

/** Start an independent signed challenge without replacing the browser session. */
async function challenge(
  page: Page,
  user: User,
  form = false
): Promise<string> {
  const response = form
    ? await page.request.post('/api/v1/auth/token', { form: user })
    : await request(page, '/auth/token/json', 'POST', user, '');
  expect(response.status()).toBe(401);
  const body = await response.json();
  expect(body.detail.code).toBe('mfa_required');
  expect('access_token' in body).toBe(false);
  return body.detail.challenge_token;
}

/** Register one passkey on a fresh software authenticator with verified UV. */
async function addPasskey(page: Page, context: BrowserContext): Promise<void> {
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  const response = page.waitForResponse('**/webauthn/register/verify');
  await page.getByRole('button', { name: 'Add passkey', exact: true }).click();
  expect((await response).status()).toBe(200);
  await cdp.detach();
}

/** Execute an existing operator reset with only synthetic identity arguments. */
function operator(script: string, user: User): void {
  const result = spawnSync(
    python,
    [
      'scripts/' + script,
      '--email',
      user.email,
      '--reason',
      'Synthetic lost-device recovery',
    ],
    { cwd: root, env: childEnv, encoding: 'utf8' }
  );
  if (result.status !== 0) throw helperFailure('The operator reset', result);
}

/** Complete the real headless CLI OAuth flow in a private scratch HOME. */
async function cliLogin(page: Page, user: User, secret: string): Promise<void> {
  const cliEnv = {
    ...childEnv,
    HOME: join(directory!, 'cli-home'),
    PRELOOP_URL: new URL(page.url()).origin,
    PRELOOP_NO_RUNNER: '1',
  };
  // A non-TTY invocation prints the URL and refuses to prompt. The supported
  // --code continuation exchanges the code after the browser completes MFA.
  const started = spawnSync(cli, ['login', '--headless', '--no-runner'], {
    env: cliEnv,
    encoding: 'utf8',
  });
  expect(started.status).toBe(1);
  const url = started.stdout.match(
    /http:\/\/localhost:\d+\/oauth\/authorize\?[^\s]+/
  );
  if (!url)
    throw new Error('The local CLI did not return an authorization URL');
  await page.goto(url[0]);
  await page.locator('sl-input[name="username"] input').fill(user.username);
  await page.locator('sl-input[name="password"] input').fill(user.password);
  await page.locator('sl-button[type="submit"]').click();
  await expect(
    page.locator('sl-input[label="Authenticator code"]')
  ).toBeVisible();
  expect((await verify(page, code(secret))).status()).toBe(200);
  await page.waitForURL('**/console/authorize**');
  await page
    .getByRole('button', { name: 'Authorize Application', exact: true })
    .click();
  await expect(page.locator('.manual-code-box')).toBeVisible();
  const authorizationCode = (await page
    .locator('.manual-code-box')
    .textContent())!.trim();
  const finished = spawnSync(
    cli,
    ['login', '--headless', '--no-runner', '--code', authorizationCode],
    { env: cliEnv, encoding: 'utf8' }
  );
  expect(finished.status).toBe(0);
}

test.use({ trace: 'off', video: 'off', screenshot: 'off' });

test.describe('local TOTP sign-in', () => {
  test.describe.configure({ mode: 'serial' });
  test.skip(!directory, 'Requires the isolated totp_server clock and outbox');

  test.beforeEach(async ({ context, baseURL }) => {
    // Keep the ten-failure scenario away from an incidental hour boundary.
    // The clock remains monotonic across tests, including next-hour recovery.
    const now = Number(readFileSync(join(directory!, 'clock'), 'utf8'));
    advance((Math.floor(now / 3600) + 1) * 3600 + 300 - now);
    for (const url of [baseURL!, apiOrigin]) {
      if (
        !['localhost', '127.0.0.1', '[::1]'].includes(new URL(url).hostname)
      ) {
        throw new Error('This suite only runs against a local stack');
      }
    }
    await context.route('**/*', async (route) => {
      const host = new URL(route.request().url()).hostname;
      if (['localhost', '127.0.0.1', '[::1]'].includes(host))
        await route.continue();
      else await route.abort();
    });
  });

  test.afterEach(async ({ page }, info) => {
    await evidence(page, info, 'final-browser-state');
  });

  // protects=the complete browser journey and token boundaries;
  // fails_when=OTP/recovery is skipped, replayed or unaudited; no_claim=live IdPs.
  test('enroll, sign in, recover, step up, reset password and disable', async ({
    page,
    context,
  }, info) => {
    test.setTimeout(180_000);
    const user = await register(page);
    const { secret, recovery, oldSession } = await enroll(page, info);
    await signOut(page);
    await password(page, user);
    expect((await verify(page, code(secret, false, true))).status()).toBe(401);
    await expect(page.getByRole('alert')).toContainText(
      'invalid or has already been used'
    );
    const used = code(secret);
    expect((await verify(page, used)).status()).toBe(200);
    await page.waitForURL('**/console**');
    await evidence(page, info, 'password-plus-totp');
    await signOut(page);
    await password(page, user);
    expect((await verify(page, used)).status()).toBe(401);
    expect((await verify(page, recovery[0], true)).status()).toBe(200);
    await page.waitForURL('**/console**');
    await signOut(page);
    await password(page, user);
    expect((await verify(page, recovery[0], true)).status()).toBe(401);
    await page
      .getByRole('button', { name: 'Use an authenticator code', exact: true })
      .click();
    expect((await verify(page, code(secret))).status()).toBe(200);
    await page.waitForURL('**/console**');

    await page.goto('/console/settings/security');
    const card = page.locator('mfa-card');
    await card.locator('sl-input input').fill(code(secret));
    await card
      .getByRole('button', { name: 'Replace recovery codes', exact: true })
      .click();
    await page
      .locator('sl-dialog[open]')
      .getByRole('button', { name: 'Replace codes', exact: true })
      .click();
    await expect(card.locator('li code')).toHaveCount(10);
    const replaced = await card.locator('li code').allTextContents();
    expect(replaced.some((value) => recovery.includes(value))).toBe(false);
    await saved(page);
    const oldCode = await request(
      page,
      '/auth/mfa/verify',
      'POST',
      {
        challenge_token: await challenge(page, user),
        recovery_code: recovery[1],
      },
      ''
    );
    expect(oldCode.status()).toBe(401);
    const newCode = await request(
      page,
      '/auth/mfa/verify',
      'POST',
      {
        challenge_token: await challenge(page, user),
        recovery_code: replaced[0],
      },
      ''
    );
    expect(newCode.status()).toBe(200);
    await store(page, await newCode.json());

    await addPasskey(page, context);
    const keys = await (
      await request(page, '/auth/webauthn/credentials')
    ).json();
    const stale = await session(page);
    advance(601);
    for (const [path, method, body] of [
      ['/auth/webauthn/credentials/' + keys[0].id, 'DELETE', undefined],
      ['/auth/webauthn/passkey-only', 'PUT', { enabled: false }],
      ['/auth/webauthn/register/options', 'POST', {}],
    ] as const) {
      const refused = await request(page, path, method, body, stale);
      expect(refused.status()).toBe(401);
      expect((await refused.json()).detail.code).toBe('step_up_required');
    }
    await page.reload();
    await page.getByRole('button', { name: 'Remove', exact: true }).click();
    const dialog = page.locator('step-up-dialog');
    await expect(dialog.locator('sl-input')).toBeVisible();
    await evidence(page, info, 'stale-session-step-up');
    await dialog.locator('sl-input input').fill(code(secret));
    await dialog
      .getByRole('button', { name: 'Verify code', exact: true })
      .click();
    await expect(
      page.getByText('Passkey removed.', { exact: true })
    ).toBeVisible();
    expect(
      (
        await request(page, '/auth/webauthn/passkey-only', 'PUT', {
          enabled: false,
        })
      ).status()
    ).toBe(200);

    for (const form of [false, true]) {
      const pending = await challenge(page, user, form);
      expect(
        (
          await request(page, '/auth/users/me', 'GET', undefined, pending)
        ).status()
      ).toBe(401);
      expect(
        (
          await request(
            page,
            '/auth/refresh',
            'POST',
            { refresh_token: pending },
            ''
          )
        ).status()
      ).toBe(401);
      expect(
        (
          await request(
            page,
            '/auth/runtime-sessions/token',
            'POST',
            {
              session_source_type: 'cli',
              session_source_id: 'synthetic-browser-session',
            },
            pending
          )
        ).status()
      ).toBe(401);
    }
    expect(
      (
        await request(page, '/auth/users/me', 'GET', undefined, oldSession)
      ).status()
    ).toBe(401);
    const refresh = await page.evaluate(() =>
      localStorage.getItem('refreshToken')
    );
    expect(
      (
        await request(page, '/auth/refresh', 'POST', { refresh_token: refresh })
      ).status()
    ).toBe(200);
    const emailToken = JSON.parse(
      driver('email', { email: user.email })
    ) as string;
    const emailLogin = await request(
      page,
      '/auth/verify-email',
      'POST',
      { token: emailToken },
      ''
    );
    expect(emailLogin.status()).toBe(401);
    const emailSession = await request(
      page,
      '/auth/mfa/verify',
      'POST',
      {
        challenge_token: (await emailLogin.json()).detail.challenge_token,
        code: code(secret),
      },
      ''
    );
    expect(emailSession.status()).toBe(200);
    await store(page, await emailSession.json());

    const issued = await request(page, '/auth/api-keys', 'POST', {
      name: 'Synthetic MFA key',
    });
    expect(issued.status()).toBe(201);
    const apiKey = (await issued.json()).key as string;
    expect(
      (await request(page, '/auth/users/me', 'GET', undefined, apiKey)).status()
    ).toBe(200);
    expect(
      (
        await request(
          page,
          '/auth/mfa/step-up',
          'POST',
          { code: code(secret, false) },
          apiKey
        )
      ).status()
      // A valid key is the wrong kind of credential: 403, not "invalid" 401.
    ).toBe(403);
    const approval = JSON.parse(driver('approval', { email: user.email })) as {
      id: string;
      token: string;
    };
    const publicURL = `${apiOrigin}/approval/${approval.id}`;
    expect(
      (
        await page.request.get(publicURL + '/data', {
          params: { token: 'invalid' },
        })
      ).status()
    ).toBe(404);
    expect(
      (
        await page.request.get(publicURL + '/data', {
          params: { token: approval.token },
        })
      ).status()
    ).toBe(200);
    expect(
      (
        await page.request.post(publicURL + '/approve', {
          params: { token: approval.token },
          data: {},
        })
      ).status()
    ).toBe(200);

    const me = await (await request(page, '/auth/users/me')).json();
    const member = {
      username: 'member' + randomUUID().replaceAll('-', ''),
      email: 'member' + randomUUID() + '@example.com',
      password: user.password,
    };
    expect((await request(page, '/users', 'POST', member)).status()).toBe(201);
    const memberLogin = await request(
      page,
      '/auth/token/json',
      'POST',
      member,
      ''
    );
    expect(memberLogin.status()).toBe(200);
    const memberToken = (await memberLogin.json()).access_token as string;
    expect(
      (
        await request(
          page,
          '/auth/mfa/disable',
          'POST',
          { user_id: me.id, code: code(secret, false) },
          memberToken
        )
      ).status()
    ).toBe(409);
    expect(
      (
        await request(
          page,
          `/users/${me.id}/mfa/reset`,
          'POST',
          {},
          memberToken
        )
      ).status()
    ).toBe(404);
    expect(
      (await (await request(page, '/auth/mfa/status')).json()).totp_enabled
    ).toBe(true);

    await signOut(page);
    await cliLogin(page, user, secret);
    await evidence(page, info, 'cli-browser-consent-completed');
    await page.goto('/forgot-password');
    await page.locator('sl-input input').fill(user.email);
    const forgot = page.waitForResponse('**/auth/forgot-password');
    await page.locator('sl-button[type="submit"]').click();
    expect((await forgot).status()).toBe(200);
    await followMail(page, user, 'reset-password');
    user.password = 'Synthetic-reset-password-43!';
    for (const input of await page.locator('sl-input input').all())
      await input.fill(user.password);
    const reset = page.waitForResponse('**/auth/reset-password');
    await page.locator('sl-button[type="submit"]').click();
    const resetResult = await reset;
    expect(resetResult.status()).toBe(200);
    expect('access_token' in (await resetResult.json())).toBe(false);
    await page.evaluate(() => localStorage.clear());
    await password(page, user);
    expect((await verify(page, code(secret))).status()).toBe(200);
    await page.waitForURL('**/console**');

    await page.goto('/console/settings/security');
    expect(
      (await request(page, '/auth/mfa/disable', 'POST', {})).status()
    ).toBe(422);
    expect(
      (await request(page, '/auth/mfa/recovery-codes', 'POST', {})).status()
    ).toBe(422);
    await card.locator('sl-input input').fill(code(secret));
    await card.getByRole('button', { name: 'Turn off', exact: true }).click();
    await page
      .locator('sl-dialog[open]')
      .getByRole('button', { name: 'Turn off', exact: true })
      .click();
    await expect(card).toContainText('Authenticator app turned off.');
    expect(await (await request(page, '/auth/mfa/status')).json()).toEqual({
      totp_enabled: false,
      totp_needs_setup: false,
      recovery_codes_remaining: 0,
    });
    await signOut(page);
    await page.goto('/login');
    await page.locator('sl-input[name="username"] input').fill(user.username);
    await page.locator('sl-input[name="password"] input').fill(user.password);
    await page.locator('sl-button[type="submit"]').click();
    await page.waitForURL('**/console**');
    const audit = await (await request(page, '/audit-logs')).json();
    const actions = audit.logs.map(
      (row: { action: string }) => row.action
    ) as string[];
    for (const action of [
      'auth.mfa.enrolled',
      'auth.mfa.verify_failed',
      'auth.mfa.recovery_code_used',
      'auth.mfa.recovery_codes_regenerated',
      'auth.mfa.disabled',
      'auth.login.succeeded',
    ])
      expect(actions).toContain(action);
    const serialized = JSON.stringify(audit);
    expect(
      [secret, ...recovery, ...replaced].some((proof) =>
        serialized.includes(proof)
      )
    ).toBe(false);
  });

  // protects=shared challenge/user budgets, fixed-hour recovery and operator reset;
  // fails_when=correct/recovery proofs bypass a lock; no_claim=distributed outage.
  test('challenge lock, hourly user lock and audited operator recovery', async ({
    page,
  }, info) => {
    test.setTimeout(120_000);
    const user = await register(page);
    const { secret, recovery } = await enroll(page, info);
    for (let round = 0; round < 2; round++) {
      const pending = await challenge(page, user);
      for (let attempt = 0; attempt < 5; attempt++) {
        const bad = await request(
          page,
          '/auth/mfa/verify',
          'POST',
          { challenge_token: pending, code: code(secret, false, true) },
          ''
        );
        expect(bad.status()).toBe(401);
        expect((await bad.json()).detail.code).toBe('invalid_code');
      }
      const locked = await request(
        page,
        '/auth/mfa/verify',
        'POST',
        { challenge_token: pending, code: code(secret) },
        ''
      );
      expect(locked.status()).toBe(429);
      expect((await locked.json()).detail.code).toBe('challenge_locked');
      if (round === 0) {
        const restored = await request(
          page,
          '/auth/mfa/verify',
          'POST',
          { challenge_token: await challenge(page, user), code: code(secret) },
          ''
        );
        expect(restored.status()).toBe(200);
      }
    }
    const recoveryLocked = await request(
      page,
      '/auth/mfa/verify',
      'POST',
      {
        challenge_token: await challenge(page, user),
        recovery_code: recovery[0],
      },
      ''
    );
    expect(recoveryLocked.status()).toBe(429);
    // The hourly lock covers recovery codes too and says when it ends.
    const lockedDetail = (await recoveryLocked.json()).detail;
    expect(lockedDetail.code).toBe('mfa_user_locked');
    expect(lockedDetail.retry_after_seconds).toBeGreaterThan(0);
    expect(recoveryLocked.headers()['retry-after']).toBe(
      String(lockedDetail.retry_after_seconds)
    );
    const now = Number(readFileSync(join(directory!, 'clock'), 'utf8'));
    advance((Math.floor(now / 3600) + 1) * 3600 - now);
    const restored = await request(
      page,
      '/auth/mfa/verify',
      'POST',
      { challenge_token: await challenge(page, user), code: code(secret) },
      ''
    );
    expect(restored.status()).toBe(200);
    await store(page, await restored.json());
    const ended = await session(page);
    operator('reset_mfa.py', user);
    expect(
      (await request(page, '/auth/users/me', 'GET', undefined, ended)).status()
    ).toBe(401);
    const login = await request(page, '/auth/token/json', 'POST', user, '');
    expect(login.status()).toBe(200);
    await store(page, await login.json());
    const audit = await (await request(page, '/audit-logs')).json();
    const counts = (action: string): number =>
      audit.logs.filter((row: { action: string }) => row.action === action)
        .length;
    expect(counts('auth.mfa.verify_failed')).toBe(10);
    expect(counts('auth.mfa.challenge_locked')).toBe(2);
    expect(counts('auth.mfa.reset')).toBe(1);
    expect(await (await request(page, '/auth/mfa/status')).json()).toEqual({
      totp_enabled: false,
      totp_needs_setup: false,
      recovery_codes_remaining: 0,
    });
  });

  // protects=the documented current-session promise when TOTP confirms the switch;
  // fails_when=the returned pair is immediately refused; no_claim=physical keys.
  // protects=a passkey signs in through the console and an assertion without
  // user verification creates no session; fails_when=the ceremony never
  // completes or an unverified assertion signs in; no_claim=hardware keys.
  test('passkey sign-in, and refusal without user verification', async ({
    page,
    context,
  }, info) => {
    test.setTimeout(90_000);
    await register(page);
    const cdp = await context.newCDPSession(page);
    await cdp.send('WebAuthn.enable');
    const { authenticatorId } = await cdp.send(
      'WebAuthn.addVirtualAuthenticator',
      {
        options: {
          protocol: 'ctap2',
          transport: 'internal',
          hasResidentKey: true,
          hasUserVerification: true,
          isUserVerified: true,
          automaticPresenceSimulation: true,
        },
      }
    );
    await page.goto('/console/settings/security');
    const registered = page.waitForResponse('**/webauthn/register/verify');
    await page
      .getByRole('button', { name: 'Add passkey', exact: true })
      .click();
    expect((await registered).status()).toBe(200);
    await signOut(page);

    await page.goto('/login');
    const signedIn = page.waitForResponse('**/webauthn/authenticate/verify');
    await page
      .getByRole('button', { name: 'Sign in with passkey', exact: true })
      .click();
    expect((await signedIn).status()).toBe(200);
    await page.waitForURL('**/console**');
    await evidence(page, info, 'passkey-sign-in');
    await signOut(page);

    await cdp.send('WebAuthn.setUserVerified', {
      authenticatorId,
      isUserVerified: false,
    });
    await page.goto('/login');
    const verifyCalls: number[] = [];
    page.on('response', (response) => {
      if (response.url().includes('/webauthn/authenticate/verify'))
        verifyCalls.push(response.status());
    });
    await page
      .getByRole('button', { name: 'Sign in with passkey', exact: true })
      .click();
    // The browser refuses a required-UV ceremony, or the server answers
    // passkey_user_verification_required; either way no session exists.
    await expect(page.getByRole('alert')).toBeVisible();
    expect(verifyCalls.every((status) => status !== 200)).toBe(true);
    expect(
      await page.evaluate(() => !!localStorage.getItem('accessToken'))
    ).toBe(false);
    await cdp.detach();
  });

  test('passkey-only enabled with TOTP keeps the current session usable', async ({
    page,
    context,
  }, info) => {
    test.setTimeout(90_000);
    const user = await register(page);
    await enroll(page, info);
    await addPasskey(page, context);
    let currentStatus = 0;
    try {
      const enabled = await request(
        page,
        '/auth/webauthn/passkey-only',
        'PUT',
        { enabled: true }
      );
      expect(enabled.status()).toBe(200);
      const tokens = (await enabled.json()) as Tokens;
      const current = await request(
        page,
        '/auth/users/me',
        'GET',
        undefined,
        tokens.access_token
      );
      currentStatus = current.status();
    } finally {
      operator('reset_passkey_only.py', user);
    }
    expect(currentStatus).toBe(200);
  });
});
