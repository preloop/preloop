/**
 * T10 E2E: custom-agent onboarding + per-run session correctness.
 *
 * This spec walks the "Connect a custom agent" onboarding flow end to end and
 * proves the headline correctness property of the per-run session fix: two
 * gateway requests sharing one minted credential but carrying DIFFERENT
 * `X-Preloop-Session-Id` values produce TWO distinct runtime sessions, while
 * two requests with the SAME header collapse into ONE.
 *
 * It runs in two modes, selected by the Playwright project:
 *
 *   e2e-ci      (full assertion, fake upstream)
 *   e2e-staging (UI-only smoke against https://staging.preloop.ai)
 *
 * ---------------------------------------------------------------------------
 * HOW TO RUN — CI (full flow + two-runs-two-sessions assertion)
 * ---------------------------------------------------------------------------
 * The CI mode needs four things running locally:
 *
 *   1. Postgres (the backend's DATABASE_URL target).
 *   2. The fake upstream model server (so the gateway's litellm call resolves
 *      without a real provider key):
 *        source preloop/.venv/bin/activate
 *        python -m tests.e2e_support.fake_upstream --port 8081
 *      (run from preloop/backend)
 *   3. The backend, seeded with the admin test account:
 *        cd preloop/backend
 *        INIT_TEST_DATA=true TESTING=true python -m preloop.server   # :8000
 *      then seed the gateway-enabled fake model:
 *        PRELOOP_E2E_FAKE_UPSTREAM=http://127.0.0.1:8081/v1 \
 *          python -m tests.e2e_support.seed_gateway_model
 *   4. The frontend dev server is started automatically by Playwright's
 *      `webServer` (npm run dev on :5173, proxying /api + /openai to :8000).
 *
 * Then:
 *        cd preloop/frontend
 *        npm run test:e2e:ci
 *      (== npx playwright test --project=e2e-ci)
 *
 * Default test credentials come from INIT_TEST_DATA: admin / admin. Override
 * with PRELOOP_E2E_USERNAME / PRELOOP_E2E_PASSWORD if needed. The gateway model
 * alias the spec sends is `preloop-fake` (see seed_gateway_model.py).
 *
 * ---------------------------------------------------------------------------
 * HOW TO RUN — STAGING smoke (pre-demo; real backend, no fake upstream)
 * ---------------------------------------------------------------------------
 * Put staging creds in ./tmp/staging-creds.json (NOT committed):
 *        { "username": "<user>", "password": "<pass>" }
 * or export PRELOOP_E2E_USERNAME / PRELOOP_E2E_PASSWORD. Then:
 *        cd preloop/frontend
 *        npm run test:e2e:staging
 *      (== PRELOOP_E2E_TARGET=staging npx playwright test --project=e2e-staging)
 *
 * The staging run exercises the UI flow and asserts a credential is minted and
 * the result screen renders the gateway base URL + session-id snippet. It does
 * NOT script gateway traffic against the real provider (no fake upstream), and
 * skips the two-sessions assertion.
 */

import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
} from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const IS_STAGING = process.env.PRELOOP_E2E_TARGET === 'staging';

// Client-supplied per-run session header. MUST match the gateway + wizard.
const SESSION_HEADER = 'X-Preloop-Session-Id';
// Gateway model alias seeded by tests/e2e_support/seed_gateway_model.py.
const GATEWAY_MODEL = process.env.PRELOOP_E2E_MODEL || 'preloop-fake';

interface Creds {
  username: string;
  password: string;
}

/** Resolve login creds from env, then ./tmp/staging-creds.json, then defaults. */
function resolveCreds(): Creds {
  const envUser = process.env.PRELOOP_E2E_USERNAME;
  const envPass = process.env.PRELOOP_E2E_PASSWORD;
  if (envUser && envPass) {
    return { username: envUser, password: envPass };
  }
  const credsPath = path.resolve(process.cwd(), 'tmp', 'staging-creds.json');
  if (fs.existsSync(credsPath)) {
    const raw = JSON.parse(fs.readFileSync(credsPath, 'utf-8'));
    if (raw.username && raw.password) {
      return { username: raw.username, password: raw.password };
    }
  }
  if (IS_STAGING) {
    throw new Error(
      'Staging creds not found. Set PRELOOP_E2E_USERNAME/PASSWORD or create ' +
        './tmp/staging-creds.json with {username, password}.'
    );
  }
  // CI default: the INIT_TEST_DATA admin account.
  return { username: 'admin', password: 'admin' };
}

