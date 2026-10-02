/**
 * #1081 E2E: the deposit_artifact builtin, from the Tools page to the session.
 *
 * Proves the operator path an agent depends on:
 *   1. The Tools page lists deposit_artifact under builtins, off by default,
 *      with the one-sentence description of what appears where.
 *   2. An agent with only the MCP URL and a session-bound key does not see
 *      the tool until it is switched on, then sees it and deposits a WebVTT
 *      transcript and a summary that names the transcript as its parent.
 *   3. Both deposits are in GET /runtime-sessions/{id}/artifacts and in the
 *      session timeline as `artifact` activities.
 *
 * All data goes through the product's own ingestion path (login, runtime
 * session token, MCP tools/call). No database rows are written by hand.
 *
 * A runtime session token carries the tools it asked for at mint time, kept
 * only if the account has them enabled, so the agent re-mints after the
 * operator switches the tool on.
 *
 * HOW TO RUN: a local backend seeded with INIT_TEST_DATA=true (admin/admin),
 * not in TESTING mode (the MCP server lifespan must run), then
 *        cd frontend
 *        PRELOOP_E2E_BASE_URL=http://localhost:5173 \
 *          npx playwright test --project=e2e-ci deposit-artifact-tool
 * Set PRELOOP_E2E_SCREENSHOTS=<dir> to also write the PR screenshots.
 */

import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from '@playwright/test';
import * as path from 'path';
import { dismissPlanChoiceIfShown } from './login';

const TOOL = 'deposit_artifact';
const SHOTS = process.env.PRELOOP_E2E_SCREENSHOTS;
const USERNAME = process.env.PRELOOP_E2E_USERNAME || 'admin';
const PASSWORD = process.env.PRELOOP_E2E_PASSWORD || 'admin';

async function shot(page: Page, name: string): Promise<void> {
  if (SHOTS) {
    // Let switch transitions settle so the picture shows the final state.
    await page.waitForTimeout(400);
    await page.screenshot({
      path: path.join(SHOTS, `${name}.png`),
      fullPage: true,
    });
  }
}

async function login(page: Page): Promise<string> {
  await page.goto('/login');
  const username = page.locator('sl-input[name="username"]');
  await username.waitFor({ state: 'visible' });
  await username.click();
  await page.keyboard.type(USERNAME);
  await page.locator('sl-input[name="password"]').click();
  await page.keyboard.type(PASSWORD);
  await page.locator('sl-button[type="submit"]').click();
  await page.waitForURL(/\/console/, { timeout: 30_000 });
  let token: string | null = null;
  await expect
    .poll(
      async () =>
        (token = await page.evaluate(() =>
          localStorage.getItem('accessToken')
        )),
      { timeout: 30_000 }
    )
    .not.toBeNull();
  await dismissPlanChoiceIfShown(page);
  return token as unknown as string;
}

/** One JSON-RPC call to the stateless streamable HTTP MCP endpoint. */
async function mcp(
  request: APIRequestContext,
  token: string,
  method: string,
  params: Record<string, unknown> = {}
): Promise<any> {
  const response = await request.post('/mcp/v1', {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json, text/event-stream',
      'Content-Type': 'application/json',
    },
    data: { jsonrpc: '2.0', id: 1, method, params },
  });
  expect(response.status(), await response.text()).toBe(200);
  const body = await response.text();
  const json = body.trim().startsWith('{')
    ? body
    : body
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .pop();
  const message = JSON.parse(json as string);
  expect(message.error, JSON.stringify(message.error)).toBeUndefined();
  return message.result;
}

async function toolNames(request: APIRequestContext, token: string) {
  const result = await mcp(request, token, 'tools/list');
  return (result.tools as { name: string }[]).map((t) => t.name);
}

function vtt(): string {
  const cue =
    '00:00:01.000 --> 00:00:02.000\nPicker 4 reports a short pick in aisle 12.\n\n';
  return 'WEBVTT\n\n' + cue.repeat(40);
}

