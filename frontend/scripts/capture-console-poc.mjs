import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
process.env.PRELOOP_DISABLE_TELEMETRY = 'true';
const revision = process.argv[2];
if (!['before', 'after'].includes(revision)) throw new Error('Invalid revision');
const output = '../docs/pr-screenshots/console-poc-polish';
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
for (const scenario of ['approvals', 'extensions', 'sessions']) {
 const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
 await page.route('**/api/**', route => {
  const url = route.request().url(); let data = {};
  if (url.includes('/approval-requests')) data = ['native_tool_approvals_off', 'bypass', 'flow_approval_policy', null, null].map((reason, i) => ({ id: `approval-${i}`, account_id: 'example-account', tool_configuration_id: 'example-tool', approval_workflow_id: 'example-workflow', tool_name: 'Read', tool_args: { path: 'design.md' }, summary: 'Read the design proposal', status: 'approved', requested_at: '2026-11-26T10:00:00Z', resolved_at: '2026-11-26T10:00:01Z', auto_approved_reason: reason, auto_approval_bypass_id: i === 1 ? 'example-bypass' : null, decided_by_ai: i === 3, decided_by_human: i === 4, human_decision_recorded: i === 4 }));
  else if (url.includes('/auth/users/me')) data = { id: 'example-user', username: 'Jane Doe', email: 'jane@example.com', permissions: ['view_agents', 'view_runtime_sessions'] };
  else if (url.includes('/features')) data = { features: {} };
  else if (/gateway-events|activity|artifacts|requests/.test(url)) data = { items: [], logs: [], total: 0, has_more: false };
  return route.fulfill({ json: data });
 });
 await page.route('**/__console-poc*', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><html class="sl-theme-light"><head><link rel="stylesheet" href="/node_modules/@shoelace-style/shoelace/dist/themes/light.css"><link rel="stylesheet" href="/src/styles/console-surfaces.css"><style>body{font:14px system-ui;margin:32px;color:var(--sl-color-neutral-900);background:var(--console-page)}h1{font-size:26px}extension-inventory,preloop-session-observer{display:block;background:var(--console-surface);padding:16px}</style></head><body></body></html>' }));
 await page.clock.install({ time: new Date('2026-11-26T10:02:00Z') });
 await page.goto('http://127.0.0.1:5197/__console-poc?status=all');
 await page.evaluate(async scenario => {
  localStorage.setItem('accessToken', 'synthetic-token');
  const { setBasePath } = await import('/node_modules/@shoelace-style/shoelace/dist/utilities/base-path.js'); setBasePath('/node_modules/@shoelace-style/shoelace/dist');
  const { unifiedWebSocketManager: ws } = await import('/src/services/unified-websocket-manager.ts'); ws.connect = async () => {}; ws.subscribe = () => () => {};
  if (scenario === 'approvals') { await import('/src/views/authed/approvals-view.ts'); document.body.append(document.createElement('approvals-view')); }
  else if (scenario === 'extensions') {
   document.body.innerHTML = '<h1>Extensions</h1>'; await import('/src/components/extension-inventory.ts');
   const el = document.createElement('extension-inventory'); el.accessLoader = async () => ({ view: true, manage: false });
   el.loader = async () => ({ items: [], next_cursor: null, completeness: 'partial', coverage: [{ fingerprint: 'a'.repeat(64), collector_version: '1.1.0-rc.1', completeness: 'partial', reasons: ['extension_project_unscanned'] }] }); document.body.append(el);
  } else {
   document.body.innerHTML = '<h1>Sessions</h1>'; await import('/src/components/preloop-session-observer.ts');
   const base = { session_source_type: 'claude_code', started_at: '2026-11-26T10:00:00Z', last_activity_at: '2026-11-26T10:00:30Z', total_requests: 3, successful_requests: 3, token_usage: { total_tokens: 2400 }, estimated_cost: 0.01 };
   const parent = { ...base, id: 'parent-example', title: 'Review the design proposal', ended_at: '2026-11-26T10:01:00Z', turn_state: 'ended' };
   const child = { ...base, id: 'child-example', title: 'Review design risks', parent_session_id: parent.id, alias: 'reviewer', subagent_role: 'reviewer', turn_state: 'your_turn', turn_ended_at: '2026-11-26T10:00:30Z', ended_at: null };
   const el = document.createElement('preloop-session-observer'); el.sessions = [parent, child]; el.activeSessionId = child.id; document.body.append(el);
  }
 }, scenario);
 await page.waitForTimeout(1800);
 await page.screenshot({ path: `${output}/${revision}-${scenario}.png`, fullPage: true });
 if (scenario === 'sessions') { await page.setViewportSize({ width: 390, height: 844 }); await page.screenshot({ path: `${output}/${revision}-sessions-phone.png`, fullPage: true }); }
 await page.close();
}
await browser.close();