/** Log in through the real /login UI and land on /console. */
async function login(page: Page, creds: Creds): Promise<void> {
  await page.goto('/login');
  // Shoelace <sl-input> exposes a focusable input; fill by label/name.
  const username = page.locator('sl-input[name="username"]');
  const password = page.locator('sl-input[name="password"]');
  await username.waitFor({ state: 'visible' });
  await username.click();
  await page.keyboard.type(creds.username);
  await password.click();
  await page.keyboard.type(creds.password);
  await page.locator('sl-button[type="submit"]').click();
  // Auth stores the token and routes to /console.
  await page.waitForURL(/\/console/, { timeout: 30_000 });
  await expect
    .poll(async () => page.evaluate(() => localStorage.getItem('accessToken')), {
      timeout: 30_000,
    })
    .not.toBeNull();
}

/**
 * Read the `.value` of the <sl-copy-button> in the labelled `.command-step`
 * containing `labelSubstring`. The result screen renders three labelled steps
 * (base URL, credential, snippet), each mirroring its exact string onto a copy
 * button. Keying off the visible label is robust to step reordering, unlike
 * positional `.nth()` indexing. Playwright's CSS engine pierces the wizard's
 * open shadow root, so `.command-step` resolves inside it.
 */
async function readCopyValueByLabel(
  wizard: Locator,
  labelSubstring: string
): Promise<string> {
  const step = wizard
    .locator('.command-step')
    .filter({ hasText: labelSubstring })
    .first();
  await step.waitFor({ state: 'attached', timeout: 15_000 });
  return step
    .locator('sl-copy-button')
    .first()
    .evaluate((el) => (el as HTMLElement & { value: string }).value);
}

/**
 * Open the deploy wizard, take the "Connect a custom agent" path, register the
 * agent + mint a credential, and return the gateway base URL, minted token, and
 * copy-paste snippet read off the result screen. Also asserts the result screen
 * is internally consistent: base URL is the OpenAI-compatible gateway on THIS
 * origin, the credential is surfaced with a shown-once warning, and the snippet
 * wires the exact base URL + per-run session header.
 */
async function runCustomAgentWizard(
  page: Page,
  agentName: string
): Promise<{ baseUrl: string; token: string; snippet: string }> {
  // Navigate to the agents view, which hosts the deploy wizard dialog.
  await page.goto('/console/agents');

  const wizard = page.locator('preloop-deploy-wizard');

  // The wizard auto-opens for a fresh (agent-less) account; otherwise click the
  // "Deploy Agent" trigger to open the onboarding dialog. Try the trigger but
  // tolerate the auto-open case.
  const trigger = page.getByRole('button', { name: /Deploy Agent/i });
  if ((await wizard.count()) === 0) {
    await trigger.first().click().catch(() => undefined);
  }
  await wizard.first().waitFor({ state: 'attached', timeout: 30_000 });

  // The wizard renders inside shadow DOM. Playwright pierces shadow roots for
  // text/role queries, so we drive it via getByText within the wizard.
  await wizard
    .getByText('Connect a custom agent', { exact: true })
    .first()
    .click();

  // Name form: type into the Shoelace name input.
  const nameInput = wizard.locator('sl-input[name="display_name"]');
  await nameInput.waitFor({ state: 'visible', timeout: 15_000 });
  await nameInput.click();
  await page.keyboard.type(agentName);

  // Register + mint.
  await wizard
    .getByRole('button', { name: /Register agent .* mint credential/i })
    .click();

  // Result screen: "Your agent is connected" + base URL + token + snippet.
  await expect(
    wizard.getByText('Your agent is connected')
  ).toBeVisible({ timeout: 30_000 });

  // The credential is surfaced exactly once with an explicit shown-once warning.
  await expect(wizard.getByText(/shown only once/i).first()).toBeVisible();

  // Read the exact values off each labelled step's copy button (robust to
  // ordering). See renderCustomResultState() in preloop-deploy-wizard.ts.
  // Use fully-qualified label text: the snippet body also mentions "gateway
  // credential" in a comment, so the parenthesised form keys the credential
  // step unambiguously.
  const baseUrl = await readCopyValueByLabel(wizard, 'Gateway base URL');
  const token = await readCopyValueByLabel(wizard, 'Gateway credential (api_key)');
  const snippet = await readCopyValueByLabel(wizard, 'per-run session id');

  // The gateway base URL must be the OpenAI-compatible endpoint on THIS origin
  // (dev: http://localhost:5173, staging: https://staging.preloop.ai). This is
  // exactly how buildGatewayBaseUrl() derives it from window.location.origin.
  const pageOrigin = await page.evaluate(() => window.location.origin);
  expect(baseUrl).toBe(`${pageOrigin}/openai/v1`);
  expect(token).toBeTruthy();

  // The copy-paste snippet must wire the exact base URL AND reference the
  // per-run session header the gateway reads, so a user pasting it gets working
  // per-run session attribution out of the box.
  expect(snippet).toContain(`base_url="${baseUrl}"`);
  expect(snippet).toContain(SESSION_HEADER);

  return { baseUrl, token, snippet };
}

