/**
 * The Policies page against the payloads the API really returns.
 *
 * Prod (2026-09-06) reported "TypeError: e is not iterable" from Lit's
 * repeat() directive and no dialog opening anywhere on the page. The cause is
 * in this file's stubs: GET /api/v1/policies/versions answers with
 * {versions: [...], total: n} while the view iterated the wrapper, and the
 * diff endpoints answer with a flat change list while the dialogs render
 * added, removed, and modified groups. A throw inside repeat() aborts the
 * update, so every part after it (all the dialogs) keeps its old value.
 */
import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './policies-view';
import type { PoliciesView } from './policies-view';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** GET /api/v1/policies/versions, PolicyVersionListResponse. */
const VERSIONS_PAYLOAD = {
  versions: [
    {
      id: 'ver-1',
      version_number: 3,
      tag: 'production',
      description: 'Before the rollout',
      is_active: false,
      mcp_servers_count: 2,
      policies_count: 4,
      tools_count: 7,
      created_at: '2026-09-01T10:00:00+00:00',
      created_by_user_id: 'user-9',
    },
  ],
  total: 1,
};

/** POST /api/v1/policies/diff, PolicyDiffResult. */
const DIFF_PAYLOAD = {
  has_changes: true,
  summary: '2 changes',
  changes: [
    { path: '$.tools[name=shell]', operation: 'add' },
    { path: '$.model_io[id=deny-pii]', operation: 'modify' },
  ],
};

interface Call {
  url: string;
  method: string;
  body: any;
}

function stubFetch(calls: Call[]) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method || 'GET').toUpperCase();
      let body: any = null;
      if (typeof init?.body === 'string') {
        try {
          body = JSON.parse(init.body);
        } catch {
          body = init.body;
        }
      }
      calls.push({ url, method, body });

      if (url.includes('/api/v1/auth/users/me')) {
        return json({ permissions: ['view_policies', 'manage_policies'] });
      }
      if (url.endsWith('/api/v1/tools')) {
        return json([]);
      }
      if (url.endsWith('/api/v1/approval-workflows')) {
        return json([]);
      }
      if (url.includes('/api/v1/features')) {
        return json({ plugins: [], features: {} });
      }
      if (url.includes('/api/v1/policies/model-io-rules')) {
        return json({ rules: [] });
      }
      if (url.includes('/api/v1/policies/export')) {
        return new Response('version: "1.0"\n', { status: 200 });
      }
      if (url.includes('/api/v1/policies/versions/prune')) {
        return json({ deleted_count: 3 });
      }
      if (url.includes('/rollback')) {
        return json({ success: true, error: null, diff: DIFF_PAYLOAD });
      }
      if (url.includes('/api/v1/policies/versions')) {
        return json(VERSIONS_PAYLOAD);
      }
      if (url.endsWith('/api/v1/policies/diff')) {
        return json(DIFF_PAYLOAD);
      }
      return json({ detail: `Unhandled: ${method} ${url}` }, 500);
    });
}

function headerButton(element: PoliciesView, label: string) {
  const buttons = Array.from(
    element.shadowRoot!.querySelectorAll('view-header sl-button')
  );
  return buttons.find((b) => b.textContent?.includes(label)) as HTMLElement;
}

function ruleDialog(element: PoliciesView) {
  return element.shadowRoot!.querySelector(
    '[data-testid="rule-dialog"]'
  ) as any;
}

function dialogByLabel(element: PoliciesView, label: string) {
  return Array.from(element.shadowRoot!.querySelectorAll('sl-dialog')).find(
    (dialog) => dialog.getAttribute('label') === label
  ) as any;
}

function toastTexts() {
  return Array.from(document.body.querySelectorAll('sl-alert')).map(
    (alert) => alert.textContent?.trim() ?? ''
  );
}

async function mount() {
  const calls: Call[] = [];
  const stub = stubFetch(calls);
  const element = (await fixture(
    html`<policies-view></policies-view>`
  )) as PoliciesView;
  await waitUntil(() => !(element as any)._loading, 'still loading');
  await waitUntil(
    () => (element as any)._versionsLoaded,
    'versions not loaded'
  );
  await element.updateComplete;
  return { element, calls, stub };
}

