import assert from 'node:assert/strict';
import fs from 'node:fs';
import { after, before, test } from 'node:test';
import { chromium } from '@playwright/test';

const template = fs.readFileSync(
  new URL('../../backend/preloop/templates/approval.html', import.meta.url),
  'utf8'
);
const externalUrl = 'https://consent.example.com/decide?r=a%2fb&state=x%2By';
let browser;

before(async () => {
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
});

for (const handoff of [
  { external_handoff_state: 'ready', external_url: externalUrl },
  { external_handoff_state: 'waiting' },
  { external_handoff_state: 'notify' },
  { external_handoff_state: 'failed' },
  {},
]) {
  test(`token page handles ${handoff.external_handoff_state || 'ordinary'} approval`, async (t) => {
    const page = await browser.newPage();
    t.after(() => page.close());
    const decisions = [];
    const data = {
      id: 'request-example',
      tool_name: 'read_record',
      tool_args: {},
      status: 'pending',
      requested_at: '2026-11-16T12:00:00Z',
      history: [],
      ...handoff,
    };
    await page.route('**/*', async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith('/data')) {
        await route.fulfill({ json: data });
      } else if (pathname.endsWith('/decide')) {
        decisions.push(route.request().postDataJSON());
        await route.fulfill({ json: { ...data, status: 'approved' } });
      } else if (pathname === '/approval/request-example') {
        await route.fulfill({ contentType: 'text/html', body: template });
      } else {
        await route.abort();
      }
    });
    await page.goto(
      'https://preloop.example.com/approval/request-example?token=test-token'
    );
    await page.locator('#content').waitFor({ state: 'visible' });

    const external = ['ready', 'waiting', 'notify'].includes(
      handoff.external_handoff_state
    );
    assert.equal(await page.locator('#approve-btn').isVisible(), !external);
    assert.equal(await page.locator('#decline-btn').isVisible(), !external);
    assert.equal(await page.locator('#comment-section').isVisible(), !external);
    if (handoff.external_url) {
      const link = page.getByRole('link', {
        name: 'Decide on external service',
      });
      assert.equal(await link.getAttribute('href'), externalUrl);
      assert.equal(await link.getAttribute('referrerpolicy'), 'no-referrer');
      assert.equal(await link.isVisible(), true);
    }
    if (external) {
      // A stale or scripted button event must not send a local decision either.
      await page.locator('#approve-btn').dispatchEvent('click');
      assert.deepEqual(decisions, []);
      if (handoff.external_handoff_state === 'waiting') {
        data.external_url = externalUrl;
        data.external_handoff_state = 'ready';
        const link = page.getByRole('link', {
          name: 'Decide on external service',
        });
        await link.waitFor({ state: 'visible' });
        assert.equal(await link.getAttribute('href'), externalUrl);
        assert.equal(await page.locator('#approve-btn').isVisible(), false);
      } else if (handoff.external_handoff_state === 'notify') {
        data.external_handoff_state = 'failed';
        await page.locator('#approve-btn').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#decline-btn').isVisible(), true);
      }
    } else {
      await page.locator('#approve-btn').click();
      await page.locator('#status').filter({ hasText: 'approved' }).waitFor();
      assert.equal(decisions.length, 1);
    }
  });
}
