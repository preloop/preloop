import { chromium, expect } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

process.env.PRELOOP_DISABLE_TELEMETRY = 'true';
const revision = process.argv[2];
if (!['before', 'after'].includes(revision))
  throw new Error('Invalid revision');
const output = '../docs/pr-screenshots/session-parent-findings';
await mkdir(output, { recursive: true });
const base = {
  session_source_type: 'claude_code',
  started_at: '2026-11-26T10:00:00Z',
  last_activity_at: '2026-11-26T10:01:00Z',
  ended_at: '2026-11-26T10:01:00Z',
  activity_status: 'ended',
  total_requests: 0,
  token_usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
};
const sessions = [
  {
    ...base,
    id: 'child-session',
    title: 'Review the changes',
    parent_session_id: 'parent-session',
    subagent_role: 'reviewer',
  },
  { ...base, id: 'parent-session', title: 'Prepare the release' },
];
const device = {
  device_key: 'fp:' + 'a'.repeat(64),
  hostname: 'example-workstation',
  os: 'linux',
  runner_state: 'online',
  collection_state: 'none',
  inventory_state: 'fresh',
  roster_state: 'not_on_roster',
  verification: 'unverified',
  last_seen_at: base.last_activity_at,
  agents_onboarded: 0,
  agents_not_onboarded: 0,
  users: [],
  runners: [],
  agents: [],
  possible_same_machine_as: [],
  sources: [],
  findings: [
    { extension_id: 'review-helper', kind: 'skill', reason: 'unapproved' },
    {
      extension_id: 'release-check',
      kind: 'hook',
      reason: 'content_changed',
      approval_state: 'approved',
    },
    {
      extension_id: 'build-tools',
      kind: 'plugin',
      reason: 'version_outside_constraint',
      effective_state: 'approved',
    },
  ].map((evidence, index) => ({
    id: `finding-${index}`,
    rule: 'unapproved_extension',
    device_key: 'fp:' + 'a'.repeat(64),
    state: 'open',
    opened_at: base.started_at,
    last_evaluated_at: base.last_activity_at,
    evidence,
    title: 'An installed agent extension needs approval.',
    detail:
      'Inventory proves files are present, not that the agent loaded or executed them.',
    limitation: 'Self-reported inventory.',
  })),
};
const browser = await chromium.launch();
try {
  for (const scenario of ['sessions', 'findings']) {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
      reducedMotion: 'reduce',
    });
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.hostname !== '127.0.0.1') return route.abort();
      if (url.pathname === '/__console-regression') {
        return route.fulfill({
          contentType: 'text/html',
          body: '<!doctype html><html class="sl-theme-light"><head><link rel="stylesheet" href="/node_modules/@shoelace-style/shoelace/dist/themes/light.css"><link rel="stylesheet" href="/src/styles/console-surfaces.css"><style>body{font:14px system-ui;margin:32px;color:var(--sl-color-neutral-900);background:var(--console-page)}</style></head><body></body></html>',
        });
      }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      let data = { items: [], logs: [], total: 0, has_more: false };
      const path = url.pathname;
      if (path.endsWith('/features'))
        data = { features: { fleet_coverage: true } };
      else if (path.endsWith('/auth/users/me'))
        data = {
          id: 'example-user',
          username: 'Jane Doe',
          email: 'jane@example.com',
          permissions: ['view_device_fleet'],
        };
      else if (path.endsWith('/entitlements')) data = { premium: true };
      else if (path.endsWith('/approval-requests')) data = [];
      else if (path.endsWith('/runtime-sessions'))
        data = { items: sessions, total: 2 };
      else if (sessions.some((session) => path.endsWith('/' + session.id)))
        data = {
          session: sessions.find((session) => path.endsWith('/' + session.id)),
        };
      else if (path.endsWith('/device-fleet/detail'))
        data = { schema_version: 1, device, observations: [] };
      return route.fulfill({ json: data });
    });
    await page.clock.install({ time: new Date('2026-11-26T10:02:00Z') });
    await page.goto('http://127.0.0.1:5197/__console-regression');
    await page.evaluate(async (scenario) => {
      localStorage.setItem('accessToken', 'synthetic-token');
      const { setBasePath } =
        await import('/node_modules/@shoelace-style/shoelace/dist/utilities/base-path.js');
      setBasePath('/node_modules/@shoelace-style/shoelace/dist');
      const { unifiedWebSocketManager: ws } =
        await import('/src/services/unified-websocket-manager.ts');
      ws.connect = async () => {};
      ws.subscribe = () => () => {};
      if (scenario === 'sessions') {
        await import('/src/views/authed/runtime-sessions-view.ts');
        const { Router } = await import('/src/router/index.ts');
        const outlet = document.createElement('main');
        document.body.append(outlet);
        const router = new Router(outlet);
        await router.setRoutes(
          [
            {
              path: '/console/runtime-sessions',
              component: 'runtime-sessions-view',
            },
          ],
          true
        );
        await router.render(
          '/console/runtime-sessions?sessionId=child-session',
          { history: 'replace' }
        );
      } else {
        history.replaceState(
          {},
          '',
          '/console/settings/device-fleet?device=' +
            encodeURIComponent('fp:' + 'a'.repeat(64))
        );
        await import('/src/views/authed/device-fleet-view.ts');
        document.body.append(document.createElement('device-fleet-view'));
      }
    }, scenario);
    if (scenario === 'sessions') {
      await expect(page.locator('#session-title')).toHaveText(
        'Review the changes'
      );
      await page
        .getByRole('link', { name: 'Parent: Prepare the release', exact: true })
        .click();
      await expect(page).toHaveURL(/sessionId=parent-session/);
      if (revision === 'before') await page.waitForTimeout(5000);
      await expect(page.locator('#session-title')).toHaveText(
        revision === 'before' ? 'Review the changes' : 'Prepare the release'
      );
      await page.screenshot({
        path: `${output}/${revision}-parent-click.png`,
        fullPage: true,
      });
    } else {
      const findings = page.locator('[data-testid="device-findings"]');
      await expect(findings.locator('li')).toHaveCount(3);
      if (revision === 'after')
        await expect(findings).toContainText(
          'release-check (hook): Content changed'
        );
      await findings.screenshot({ path: `${output}/${revision}-findings.png` });
    }
    await page.close();
  }
} finally {
  await browser.close();
}