/** Fire one gateway chat-completion using the minted token + a session id. */
async function gatewayChat(
  request: APIRequestContext,
  baseUrl: string,
  token: string,
  sessionId: string
): Promise<number> {
  const resp = await request.post(`${baseUrl}/chat/completions`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      [SESSION_HEADER]: sessionId,
    },
    data: {
      model: GATEWAY_MODEL,
      messages: [{ role: 'user', content: 'Hello from the T10 E2E.' }],
    },
  });
  return resp.status();
}

/**
 * Count distinct runtime sessions for this account whose principal id starts
 * with the custom agent's source id. The per-run fix keys sessions as
 * `{principal_id}:{client_session_id}`, so distinct headers => distinct rows.
 */
async function listSessions(
  request: APIRequestContext,
  accessToken: string,
  origin: string
): Promise<Array<{ id: string; runtime_principal_id: string | null; session_source_id: string }>> {
  const resp = await request.get(
    `${origin}/api/v1/runtime-sessions?session_source_type=custom&limit=100`,
    { headers: { Authorization: `Bearer ${accessToken}` } }
  );
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  return body.items as Array<{
    id: string;
    runtime_principal_id: string | null;
    session_source_id: string;
  }>;
}

test.describe('Custom-agent onboarding + per-run session', () => {
  test('walks the wizard and mints a gateway credential', async ({ page }) => {
    const creds = resolveCreds();
    await login(page, creds);
    const agentName = `E2E Custom Agent ${Date.now()}`;
    const { baseUrl, token, snippet } = await runCustomAgentWizard(
      page,
      agentName
    );

    // Origin match + snippet wiring are asserted inside runCustomAgentWizard for
    // both CI and staging. Re-assert the headline surface here so this smoke
    // test fails loudly if the result screen stops exposing them.
    expect(baseUrl.endsWith('/openai/v1')).toBeTruthy();
    expect(token.length).toBeGreaterThan(8);
    expect(snippet).toContain(SESSION_HEADER);
    // The header must also be visible on-screen (not just in the copied value).
    await expect(
      page
        .locator('preloop-deploy-wizard')
        .getByText(SESSION_HEADER, { exact: false })
        .first()
    ).toBeVisible();
  });

  test('two distinct session ids produce two sessions; same id reuses one', async ({
    page,
    request,
  }) => {
    test.skip(
      IS_STAGING,
      'Staging has no fake upstream; gateway traffic + session assertion is CI-only.'
    );

    const creds = resolveCreds();
    await login(page, creds);

    const agentName = `E2E Session Agent ${Date.now()}`;
    const { baseUrl, token } = await runCustomAgentWizard(page, agentName);

    const origin = new URL(baseUrl).origin;
    const accessToken = (await page.evaluate(() =>
      localStorage.getItem('accessToken')
    )) as string;
    expect(accessToken).toBeTruthy();

    // Unique per-run ids so this test is isolated from prior runs / agents.
    const stamp = Date.now();
    const runA = `e2e-run-a-${stamp}`;
    const runB = `e2e-run-b-${stamp}`;

    // Two requests with the SAME session id -> should collapse to ONE session.
    expect(await gatewayChat(request, baseUrl, token, runA)).toBe(200);
    expect(await gatewayChat(request, baseUrl, token, runA)).toBe(200);
    // One request with a DIFFERENT session id -> a SECOND distinct session.
    expect(await gatewayChat(request, baseUrl, token, runB)).toBe(200);

    // The runtime session principal id is `{agent_source_id}:{client_session_id}`.
    // Find the two principal ids we expect and assert exactly two distinct
    // sessions appeared for THIS run pair.
    await expect
      .poll(
        async () => {
          const sessions = await listSessions(request, accessToken, origin);
          const principals = new Set(
            sessions
              .map((s) => s.runtime_principal_id || s.session_source_id || '')
              .filter((p) => p.endsWith(`:${runA}`) || p.endsWith(`:${runB}`))
          );
          return principals.size;
        },
        { timeout: 30_000, intervals: [500, 1000, 2000] }
      )
      .toBe(2);

    // And the SAME-id pair must not have created a duplicate: exactly one
    // session ends in `:runA`.
    const sessions = await listSessions(request, accessToken, origin);
    const runASessions = sessions.filter((s) =>
      (s.runtime_principal_id || s.session_source_id || '').endsWith(`:${runA}`)
    );
    expect(runASessions.length).toBe(1);
  });
});
