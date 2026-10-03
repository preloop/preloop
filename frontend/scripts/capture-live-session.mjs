import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

process.env.PRELOOP_DISABLE_TELEMETRY = 'true';
const beforeUrl = process.argv[2] || 'http://127.0.0.1:5190';
const afterUrl = process.argv[3] || 'http://127.0.0.1:5189';
for (const url of [beforeUrl, afterUrl]) {
  if (!['localhost', '127.0.0.1', '[::1]'].includes(new URL(url).hostname))
    throw new Error('Synthetic screenshots require local Vite servers.');
}
const outputDirectory = path.resolve(
  process.argv[4] || '../docs/assets/screenshots/sessions'
);
await mkdir(outputDirectory, { recursive: true });

const browser = await chromium.launch();
const approval = {
  id: 'approval-example',
  account_id: 'account-example',
  tool_name: 'publish_report',
  summary: 'Publish the report to the example project',
  tool_args: { project: 'example/project', path: 'reports/review.md' },
  agent_reasoning: 'The report is ready for review.',
  status: 'pending',
  requested_at: '2026-10-02T10:00:30Z',
  expires_at: '2099-10-02T10:10:30Z',
  runtime_session_id: 'session-example',
  resolved_at: null,
};
// Fixed clock makes the same synthetic fixture reproducible on both revisions.
const fixedNow = Date.parse('2026-10-02T10:00:45Z');
approval.expires_at = '2026-10-02T10:10:30Z';
for (const [revision, baseUrl] of [
  ['before', beforeUrl],
  ['after', afterUrl],
]) {
  const context = await browser.newContext({
    viewport: { width: 1150, height: 1100 },
    timezoneId: 'UTC',
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript((now) => {
    Date.now = () => now;
  }, fixedNow);
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    let data = {};
    if (url.pathname.endsWith('/auth/users/me'))
      data = {
        id: 'user-example',
        username: 'Jane Doe',
        email: 'jane@example.com',
        permissions: ['view_approvals', 'decide_approvals'],
      };
    else if (url.pathname === '/api/v1/approval-requests') data = [approval];
    else if (url.pathname.includes('approval-requests/')) data = approval;
    return route.fulfill({ json: data });
  });
  await page.route('**/__session-review', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><link id="theme" rel="stylesheet" href="/node_modules/@shoelace-style/shoelace/dist/themes/light.css"><style>body{font-family:system-ui;margin:24px;background:var(--sl-color-neutral-0);color:var(--sl-color-neutral-900)}h2{font-size:22px;margin:0 0 24px}session-chat-view{display:block;max-width:1100px}</style></head><body><h2>Example agent session</h2><session-chat-view></session-chat-view></body></html>`,
    })
  );
  await page.goto(`${baseUrl}/__session-review`);
  await page.evaluate(async () => {
    localStorage.setItem('accessToken', 'synthetic-test-token');
    const ws = await import('/src/services/unified-websocket-manager.ts');
    ws.unifiedWebSocketManager.getState = () => ws.ConnectionState.CONNECTED;
    await import('/src/components/session-chat-view.ts');
    const chat = document.querySelector('session-chat-view');
    chat.sessionId = 'session-example';
    chat.events = [
      {
        id: 'event-example',
        execution_id: '',
        type: 'model_gateway_call',
        timestamp: '2026-10-02T10:00:20Z',
        payload: {
          outcome: 'success',
          conversation_preview: {
            messages: [
              {
                source: 'request',
                role: 'user',
                text: 'Check the workspace and publish the report.',
              },
              {
                source: 'request',
                role: 'tool',
                text: '{"clean":true,"files_changed":0}',
                tool_call_ids: ['call-example'],
              },
              {
                source: 'response',
                role: 'assistant',
                text: 'The workspace looks good. Publishing requires approval.',
              },
            ],
          },
          request: {
            messages: [
              {
                role: 'tool',
                tool_call_id: 'call-example',
                content: '{"clean":true,"files_changed":0}',
              },
            ],
          },
          tools: [
            {
              kind: 'call',
              call_id: 'call-example',
              name: 'terminal',
              text: '{"command":"git status --short","cwd":"/workspace/example"}',
            },
            {
              kind: 'result',
              call_id: 'call-example',
              text: '{"clean":true,"files_changed":0}',
            },
          ],
        },
      },
    ];
    await chat.updateComplete;
  });
  if (revision === 'after') {
    await page.getByRole('button', { name: 'Approve', exact: true }).waitFor();
    await page.locator('session-tool-card summary').click();
    await page.locator('session-tool-card pre').first().waitFor();
    if (await page.locator('session-approval-card sl-button sl-button').count())
      throw new Error('Decision actions are nested');
  } else {
    await page
      .locator('session-chat-view sl-details')
      .first()
      .evaluate((el) => (el.open = true));
  }
  await page.waitForTimeout(350);
  for (const [size, width, height] of [
    ['desktop', 1150, 1100],
    ['mobile', 390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.screenshot({
      path: path.join(outputDirectory, `live-session-${revision}-${size}.png`),
      fullPage: true,
    });
  }
  if (revision === 'after') {
    await page.setViewportSize({ width: 1150, height: 1100 });
    await page.evaluate(() => {
      document.documentElement.classList.add('sl-theme-dark');
      document.querySelector('#theme').href =
        '/node_modules/@shoelace-style/shoelace/dist/themes/dark.css';
    });
    await page.waitForFunction(
      () =>
        getComputedStyle(document.documentElement)
          .getPropertyValue('--sl-color-neutral-0')
          .trim() !== 'hsl(0, 0%, 100%)'
    );
    await page.waitForTimeout(350);
    await page.screenshot({
      path: path.join(outputDirectory, 'live-session-after-dark.png'),
      fullPage: true,
    });
  }
  console.log(
    JSON.stringify({
      revision,
      errors,
      toolCards: await page.locator('session-tool-card').count(),
      approvalCards: await page.locator('session-approval-card').count(),
    })
  );
  await context.close();
}
await browser.close();
