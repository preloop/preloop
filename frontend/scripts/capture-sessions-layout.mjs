import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

process.env.PRELOOP_DISABLE_TELEMETRY = 'true';
const baseUrl = process.argv[2] || 'http://127.0.0.1:5197';
const revision = process.argv[3] || 'before';
if (!['localhost', '127.0.0.1', '[::1]'].includes(new URL(baseUrl).hostname)) {
  throw new Error('Synthetic screenshots require a local Vite server.');
}
if (!['before', 'after'].includes(revision))
  throw new Error('Expected before or after.');
const output = path.resolve('../docs/pr-screenshots/sessions-list-layout');
await mkdir(output, { recursive: true });
const now = Date.parse('2026-11-25T12:00:00Z');
const sessions = Array.from({ length: 20 }, (_, index) => ({
  id: `session-example-${index + 1}`,
  title: [
    'Review the release checklist',
    'Investigate a failed test',
    'Document the setup steps',
    'Check dependency updates',
  ][index % 4],
  session_source_type: 'claude_code',
  session_source_id: `workspace-example-${index + 1}`,
  session_reference: `example-${index + 1}`,
  runtime_principal_name: 'Example agent',
  started_at: new Date(now - (index + 1) * 3600000).toISOString(),
  last_activity_at: new Date(now - index * 3600000 - 120000).toISOString(),
  ended_at: null,
  turn_state: index === 0 ? 'working' : 'idle',
  activity_status: index === 0 ? 'active_now' : 'idle',
  is_active_now: index === 0,
  total_requests: 16 + index,
  successful_requests: 16 + index,
  failed_requests: 0,
  token_usage: {
    prompt_tokens: 12400,
    completion_tokens: 3100,
    total_tokens: 15500,
  },
  estimated_cost: 0.42 + index * 0.1,
  artifact_counts: index % 3 === 0 ? { document: 2, screenshot: 1 } : {},
}));
const browser = await chromium.launch();
for (const [size, width, height] of [
  ['desktop', 1440, 1000],
  ['phone', 390, 844],
  ...(revision === 'after' ? [['phone-free', 390, 844]] : []),
]) {
  const context = await browser.newContext({
    viewport: { width, height },
    timezoneId: 'UTC',
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.clock.install({ time: now });
  await page.route('**/api/**', (route) => {
    const pathname = new URL(route.request().url()).pathname;
    let data = {};
    if (pathname === '/api/v1/runtime-sessions')
      data = {
        items: sessions,
        total: 55,
        limit: 20,
        offset: 0,
        has_more: true,
      };
    else if (/\/runtime-sessions\/[^/]+$/.test(pathname))
      data = {
        session:
          sessions.find((session) => pathname.endsWith(session.id)) ||
          sessions[0],
        usage_by_model: [],
        activity_timeline: [],
      };
    else if (pathname.endsWith('/gateway-events'))
      data = {
        logs: [
          {
            id: 'event-example',
            timestamp: '2026-11-25T11:58:00Z',
            type: 'model_gateway_call',
            payload: {
              outcome: 'success',
              conversation_preview: {
                messages: [
                  {
                    role: 'user',
                    text: 'Review the release checklist and identify any missing steps.',
                  },
                  {
                    role: 'assistant',
                    text: 'The checklist covers validation and release notes. I am checking the setup instructions next.',
                  },
                ],
              },
            },
          },
        ],
        total: 1,
        has_more: false,
        next_offset: null,
      };
    else if (
      pathname.endsWith('/activity') ||
      pathname.endsWith('/artifacts') ||
      pathname.endsWith('/requests')
    )
      data = { items: [], total: 0, has_more: false };
    else if (pathname.endsWith('/auth/users/me'))
      data = {
        id: 'user-example',
        username: 'Jane Doe',
        email: 'jane@example.com',
        permissions: ['view_runtime_sessions'],
      };
    else if (pathname.endsWith('/billing/entitlements'))
      data = { premium: size !== 'phone-free' };
    else if (pathname.endsWith('/features')) data = { features: {} };
    else if (
      pathname.endsWith('/ai-models') ||
      pathname.endsWith('/approval-requests') ||
      pathname.endsWith('/flow-conversations')
    )
      data = [];
    return route.fulfill({ json: data });
  });
  await page.route('**/__sessions-layout**', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html class="sl-theme-light"><head><script data-shoelace="/vendor/shoelace"></script><link rel="stylesheet" href="/node_modules/@shoelace-style/shoelace/dist/themes/light.css"><link rel="stylesheet" href="/src/styles/console-surfaces.css"><link rel="stylesheet" href="/src/styles/console-fonts.css"><style>body{font-family:system-ui;margin:0;padding:16px 32px;background:var(--console-surface);color:var(--sl-color-neutral-900)}@media(max-width:640px){body{padding:16px}}runtime-sessions-view{display:block}</style></head><body></body></html>`,
    })
  );
  await page.goto(`${baseUrl}/__sessions-layout?sessionId=session-example-1`);
  await page.evaluate(async () => {
    localStorage.setItem('accessToken', 'synthetic-test-token');
    const { setBasePath } =
      await import('/node_modules/@shoelace-style/shoelace/dist/utilities/base-path.js');
    setBasePath('/node_modules/@shoelace-style/shoelace/dist');
    const { unifiedWebSocketManager: ws, ConnectionState } =
      await import('/src/services/unified-websocket-manager.ts');
    ws.connect = async () => {};
    ws.subscribe = () => () => {};
    ws.send = () => true;
    ws.getState = () => ConnectionState.CONNECTED;
    await import('/src/views/authed/runtime-sessions-view.ts');
    document.body.append(document.createElement('runtime-sessions-view'));
  });
  await page
    .locator('session-list-panel .session-card')
    .first()
    .waitFor({ state: 'attached' });
  await page.locator('session-chat-view').waitFor({ state: 'attached' });
  await page.waitForTimeout(600);
  await page.screenshot({
    path: path.join(output, `${revision}-${size}.png`),
    fullPage: true,
  });
  if (revision === 'after' && size !== 'phone-free') {
    if (size === 'desktop') {
      await page
        .locator('preloop-session-observer .sidebar')
        .evaluate((element) => {
          element.scrollTop = element.scrollHeight;
        });
      await page.screenshot({
        path: path.join(output, 'after-desktop-list-footer.png'),
        fullPage: true,
      });
      await page
        .locator('preloop-session-observer .sidebar')
        .evaluate((element) => {
          element.scrollTop = 0;
        });
    } else {
      await page.getByRole('link', { name: 'Back to sessions' }).click();
      await page.screenshot({
        path: path.join(output, 'after-phone-list.png'),
        fullPage: true,
      });
      await page.locator('a.session-select').first().click();
    }
    await page.evaluate(async () => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/node_modules/@shoelace-style/shoelace/dist/themes/dark.css';
      const loaded = new Promise((resolve) =>
        link.addEventListener('load', resolve, { once: true })
      );
      document.head.append(link);
      await loaded;
      document.documentElement.className = 'sl-theme-dark';
    });
    await page.waitForTimeout(350);
    await page.screenshot({
      path: path.join(output, `after-${size}-dark.png`),
      fullPage: true,
    });
  }
  if (errors.length) throw new Error(errors.join('\n'));
  console.log(JSON.stringify({ revision, size, errors }));
  await context.close();
}
await browser.close();