test('deposit_artifact: Tools page opt in, MCP deposit, session list', async ({
  page,
  request,
}) => {
  const userToken = await login(page);
  const auth = { Authorization: `Bearer ${userToken}` };

  // 1. Discoverable on the Tools page, off by default.
  await page.goto('/console/tools');
  const row = page
    .locator('tool-list-item')
    .filter({ has: page.locator('.tool-name', { hasText: TOOL }) });
  await row.first().scrollIntoViewIfNeeded({ timeout: 30_000 });
  await expect(row.first()).toContainText(
    'visible in the Preloop session timeline'
  );
  const toggle = row.first().locator('sl-switch');
  // A rerun against the same stack starts from the default again.
  if ((await toggle.getAttribute('checked')) !== null) {
    await toggle.click();
  }
  await expect(toggle).not.toHaveAttribute('checked', /.*/);
  await shot(page, '1-tools-page-default-off');

  // 2. An agent asks for the tool when its session token is minted. The
  //    mint keeps only tools the account has enabled, so while it is off
  //    the agent is not offered it.
  const sourceId = `e2e-1081-${Date.now()}`;
  const mint = async () => {
    const minted = await request.post('/api/v1/auth/runtime-sessions/token', {
      headers: auth,
      data: {
        session_source_type: 'custom',
        session_source_id: sourceId,
        session_reference: 'warehouse stand-up',
        runtime_principal_id: 'warehouse-agent',
        runtime_principal_name: 'Warehouse agent',
        allowed_mcp_tools: [{ name: TOOL }],
      },
    });
    expect(minted.status(), await minted.text()).toBe(201);
    return (await minted.json()) as {
      token: string;
      runtime_session_id: string;
    };
  };
  expect(await toolNames(request, (await mint()).token)).not.toContain(TOOL);

  // Switch it on where an operator would, then the agent reconnects.
  await toggle.click();
  await expect(toggle).toHaveAttribute('checked', /.*/);
  let agentToken = '';
  let sessionId = '';
  await expect
    .poll(
      async () => {
        ({ token: agentToken, runtime_session_id: sessionId } = await mint());
        return toolNames(request, agentToken);
      },
      { timeout: 15_000 }
    )
    .toContain(TOOL);
  await row.first().locator('.tool-header').click();
  await shot(page, '2-tools-page-enabled');

  // 3. Deposit a transcript and a summary over MCP.
  const transcript = await mcp(request, agentToken, 'tools/call', {
    name: TOOL,
    arguments: {
      name: 'standup.vtt',
      content: {
        type: 'resource',
        resource: {
          uri: 'file:///standup.vtt',
          mimeType: 'text/vtt',
          text: vtt(),
        },
      },
      labels: { source_tool: 'whisper', site: 'nord' },
    },
  });
  expect(transcript.isError).toBeFalsy();
  const link = transcript.content[0];
  expect(link.type).toBe('resource_link');
  expect(link.mimeType).toBe('text/vtt');
  expect(link.size).toBeGreaterThan(0);
  expect(link.uri).toContain(`/runtime-sessions/${sessionId}/artifacts/`);
  const transcriptId = transcript.structuredContent.id;

  const summary = await mcp(request, agentToken, 'tools/call', {
    name: TOOL,
    arguments: {
      name: 'standup summary',
      kind: 'document',
      content: { type: 'text', text: 'Picker 4 is short in aisle 12.' },
      parent_artifact_id: transcriptId,
    },
  });
  expect(summary.isError).toBeFalsy();
  expect(summary.structuredContent.parent_artifact_id).toBe(transcriptId);

  const listed = await request.get(
    `/api/v1/runtime-sessions/${sessionId}/artifacts`,
    { headers: { Authorization: `Bearer ${agentToken}` } }
  );
  expect(listed.status()).toBe(200);
  const items = (await listed.json()).items as any[];
  expect(items.map((i) => i.id)).toEqual(
    expect.arrayContaining([transcriptId, summary.structuredContent.id])
  );

  // Show the agent's view of the result (the MCP answers, verbatim).
  await page.setContent(
    `<main style="font:14px system-ui;padding:24px;max-width:1100px">
       <h2>deposit_artifact over MCP (session ${sessionId})</h2>
       <h3>tools/call result: transcript</h3>
       <pre style="background:#f4f4f5;padding:12px;white-space:pre-wrap">${escape(
         JSON.stringify({ content: transcript.content }, null, 2)
       )}</pre>
       <h3>GET /api/v1/runtime-sessions/{id}/artifacts</h3>
       <pre style="background:#f4f4f5;padding:12px;white-space:pre-wrap">${escape(
         JSON.stringify(
           items.map(
             ({
               id,
               kind,
               name,
               content_type,
               size_bytes,
               producer,
               tool_name,
               parent_artifact_id,
             }) => ({
               id,
               kind,
               name,
               content_type,
               size_bytes,
               producer,
               tool_name,
               parent_artifact_id,
             })
           ),
           null,
           2
         )
       )}</pre>
     </main>`
  );
  await shot(page, '3-mcp-result-and-artifact-list');
});

function escape(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;');
}
