/** Real local Lit views, synthetic fixtures, all backend traffic intercepted. */
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
if (process.env.PRELOOP_DISABLE_TELEMETRY !== 'true')
  throw new Error('Disable telemetry.');
const base = 'http://127.0.0.1:5187';
const output = resolve('../docs/pr-screenshots/approval-readiness');
await mkdir(output, { recursive: true });
const agentId = '00000000-0000-4000-8000-000000000001',
  observerId = '00000000-0000-4000-8000-000000000002',
  workflowId = '00000000-0000-4000-8000-000000000003';
const time = '2026-11-26T02:00:00Z';
const workflow = {
  id: workflowId,
  name: 'Native tool review',
  approval_type: 'standard',
  approval_mode: 'standard',
  timeout_seconds: 300,
  is_default: true,
};
const readiness = {
  enforced: true,
  workflow_id: workflowId,
  workflow_name: workflow.name,
  recipients: ['Jane Doe'],
  deciders: ['Jane Doe'],
  destinations: [],
  issues: [
    "Only the agent's owner receives requests, and no notification destination is available. Enable notifications or add another recipient.",
  ],
  timeout_seconds: 300,
  ai_fallback: null,
  external: false,
};
const device = {
  device_key: 'fp:' + 'a'.repeat(64),
  hostname: 'example-workstation',
  os: 'linux',
  runner_state: 'never',
  collection_state: 'fresh',
  inventory_state: 'fresh',
  roster_state: 'not_on_roster',
  verification: 'unverified',
  last_seen_at: time,
  agents_onboarded: 1,
  agents_not_onboarded: 0,
  users: [{ id: 'user-1', label: 'Jane Doe', source: 'agent_owner' }],
  runners: [],
  agents: [
    {
      id: agentId,
      managed_agent_id: agentId,
      name: 'Example agent',
      kind: 'claude_code',
      status: 'onboarded',
      source: 'candidate',
      first_seen_at: time,
      last_seen_at: time,
    },
  ],
  possible_same_machine_as: [],
  findings: [],
};
const browser = await chromium.launch();
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    timezoneId: 'UTC',
  });
  await context.addInitScript(() =>
    localStorage.setItem('accessToken', 'synthetic-fixture')
  );
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== base) return route.abort();
    if (url.pathname === '/capture')
      return route.fulfill({
        contentType: 'text/html',
        body: '<!doctype html><html lang="en" class="sl-theme-light"><head><link rel="stylesheet" href="/vendor/shoelace/themes/light.css"><link rel="stylesheet" href="/src/main.css"></head><body style="margin:32px"><main id="capture"></main></body></html>',
      });
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const path = url.pathname;
    let data = [];
    if (path.endsWith('/features'))
      data = {
        features: { fleet_coverage: true, advanced_approvals: true },
        plugins: [],
      };
    else if (path.endsWith('/auth/users/me'))
      data = {
        id: 'user-1',
        username: 'reviewer',
        email: 'reviewer@example.com',
        account_id: 'account-1',
        is_account_owner: true,
        is_superuser: true,
        permissions: null,
      };
    else if (path.includes('configuration-capabilities'))
      data = { advanced_approvals: true };
    else if (path.endsWith('/account/governance-defaults'))
      data = {
        defaults: {
          native_tool_approvals: 'enforce',
          approval_workflow_id: workflowId,
        },
        override_agent_ids: [],
      };
    else if (
      path.endsWith('/approval-readiness') ||
      path.endsWith('/readiness')
    )
      data = readiness;
    else if (path.endsWith('/approval-workflows')) data = [workflow];
    else if (path.endsWith('/tools/registry'))
      data = [
        {
          id: 'entry-1',
          tool_name: 'fetch',
          mcp_server_id: '00000000-0000-4000-8000-000000000004',
          server_name: 'Documentation',
          server_label: null,
          first_seen_agent_id: agentId,
          first_seen_agent_name: 'Example agent',
          first_seen_at: time,
          state: 'approved',
        },
      ];
    else if (path.endsWith('/agents')) data = { agents: [], total: 0 };
    else if (path.endsWith('/fleet/device-fleet/detail'))
      data = {
        device,
        observations: [
          { source_ref: observerId, received_at: time, evidence: {} },
        ],
      };
    else if (path.endsWith('/fleet/identities'))
      data = {
        agents: [
          {
            id: agentId,
            name: 'Example agent',
            lifecycle_state: 'decommissioned',
          },
        ],
        observers: { [observerId]: 'Workstation reporter' },
      };
    else if (path.includes('discovery-extensions'))
      data = { items: [], next_cursor: null, completeness: 'complete' };
    return route.fulfill({ json: data });
  });
  const page = await context.newPage();
  await page.routeWebSocket('**/*', (socket) => socket.close());
  async function mount(module, tag, props = {}) {
    await page.goto(base + '/capture');
    await page.evaluate(
      async ({ module, tag, props }) => {
        await import(module);
        const el = document.createElement(tag);
        Object.assign(el, props);
        document.querySelector('#capture').append(el);
        await el.updateComplete;
      },
      { module, tag, props }
    );
  }
  await mount('/src/views/authed/tools-view.ts', 'tools-view');
  await page.waitForFunction(
    () => !document.querySelector('tools-view').loading
  );
  await page.evaluate(() => {
    document.querySelector('tools-view').activeTab = 'native';
  });
  await page
    .getByText("Only the agent's owner receives requests", { exact: false })
    .waitFor();
  await page.screenshot({ path: resolve(output, 'native-mode.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: resolve(output, 'native-mode-phone.png'),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mount(
    '/src/components/approval-workflow-dialog.ts',
    'approval-workflow-dialog',
    {
      open: true,
      features: { advanced_approvals: true },
      policy: {
        ...workflow,
        approval_type: 'ai_driven',
        approval_mode: 'ai_driven',
        ai_confidence_threshold: 0.8,
        ai_fallback_behavior: 'escalate',
      },
    }
  );
  await page.waitForFunction(
    () =>
      document.querySelector('approval-workflow-dialog')
        ._capabilitiesLoading === false
  );
  await page.getByText('At or above 80%', { exact: false }).waitFor();
  await page
    .getByText('At or above 80%', { exact: false })
    .scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  await page.screenshot({ path: resolve(output, 'ai-threshold.png') });
  await mount('/src/views/authed/policies-view.ts', 'policies-view');
  await page.waitForFunction(
    () => !document.querySelector('policies-view')._loading
  );
  await page.getByRole('tab', { name: 'Tools seen' }).click();
  await page.waitForTimeout(500);
  await page.locator('table[aria-label="Tools seen"] tbody tr').waitFor();
  await page.waitForTimeout(200);
  await page.screenshot({ path: resolve(output, 'tools-seen.png') });
  await mount('/src/views/authed/device-fleet-view.ts', 'device-fleet-view', {
    routeSearch: '?device=' + encodeURIComponent(device.device_key),
  });
  await page.getByText('Decommissioned', { exact: true }).waitFor();
  await page.screenshot({
    path: resolve(output, 'device-enrollment.png'),
    fullPage: true,
  });
  await page
    .getByRole('heading', { name: 'Device evidence' })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'device-evidence.png') });
  await mount('/src/components/approval-readiness.ts', 'approval-readiness', {
    requestId: 'request-1',
    expiresAt: '2026-11-26T02:07:15Z',
  });
  await page.getByText('Deadline:', { exact: false }).waitFor();
  await page
    .locator('approval-readiness')
    .screenshot({ path: resolve(output, 'pending-route.png') });
} finally {
  await browser.close();
}