describe('Policies page against real API shapes', () => {
  let stub: sinon.SinonStub | undefined;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    stub?.restore();
    (window.fetch as any).restore?.();
    localStorage.clear();
    document.body.querySelectorAll('sl-alert').forEach((el) => el.remove());
  });

  it('P1 opens the rule dialog after the versions payload arrives', async () => {
    const m = await mount();
    stub = m.stub;

    // The wrapper object used to reach repeat() here and abort the render.
    expect((m.element as any)._versions)
      .to.be.an('array')
      .with.lengthOf(1);

    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;

    expect(ruleDialog(m.element), 'rule dialog missing').to.exist;
    expect(ruleDialog(m.element).open, 'rule dialog did not open').to.be.true;
    expect(toastTexts().join(' ')).to.not.contain('could not finish drawing');
  });

  it('P2 lists the version and its counts from the flat payload', async () => {
    const m = await mount();
    stub = m.stub;

    const header = m.element.shadowRoot!.querySelector(
      '.version-header'
    ) as HTMLElement;
    expect(header, 'no version row rendered').to.exist;
    header.click();
    await m.element.updateComplete;

    const details = m.element.shadowRoot!.querySelector('.version-details');
    const text = details?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).to.contain('2 MCP servers');
    expect(text).to.contain('7 tools');
    expect(text).to.contain('4 policies');
  });

  it('P3 groups the flat diff list in the import preview dialog', async () => {
    const m = await mount();
    stub = m.stub;

    await (m.element as any).previewPolicyFile(
      new File(['version: "1.0"\n'], 'policy.yaml', { type: 'text/yaml' })
    );
    await m.element.updateComplete;

    const dialog = dialogByLabel(m.element, 'Preview Policy Changes');
    expect(dialog.open, 'diff dialog did not open').to.be.true;
    const text = dialog.textContent.replace(/\s+/g, ' ');
    expect(text).to.contain('Added (1)');
    expect(text).to.contain('Tool: shell');
    expect(text).to.contain('Modified (1)');
    expect(text).to.contain('Model I/O rule: deny-pii');
  });

  it('P4 reads the rollback preview from the diff field', async () => {
    const m = await mount();
    stub = m.stub;

    const rollback = m.element.shadowRoot!.querySelector(
      'sl-icon-button[name="arrow-counterclockwise"]'
    ) as HTMLElement;
    expect(rollback, 'no rollback action rendered').to.exist;
    rollback.click();
    await waitUntil(
      () => Boolean((m.element as any)._rollbackPreview),
      'no rollback preview'
    );
    await m.element.updateComplete;

    const dialog = dialogByLabel(m.element, 'Rollback to Version');
    const text = dialog.textContent.replace(/\s+/g, ' ');
    expect(text).to.contain('The following changes will be made:');
    expect(text).to.contain('Tool: shell');
    const confirm = Array.from(dialog.querySelectorAll('sl-button')).find(
      (button: any) => button.textContent?.includes('Confirm Rollback')
    ) as any;
    expect(confirm.disabled, 'confirm stayed disabled').to.not.be.true;
  });

  it('P5 prunes with the field names the endpoint reads', async () => {
    const m = await mount();
    stub = m.stub;

    (m.element as any)._pruneForm = {
      keepDays: 14,
      keepTagged: true,
      minVersionsToKeep: 5,
    };
    await (m.element as any).pruneVersions();
    await m.element.updateComplete;

    const pruneCall = m.calls.find((call) => call.url.includes('/prune'));
    expect(pruneCall!.body).to.deep.equal({
      older_than_days: 14,
      keep_tagged: true,
      keep_count: 5,
    });
    expect(toastTexts().join(' ')).to.contain('Pruned 3 old versions.');
  });

  it('P6 reports a failure to load versions with a toast', async () => {
    const m = await mount();
    stub = m.stub;
    m.stub.restore();
    stub = sinon.stub(window, 'fetch').resolves(json({ detail: 'nope' }, 500));

    await (m.element as any).loadVersions();
    await m.element.updateComplete;

    expect(toastTexts().join(' ')).to.contain('Failed to fetch versions');
    expect((m.element as any)._error).to.contain('Failed to fetch versions');
  });

  it('P7 surfaces a render failure instead of going silent', async () => {
    const m = await mount();
    stub = m.stub;

    (m.element as any).renderVersionsSection = () => {
      throw new Error('boom');
    };
    m.element.requestUpdate();
    await m.element.updateComplete;

    expect(toastTexts().join(' ')).to.contain('could not finish drawing');
    expect(toastTexts().join(' ')).to.contain('boom');

    // The view keeps accepting updates: the next render is not blocked.
    delete (m.element as any).renderVersionsSection;
    m.element.requestUpdate();
    await m.element.updateComplete;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    expect(ruleDialog(m.element).open).to.be.true;
  });
});
